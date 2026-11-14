"""Character sheet image activity: one sheet image = one paid still call through the guarded path.

Same guards and order as the still submit (hfvg.activities.studio_generation) and the ref edit
(hfvg.activities.ref_generation), on the requesting episode's budget:

  provider key -> smoke test gate -> episode live mode -> ref uploads -> provider /estimate (fail
  closed) -> estimate within the price the creator approved -> balance gate -> credit reserve ->
  L1_refs USD reserve (episode cap, line cap, stop threshold, GC.01 balance required) -> submit
  (idempotency key from the job id) -> per-job ledger row with the episode owner (dashboard) ->
  poll with heartbeats -> commit (credits + USD, actual cost when the provider reports it) ->
  download -> stored in the cast library (refused if the version was locked meanwhile).

A failure before the provider completes releases both holds; a poll that runs out keeps them and
hands the job to the reconciler (job type ``cast_sheet``, see reconcile_cast_sheet_job_activity).
Idempotent: a job whose image is already stored returns it (no spend); a job with a persisted
provider job id resumes polling instead of submitting again. Dry run (DRY_RUN=true): a
deterministic placeholder image, no spend. There is no override that skips the estimate or the
balance gate.

The cast store is synchronous SQLite and file I/O: it always runs in a thread (asyncio.to_thread),
never on the worker's event loop, where waiting for a lock held by another activity's aiosqlite
transaction would block that transaction from finishing (a deadlock until the busy timeout).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg import cast_store
from hfvg.cast_sheets import (
    SheetPlanError,
    check_params,
    check_sheet_model,
    placeholder_image,
    prompt_problems,
)

JOB_KEYS = ("job_id", "character_id", "version", "stage", "view", "attempt", "prompt", "refs", "ref_shas",
            "aspect_ratio", "resolution", "quality", "model_path", "episode_id", "approved_usd_micros")
LINE = "L1_refs"
POLL_INTERVAL_S = 5
POLL_LIMIT = 120


class _PollExhausted(RuntimeError):
    pass


def _refuse(message: str, kind: str = "CastSheetRefused") -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=True)


def _validate(job: dict) -> None:
    missing = [k for k in JOB_KEYS if k not in job]
    if missing:
        raise _refuse(f"sheet job is missing {missing}")
    approved = job["approved_usd_micros"]
    if isinstance(approved, bool) or not isinstance(approved, int) or approved <= 0:
        raise _refuse("sheet job has no approved estimate: the creator approves the price before any spend")
    if len(job["refs"]) != len(job["ref_shas"]):
        raise _refuse("sheet job refs and ref hashes differ")


def _check_refs(job: dict) -> None:
    for path, sha in zip(job["refs"], job["ref_shas"]):
        p = Path(path)
        if not p.is_file():
            raise _refuse(f"reference image {p.name} is missing on disk")
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise _refuse(f"reference image {p.name} changed since the job was planned")


def _stored_image(job_id: str) -> dict | None:
    with cast_store.connect() as conn:
        row = conn.execute("SELECT image_id FROM cast_images WHERE job_id = ? ORDER BY created_at LIMIT 1",
                           (job_id,)).fetchone()
    return cast_store.get_image(row[0]) if row else None


async def _set_status(job_id: str, status: str, *, image_id: str | None = None, error: str | None = None) -> None:
    await asyncio.to_thread(cast_store.update_sheet_job, job_id, status=status, image_id=image_id, error=error)


async def _store(job: dict, data: bytes, *, placeholder: bool, actor: str) -> dict:
    """Store the image in the cast library and complete the job (refused if the version was locked)."""
    image = await asyncio.to_thread(
        cast_store.add_generated_image, job["character_id"], int(job["version"]), data, role=job["view"],
        job_id=job["job_id"], placeholder=placeholder, actor=actor, episode_id=job["episode_id"])
    await _set_status(job["job_id"], "completed", image_id=image["image_id"])
    return image


def _heartbeat(detail: str) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


async def _ensure_budget_lines(db_path: str, episode_id: str) -> None:
    """The episode's credit line and budget lines exist (created as a run start creates them)."""
    import aiosqlite
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    credit = CreditLedger(db_path)
    await credit.init_db()
    await credit.create_episode_line(episode_id)
    budget = BudgetLedger(db_path)
    await budget.init_db()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT 1 FROM budget_lines WHERE line_id = ?", (f"{episode_id}:{LINE}",)) as cur:
            exists = await cur.fetchone()
    if not exists:
        await budget.init_episode_budget(episode_id)


def _mark_stopped(job_id: str, error: Exception) -> None:
    """A job that stopped without a final status (refused early, unexpected error) is marked so the
    Cast panel stops waiting; refused / failed / pending_reconcile set on purpose are kept."""
    with cast_store.connect() as conn:
        row = conn.execute("SELECT status FROM cast_sheet_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row and row[0] in ("queued", "running"):
        refused = isinstance(error, ApplicationError) and error.non_retryable
        cast_store.update_sheet_job(job_id, status="refused" if refused else "failed", error=str(error))


@activity.defn
async def generate_cast_sheet_image_activity(job: dict) -> dict:
    """Generate one character sheet image (see the module docstring for the guard order)."""
    try:
        return await _generate(job)
    except Exception as e:
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if isinstance(job_id, str):
            try:
                await asyncio.to_thread(_mark_stopped, job_id, e)
            except Exception as mark_error:  # the original error is the one to report
                activity.logger.error(f"cast sheet {job_id}: could not record the stop ({mark_error})")
        raise


def _admit(job: dict) -> dict | None:
    """The checks before any work: the version exists and is not locked, the model takes the refs,
    the prompt passes the content policy. Returns the job's already stored image, if any."""
    job_id = job["job_id"]
    try:
        version = cast_store.get_version(job["character_id"], int(job["version"]))
        caps = check_sheet_model(job["model_path"], len(job["refs"]))
        check_params(caps, job["aspect_ratio"])
    except (cast_store.CastError, SheetPlanError) as e:
        cast_store.update_sheet_job(job_id, status="refused", error=str(e))
        raise _refuse(str(e)) from None
    if version["locked"]:
        cast_store.update_sheet_job(job_id, status="refused", error="version locked")
        raise _refuse(f"Version {job['version']} is locked; nothing is generated for a locked sheet")
    problems = prompt_problems(job["prompt"])
    if problems:
        cast_store.update_sheet_job(job_id, status="refused", error="; ".join(problems))
        raise _refuse("sheet prompt refused by the content policy: " + ", ".join(problems))
    return _stored_image(job_id)


def _placeholder(job: dict) -> bytes:
    _check_refs(job)
    return placeholder_image(job)


async def _generate(job: dict) -> dict:
    _validate(job)
    job_id = job["job_id"]
    stored = await asyncio.to_thread(_admit, job)
    if stored:
        await _set_status(job_id, "completed", image_id=stored["image_id"])
        return {"job_id": job_id, "status": "reused", "image_id": stored["image_id"], "usd_micros": 0}

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    if dry_run:
        image = await _store(job, await asyncio.to_thread(_placeholder, job), placeholder=True, actor="worker")
        activity.logger.info(f"[DRY-RUN] cast sheet {job_id}: placeholder {image['image_id']}")
        return {"job_id": job_id, "status": "completed", "image_id": image["image_id"], "usd_micros": 0,
                "placeholder": True}
    return await _generate_live(job)


async def _generate_live(job: dict) -> dict:
    import httpx
    from hfvg.activities.studio_generation import _check_higgsfield_credentials, check_live_mode_and_g108
    from hfvg.balance_monitor import HIGGSFIELD_AUTO_TOPUP_ENABLED, BalanceMonitor
    from hfvg.budget import BudgetLedger
    from hfvg.cost_tracking import CostTracker, extract_actual_cost_from_provider_job
    from hfvg.credit_ledger import CreditLedger
    from hfvg.providers.base import ProviderJobStatus
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    from hfvg.studio_db import enforce_smoke_test_gate, get_episode_user_id

    job_id = job["job_id"]
    episode_id = job["episode_id"]
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    _check_higgsfield_credentials()
    await enforce_smoke_test_gate(db_path, episode_id, "generate_cast_sheet_image_activity")
    live_mode, _g108 = await check_live_mode_and_g108(episode_id)
    if not live_mode:
        await _set_status(job_id, "refused", error="episode not in live mode")
        raise _refuse(f"Episode {episode_id} is not in live mode; switch it to live mode before paid generation")
    await asyncio.to_thread(_check_refs, job)
    await _ensure_budget_lines(db_path, episode_id)
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    usd_ledger = BudgetLedger(db_path)
    await usd_ledger.init_db()

    provider = HiggsfieldStillProvider(model_path=job["model_path"])
    credits_reserved = usd_reserved = False
    estimated_usd, estimated_credits = 0, 0.0
    await _set_status(job_id, "running")
    try:
        params = {"prompt": job["prompt"], "resolution": job["resolution"], "quality": job["quality"],
                  "aspect_ratio": job["aspect_ratio"]}
        provider_job_id = await credit_ledger.get_provider_job(job_id)
        if provider_job_id:
            # submitted by an earlier attempt: its holds are in place, resume polling (no new submit)
            estimated_usd, estimated_credits = await provider.estimate_usd_and_credits(**params)
            credits_reserved = usd_reserved = True
            activity.logger.info(f"cast sheet {job_id}: resuming poll of {provider_job_id}")
        else:
            urls = [(await provider.upload_reference(path, 1))["public_url"] for path in job["refs"]]
            # Estimates guard: the provider's own estimate, fail closed
            estimated_usd, estimated_credits = await provider.estimate_usd_and_credits(**params)
            if estimated_usd > job["approved_usd_micros"]:
                raise _refuse(f"The price went up since it was approved ({estimated_usd} > "
                              f"{job['approved_usd_micros']} usd_micros per image); review the new estimate",
                              "EstimateAboveApproved")
            passed, reason = await BalanceMonitor(db_path).check_preflight_balance(
                provider="higgsfield", estimate_usd_micros=estimated_usd,
                auto_topup_enabled=HIGGSFIELD_AUTO_TOPUP_ENABLED)
            if not passed:
                raise _refuse(f"Balance check failed: {reason}", "InsufficientBalance")
            res = await credit_ledger.reserve_credits(episode_id, job_id, estimated_credits,
                                                      notes=f"cast sheet {job['view']} v{job['version']}")
            if not res.get("reserved"):
                raise _refuse(f"Credit reserve refused for {job_id}: {res}", "CreditReserveRefused")
            credits_reserved = True
            ok = await usd_ledger.reserve(
                episode_id=episode_id, line_name=LINE, amount_usd_micros=estimated_usd,
                reason=f"Cast sheet {job['view']} {job['character_id']} v{job['version']}",
                job_id=job_id, require_balance=True,
            )
            if not ok:
                raise _refuse(f"USD budget reserve refused on {LINE} (at the stop threshold): requested "
                              f"{estimated_usd} usd_micros", "BudgetReserveRefused")
            usd_reserved = True
            key_hash = hashlib.sha256(
                f"{job['model_path']}|{job['prompt']}|{job['resolution']}|{job['quality']}|"
                f"{job['aspect_ratio']}|{','.join(job['ref_shas'])}".encode()).hexdigest()[:16]
            provider_job_id = await provider.submit_image(image_urls=urls, idempotency_key=f"{job_id}-{key_hash}",
                                                          **params)
            await credit_ledger.persist_provider_job(job_id, provider_job_id, provider="higgsfield")
            try:
                await usd_ledger.record_job(episode_id, job_id, None, job["model_path"], "final", LINE,
                                            estimated_usd, user_id=await get_episode_user_id(db_path, episode_id))
            except Exception as ledger_err:  # the provider accepted: never release the hold for this
                activity.logger.error(f"cast sheet {job_id}: job ledger row failed ({ledger_err}); hold kept")
            activity.logger.info(f"cast sheet {job_id} submitted -> {provider_job_id}")

        result = None
        for _ in range(POLL_LIMIT):
            result = await provider.get_job_status(provider_job_id)
            if result.status == ProviderJobStatus.COMPLETED:
                break
            if result.status in (ProviderJobStatus.FAILED, ProviderJobStatus.BLOCKED, ProviderJobStatus.CANCELED):
                raise RuntimeError(f"cast sheet {job_id} {result.status.value}: {result.error}")
            _heartbeat(f"{job_id} {result.status.value}")
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            raise _PollExhausted(f"cast sheet {job_id}: provider still not done after polling")

        await credit_ledger.commit_credits(episode_id, job_id, estimated_credits)
        await usd_ledger.commit(episode_id=episode_id, line_name=LINE, reserved_usd_micros=estimated_usd,
                                actual_usd_micros=estimated_usd, job_id=job_id,
                                reason=f"Cast sheet {job['view']} completed")
        actual, provider_credits, source = extract_actual_cost_from_provider_job(result.model_dump(), "still")
        if actual is not None:
            tracker = CostTracker(db_path)
            await tracker.init_db()
            await tracker.record_actual_cost(job_id=job_id, actual_usd_micros=actual,
                                             provider_credits=provider_credits, cost_source=source)
        if not result.output_url:
            raise RuntimeError(f"cast sheet {job_id}: completed without an output URL")
        async with httpx.AsyncClient() as client:
            resp = await client.get(result.output_url)
            resp.raise_for_status()
            data = resp.content
        _heartbeat(f"{job_id} downloaded")
        image = await _store(job, data, placeholder=False, actor="worker")
        return {"job_id": job_id, "status": "completed", "image_id": image["image_id"],
                "provider_job_id": provider_job_id, "usd_micros": estimated_usd}
    except _PollExhausted as e:
        # The provider may still finish and charge: keep both holds; the reconciler settles the job
        # (commit + store the image, or release) on its next pass.
        await usd_ledger.mark_job_status(job_id, "pending_reconcile")
        await _set_status(job_id, "pending_reconcile", error=str(e))
        raise _refuse(f"{e}; the holds are kept and the reconciler settles it", "CastSheetPending") from None
    except Exception as e:
        committed = await _committed(db_path, job_id)
        if not committed:
            if credits_reserved:
                await credit_ledger.release_credits(episode_id, job_id, notes=f"cast sheet failed: {str(e)[:100]}")
            if usd_reserved:
                await usd_ledger.release(episode_id=episode_id, line_name=LINE, amount_usd_micros=estimated_usd,
                                         reason=f"Cast sheet {job_id} failed: {str(e)[:100]}", job_id=job_id)
        await _set_status(job_id, "failed", error=str(e))
        raise
    finally:
        await provider.close()


async def _committed(db_path: str, job_id: str) -> bool:
    import aiosqlite
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'commit'",
                                  (job_id,)) as cur:
                return (await cur.fetchone())[0] > 0
    except aiosqlite.Error:
        return False


@activity.defn
async def reconcile_cast_sheet_job_activity(job: dict) -> str:
    """The reconciler's pass over a stalled cast sheet job (its activity died after the submit):
    poll the provider once; completed -> commit credits and USD (idempotent per job id) and store the
    image unless the version was locked meanwhile; failed -> release both holds; otherwise leave it
    pending. Returns "committed", "released" or "still_pending"."""
    import aiosqlite
    import httpx
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.providers.base import ProviderJobStatus
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    if os.getenv("DRY_RUN", "true").lower() == "true":
        return "still_pending"  # dry sheet jobs never reserve or reach the provider
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    job_id, episode_id = job["job_id"], job["episode_id"]
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    provider_job_id = await credit_ledger.get_provider_job(job_id)
    if not provider_job_id:
        activity.logger.error(f"cast sheet {job_id}: no provider job id persisted; left pending")
        return "still_pending"
    provider = HiggsfieldStillProvider(model_path=job.get("model") or None)
    try:
        status = await provider.get_job_status(provider_job_id)
    finally:
        await provider.close()
    usd_ledger = BudgetLedger(db_path)
    await usd_ledger.init_db()
    if status.status in (ProviderJobStatus.FAILED, ProviderJobStatus.BLOCKED, ProviderJobStatus.CANCELED):
        reason = f"reconciled {status.status.value}: {status.error or ''}".strip()
        await usd_ledger.release(episode_id=episode_id, line_name=job["line_name"],
                                 amount_usd_micros=job["usd_micros"], reason=reason, job_id=job_id)
        await credit_ledger.release_credits(episode_id, job_id, notes=reason[:200])
        await _set_status(job_id, "failed", error=reason)
        return "released"
    if status.status != ProviderJobStatus.COMPLETED:
        return "still_pending"
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT amount_credits FROM credit_transactions WHERE job_id = ? AND "
                              "txn_type = 'reserve'", (job_id,)) as cur:
            row = await cur.fetchone()
    if row:
        await credit_ledger.commit_credits(episode_id, job_id, row[0])
    await usd_ledger.commit(episode_id=episode_id, line_name=job["line_name"], reserved_usd_micros=job["usd_micros"],
                            actual_usd_micros=job["usd_micros"], job_id=job_id,
                            reason="Cast sheet reconciled: completed")
    sheet = await asyncio.to_thread(_unstored_sheet_job, job_id)
    if sheet and status.output_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(status.output_url)
                resp.raise_for_status()
            await _store({**sheet, "episode_id": episode_id}, resp.content, placeholder=False, actor="reconciler")
        except (cast_store.CastError, httpx.HTTPError) as e:  # paid and settled; the image is just not kept
            await _set_status(job_id, "failed", error=f"completed but not stored: {e}")
    return "committed"


def _unstored_sheet_job(job_id: str) -> dict | None:
    """The sheet job row, when its image is not stored yet."""
    with cast_store.connect() as conn:
        row = conn.execute("SELECT * FROM cast_sheet_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row and not _stored_image(job_id) else None


def _finish(job_ids: list[str], status: str, reason: str) -> None:
    for job_id in job_ids:
        cast_store.update_sheet_job(job_id, status=status, error=reason)


@activity.defn
async def finish_cast_sheet_jobs_activity(job_ids: list[str], status: str, reason: str) -> int:
    """Mark the jobs a sheet run did not get to (after a failure or refusal) so the UI stops waiting."""
    if status not in ("skipped", "failed"):
        raise _refuse(f"unknown sheet job status {status!r}")
    await asyncio.to_thread(_finish, job_ids, status, reason)
    return len(job_ids)

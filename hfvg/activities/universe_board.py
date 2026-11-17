"""Live mood board picture activity: one picture = one paid still call through the guarded path.

The character sheet path (hfvg.activities.cast_sheet) with the same provider adapter, on the
Universe's own budget line (hfvg.universe_boards: LINE under the ledger key ``universe-<series id>``):

  provider key -> reference photos unchanged on disk -> ref uploads -> provider /estimate (fail
  closed) -> estimate within the price the owner approved -> balance gate -> credit reserve -> USD
  reserve (line cap, stop threshold, GC.01 balance required) -> submit (idempotency key from the
  job id) -> per-job ledger row in the Universe owner's name (dashboard, cost truth) -> poll with
  heartbeats -> commit (credits + USD, actual cost when the provider reports it) -> download -> kept
  with its board request (hfvg.world_store.store_board_job_picture).

The episode gates of a still (smoke test, live mode) belong to an episode, and a Universe has none;
every money guard above applies, and there is no override that skips the estimate or the balance
gate. A failure before the provider completes releases both holds; a poll that runs out keeps them
and hands the job to the reconciler (job type ``universe_board``, see
reconcile_universe_board_job_activity). Idempotent: a job whose picture is kept returns it (no
spend); a job with a persisted provider job id resumes polling instead of submitting again. Dry run
(DRY_RUN=true): a deterministic placeholder picture, no spend.

The cast store is synchronous SQLite and file I/O: it always runs in a thread (asyncio.to_thread),
as in the cast sheet activity.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg import world_store
from hfvg.actual_costs import record_status_charge
from hfvg.cast_sheets import prompt_problems
from hfvg.universe_boards import LINE, LINE_CAP_USD_MICROS, BoardPlanError, check_model, ledger_key

JOB_KEYS = ("job_id", "request_id", "series_id", "position", "prompt", "refs", "ref_shas", "model_path",
            "resolution", "quality", "aspect_ratio", "ledger_key", "owner_user_id", "approved_usd_micros")
POLL_INTERVAL_S = 5
POLL_LIMIT = 120


class _PollLimitError(RuntimeError):
    pass


def _refuse(message: str, kind: str = "MoodBoardRefused") -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=True)


def _validate(job: dict) -> None:
    missing = [k for k in JOB_KEYS if k not in job]
    if missing:
        raise _refuse(f"mood board job is missing {missing}")
    approved = job["approved_usd_micros"]
    if isinstance(approved, bool) or not isinstance(approved, int) or approved <= 0:
        raise _refuse("mood board job has no approved estimate: the owner approves the price before any spend")
    if len(job["refs"]) != len(job["ref_shas"]):
        raise _refuse("mood board job refs and ref hashes differ")
    if job["ledger_key"] != ledger_key(job["series_id"]):
        raise _refuse("mood board job is not booked to its Universe's mood board budget")


def _check_refs(job: dict) -> None:
    for path, sha in zip(job["refs"], job["ref_shas"]):
        p = Path(path)
        if not p.is_file():
            raise _refuse(f"reference photo {p.name} is missing on disk")
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise _refuse(f"reference photo {p.name} changed since the board was planned")


async def _set_status(job_id: str, status: str, error: str | None = None) -> None:
    await asyncio.to_thread(world_store.set_board_job, job_id, status, error)


async def _store(job: dict, data: bytes, *, placeholder: bool) -> dict:
    return await asyncio.to_thread(world_store.store_board_job_picture, job["job_id"], data, placeholder=placeholder)


def _heartbeat(detail: str) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


async def _ensure_budget_lines(db_path: str, key: str) -> None:
    """The Universe's credit line and mood board budget line exist (created on first use, never reset)."""
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    credit = CreditLedger(db_path)
    await credit.init_db()
    await credit.create_episode_line(key)
    await BudgetLedger(db_path).ensure_line(key, LINE, LINE_CAP_USD_MICROS)


def _mark_stopped(job_id: str, error: Exception) -> None:
    """A job that stopped without a final status (refused early, unexpected error) is marked so the
    Universe page stops waiting; refused / failed / pending_reconcile set on purpose are kept."""
    row = world_store.board_job(job_id)
    if row and row["status"] in ("queued", "running"):
        refused = isinstance(error, ApplicationError) and error.non_retryable
        world_store.set_board_job(job_id, "refused" if refused else "failed", str(error))


@activity.defn
async def generate_universe_board_image_activity(job: dict) -> dict:
    """Make one picture of a live mood board (see the module docstring for the guard order)."""
    try:
        return await _generate(job)
    except Exception as e:
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if isinstance(job_id, str):
            try:
                await asyncio.to_thread(_mark_stopped, job_id, e)
            except Exception as mark_error:  # the original error is the one to report
                activity.logger.error(f"mood board {job_id}: could not record the stop ({mark_error})")
        raise


def _admit(job: dict) -> dict | None:
    """The checks before any work: the job belongs to a board still being made, the model makes it,
    the prompt passes the content policy. Returns the job when its picture is kept already."""
    job_id = job["job_id"]
    row = world_store.board_job(job_id)
    if row is None or row["request_id"] != job["request_id"] or row["series_id"] != job["series_id"]:
        raise _refuse(f"mood board job {job_id} is not recorded for this board")
    if row["sha256"]:
        return row
    if row["request_status"] != "making":
        world_store.set_board_job(job_id, "refused", "the board request ended")
        raise _refuse(f"The mood board request {job['request_id']} ended; nothing is generated for it")
    try:
        check_model(job["model_path"], len(job["refs"]))
    except BoardPlanError as e:
        world_store.set_board_job(job_id, "refused", str(e))
        raise _refuse(str(e)) from None
    problems = prompt_problems(job["prompt"])
    if problems:
        world_store.set_board_job(job_id, "refused", "; ".join(problems))
        raise _refuse("mood board prompt refused by the content policy: " + ", ".join(problems))
    return None


def _placeholder(job: dict) -> bytes:
    _check_refs(job)
    return world_store.placeholder_board_image(job["request_id"], job["refs"], int(job["position"]))


async def _generate(job: dict) -> dict:
    _validate(job)
    job_id = job["job_id"]
    stored = await asyncio.to_thread(_admit, job)
    if stored:
        await _set_status(job_id, "completed")
        return {"job_id": job_id, "status": "reused", "usd_micros": 0}
    if os.getenv("DRY_RUN", "true").lower() == "true":
        await _store(job, await asyncio.to_thread(_placeholder, job), placeholder=True)
        activity.logger.info(f"[DRY-RUN] mood board {job_id}: placeholder")
        return {"job_id": job_id, "status": "completed", "usd_micros": 0, "placeholder": True}
    return await _generate_live(job)


async def _generate_live(job: dict) -> dict:
    import httpx

    from hfvg.activities.studio_generation import _check_higgsfield_credentials
    from hfvg.balance_monitor import HIGGSFIELD_AUTO_TOPUP_ENABLED, BalanceMonitor
    from hfvg.budget import BudgetLedger
    from hfvg.cost_tracking import CostTracker, extract_actual_cost_from_provider_job
    from hfvg.credit_ledger import CreditLedger
    from hfvg.providers.base import ProviderJobStatus
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    job_id, key = job["job_id"], job["ledger_key"]
    picture = f"picture {int(job['position']) + 1} of mood board {job['request_id']}"
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    _check_higgsfield_credentials()
    await asyncio.to_thread(_check_refs, job)
    await _ensure_budget_lines(db_path, key)
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
            activity.logger.info(f"mood board {job_id}: resuming poll of {provider_job_id}")
        else:
            urls = [(await provider.upload_reference(path, 1))["public_url"] for path in job["refs"]]
            # Estimates guard: the provider's own estimate, fail closed
            estimated_usd, estimated_credits = await provider.estimate_usd_and_credits(**params)
            if estimated_usd > job["approved_usd_micros"]:
                raise _refuse(f"The price went up since it was approved ({estimated_usd} > "
                              f"{job['approved_usd_micros']} usd_micros per picture); review the new estimate",
                              "EstimateAboveApproved")
            passed, reason = await BalanceMonitor(db_path).check_preflight_balance(
                provider="higgsfield", estimate_usd_micros=estimated_usd,
                auto_topup_enabled=HIGGSFIELD_AUTO_TOPUP_ENABLED)
            if not passed:
                raise _refuse(f"Balance check failed: {reason}", "InsufficientBalance")
            res = await credit_ledger.reserve_credits(key, job_id, estimated_credits, notes=picture)
            if not res.get("reserved"):
                raise _refuse(f"Credit reserve refused for {job_id}: {res}", "CreditReserveRefused")
            credits_reserved = True
            ok = await usd_ledger.reserve(
                episode_id=key, line_name=LINE, amount_usd_micros=estimated_usd,
                reason=f"Mood board {picture}", job_id=job_id, require_balance=True,
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
                await usd_ledger.record_job(key, job_id, None, job["model_path"], "final", LINE, estimated_usd,
                                            user_id=job["owner_user_id"])
            except Exception as ledger_err:  # the provider accepted: never release the hold for this
                activity.logger.error(f"mood board {job_id}: job ledger row failed ({ledger_err}); hold kept")
            activity.logger.info(f"mood board {job_id} submitted -> {provider_job_id}")

        result = None
        for _ in range(POLL_LIMIT):
            result = await provider.get_job_status(provider_job_id)
            if result.status == ProviderJobStatus.COMPLETED:
                break
            if result.status in (ProviderJobStatus.FAILED, ProviderJobStatus.BLOCKED, ProviderJobStatus.CANCELED):
                raise RuntimeError(f"mood board {job_id} {result.status.value}: {result.error}")
            _heartbeat(f"{job_id} {result.status.value}")
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            raise _PollLimitError(f"mood board {job_id}: provider still not done after polling")

        await credit_ledger.commit_credits(key, job_id, estimated_credits)
        await usd_ledger.commit(episode_id=key, line_name=LINE, reserved_usd_micros=estimated_usd,
                                actual_usd_micros=estimated_usd, job_id=job_id,
                                reason=f"Mood board {picture} completed")
        await record_status_charge(db_path, job_id, getattr(result, "raw", None))
        actual, provider_credits, source = extract_actual_cost_from_provider_job(result.model_dump(), "still")
        if actual is not None:
            tracker = CostTracker(db_path)
            await tracker.init_db()
            await tracker.record_actual_cost(job_id=job_id, actual_usd_micros=actual,
                                             provider_credits=provider_credits, cost_source=source)
        if not result.output_url:
            raise RuntimeError(f"mood board {job_id}: completed without an output URL")
        async with httpx.AsyncClient() as client:
            resp = await client.get(result.output_url)
            resp.raise_for_status()
            data = resp.content
        _heartbeat(f"{job_id} downloaded")
        await _store(job, data, placeholder=False)
        return {"job_id": job_id, "status": "completed", "provider_job_id": provider_job_id,
                "usd_micros": estimated_usd}
    except _PollLimitError as e:
        # The provider may still finish and charge: keep both holds; the reconciler settles the job
        # (commit + keep the picture, or release) on its next pass.
        await usd_ledger.mark_job_status(job_id, "pending_reconcile")
        await _set_status(job_id, "pending_reconcile", str(e))
        raise _refuse(f"{e}; the holds are kept and the reconciler settles it", "MoodBoardPending") from None
    except Exception as e:
        if not await _committed(db_path, job_id):
            if credits_reserved:
                await credit_ledger.release_credits(key, job_id, notes=f"mood board failed: {str(e)[:100]}")
            if usd_reserved:
                await usd_ledger.release(episode_id=key, line_name=LINE, amount_usd_micros=estimated_usd,
                                         reason=f"Mood board {job_id} failed: {str(e)[:100]}", job_id=job_id)
        await _set_status(job_id, "failed", str(e))
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
async def finish_universe_board_activity(request_id: str, error: str | None = None) -> dict:
    """End a live board request (hfvg.world_store.finish_board_request): the next board version when
    every picture was made, else failed with the reason (its jobs that did not run are skipped)."""
    view = await asyncio.to_thread(world_store.finish_board_request, request_id, error, "worker")
    return {"request_id": request_id, "status": view["status"], "board_id": view["board_id"], "made": view["made"]}


@activity.defn
async def reconcile_universe_board_job_activity(job: dict) -> str:
    """The reconciler's pass over a stalled mood board picture (its activity died after the submit):
    poll the provider once; completed -> commit credits and USD (idempotent per job id) and keep the
    picture with its request; failed -> release both holds; otherwise leave it pending. The board
    request itself is not changed (it ended when its workflow did). Returns "committed",
    "released" or "still_pending"."""
    import aiosqlite
    import httpx

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.providers.base import ProviderJobStatus
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    if os.getenv("DRY_RUN", "true").lower() == "true":
        return "still_pending"  # dry board jobs never reserve or reach the provider
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    job_id, key = job["job_id"], job["episode_id"]
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    provider_job_id = await credit_ledger.get_provider_job(job_id)
    if not provider_job_id:
        activity.logger.error(f"mood board {job_id}: no provider job id persisted; left pending")
        return "still_pending"
    provider = HiggsfieldStillProvider(model_path=job.get("model") or None)
    try:
        status = await provider.get_job_status(provider_job_id)
    finally:
        await provider.close()
    usd_ledger = BudgetLedger(db_path)
    await usd_ledger.init_db()
    row = await asyncio.to_thread(world_store.board_job, job_id)
    if status.status in (ProviderJobStatus.FAILED, ProviderJobStatus.BLOCKED, ProviderJobStatus.CANCELED):
        reason = f"reconciled {status.status.value}: {status.error or ''}".strip()
        await usd_ledger.release(episode_id=key, line_name=job["line_name"], amount_usd_micros=job["usd_micros"],
                                 reason=reason, job_id=job_id)
        await credit_ledger.release_credits(key, job_id, notes=reason[:200])
        if row:
            await _set_status(job_id, "failed", reason)
        return "released"
    if status.status != ProviderJobStatus.COMPLETED:
        return "still_pending"
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT amount_credits FROM credit_transactions WHERE job_id = ? AND "
                              "txn_type = 'reserve'", (job_id,)) as cur:
            reserved = await cur.fetchone()
    if reserved:
        await credit_ledger.commit_credits(key, job_id, reserved[0])
    await usd_ledger.commit(episode_id=key, line_name=job["line_name"], reserved_usd_micros=job["usd_micros"],
                            actual_usd_micros=job["usd_micros"], job_id=job_id,
                            reason="Mood board picture reconciled: completed")
    await record_status_charge(db_path, job_id, getattr(status, "raw", None))
    if row and not row["sha256"] and status.output_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(status.output_url)
                resp.raise_for_status()
            await _store({"job_id": job_id}, resp.content, placeholder=False)
        except (world_store.CastError, httpx.HTTPError) as e:  # paid and settled; the picture is just not kept
            await _set_status(job_id, "failed", f"completed but not kept: {e}")
    return "committed"

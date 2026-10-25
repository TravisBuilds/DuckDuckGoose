"""Reconciler + stall watchdog activities (Round 2).

Jobs live in the per-job USD ledger (``job_ledger``, hfvg.budget):

- ``reserved``            submitted, hold taken, not finished yet
- ``pending_reconcile``   the shot workflow's poll window ran out (or the watchdog routed it here);
                          the hold is still taken
- ``committed``/``released``  terminal (commit/release are idempotent per job id: never double-charged)

Watchdog (:func:`detect_stalled_jobs`): a ``reserved`` job whose ledger row has not moved for
``STALL_THRESHOLD_MINUTES`` (default 45) gets a ``stall_alerts`` row (visible via
``GET /api/stalls`` and ``GET /api/episodes/{id}/stalls``), an audit entry, and is routed to the
reconciler (status -> ``pending_reconcile``).

Reconciler (workflow ``ReconcilePendingJobsWorkflow``, run on a Temporal Schedule): re-poll each
``pending_reconcile`` job by its harness job id; completed -> commit (idempotent) and land the
late asset (:func:`land_late_asset`: download, normalize, attach); failed/blocked/canceled ->
release (idempotent); still running -> left for the next run. Late assets whose landing failed
are retried by later runs (bounded by ``MAX_LAND_ATTEMPTS``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from temporalio import activity

from hfvg.budget import BudgetLedger

MAX_LAND_ATTEMPTS = 5


def _db_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/studio.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def stall_threshold_minutes() -> int:
    """STALL_THRESHOLD_MINUTES (env at call time; default 45). Must be a positive integer."""
    value = int(os.getenv("STALL_THRESHOLD_MINUTES", "45"))
    if value <= 0:
        raise ValueError(f"STALL_THRESHOLD_MINUTES must be positive, got {value}")
    return value


def job_type_of(job_id: str) -> str:
    """Harness job ids are still-... / clip-... (see submit_*_job_enforced)."""
    if job_id.startswith("still-"):
        return "still"
    if job_id.startswith("clip-"):
        return "clip"
    raise ValueError(f"Cannot derive job type from job id {job_id!r}")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            ts = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # SQLite datetime('now') is UTC
    return ts


async def _ensure_schema(db_path: str) -> None:
    from hfvg.studio_db import init_studio_db
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()


async def _audit(db, episode_id: str, action: str, details: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
        (episode_id, action, json.dumps(details, sort_keys=True), "reconciler"))


async def audit_event(episode_id: str, action: str, details: dict[str, Any]) -> None:
    db_path = _db_path()
    await _ensure_schema(db_path)
    async with aiosqlite.connect(db_path) as db:
        await _audit(db, episode_id, action, details)
        await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Stall watchdog
# ─────────────────────────────────────────────────────────────────────────────

async def find_stalled_jobs(db_path: str, threshold_minutes: int, now: datetime | None = None) -> list[dict]:
    """Read-only: ``reserved`` jobs whose ledger row has not moved for >= threshold minutes."""
    now = now or _now()
    await _ensure_schema(db_path)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("""
            SELECT job_id, episode_id, shot_id, model, line_name, usd_micros, created_at, updated_at
            FROM job_ledger WHERE status = 'reserved' ORDER BY created_at, rowid
        """) as cur:
            rows = await cur.fetchall()
    stalled = []
    for job_id, episode_id, shot_id, model, line_name, usd, created_at, updated_at in rows:
        last = _parse_ts(updated_at) or _parse_ts(created_at)
        if last is None:
            continue
        minutes = int((now - last).total_seconds() // 60)
        if minutes >= threshold_minutes:
            stalled.append({
                "job_id": job_id, "episode_id": episode_id, "shot_id": shot_id, "model": model,
                "line_name": line_name, "usd_micros": usd, "job_type": job_type_of(job_id),
                "minutes_stalled": minutes, "threshold_minutes": threshold_minutes,
            })
    return stalled


async def raise_stall_alerts(db_path: str, threshold_minutes: int | None = None,
                             route: bool = True, now: datetime | None = None) -> list[dict]:
    """Detect stalled jobs, record/refresh their stall alert and audit entry, and (``route``)
    hand them to the reconciler (job status -> pending_reconcile; the hold stays taken)."""
    threshold = threshold_minutes if threshold_minutes is not None else stall_threshold_minutes()
    stalled = await find_stalled_jobs(db_path, threshold, now)
    if not stalled:
        return []
    ts = (now or _now()).isoformat()
    ledger = BudgetLedger(db_path)
    async with aiosqlite.connect(db_path) as db:
        for job in stalled:
            await db.execute("""
                INSERT INTO stall_alerts (job_id, episode_id, shot_id, job_type, minutes_stalled,
                                          threshold_minutes, status, detected_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET minutes_stalled = excluded.minutes_stalled,
                    threshold_minutes = excluded.threshold_minutes, updated_at = excluded.updated_at
            """, (job["job_id"], job["episode_id"], job["shot_id"], job["job_type"],
                  job["minutes_stalled"], threshold, ts, ts))
            await _audit(db, job["episode_id"], "job_stalled", {
                "job_id": job["job_id"], "shot_id": job["shot_id"], "job_type": job["job_type"],
                "minutes_stalled": job["minutes_stalled"], "threshold_minutes": threshold,
                "routed_to_reconciler": route})
        await db.commit()
    if route:
        for job in stalled:
            await ledger.mark_job_status(job["job_id"], "pending_reconcile")
        async with aiosqlite.connect(db_path) as db:
            for job in stalled:
                await db.execute(
                    "UPDATE stall_alerts SET status = 'routed', updated_at = ? WHERE job_id = ? AND status = 'open'",
                    (ts, job["job_id"]))
            await db.commit()
        for job in stalled:
            job["routed"] = True
    return stalled


@activity.defn
async def detect_stalled_jobs(threshold_minutes: int | None = None) -> list[dict]:
    """Watchdog activity: alert on stalled ``reserved`` jobs and route them to the reconciler."""
    stalled = await raise_stall_alerts(_db_path(), threshold_minutes, route=True)
    for job in stalled:
        activity.logger.warning(
            f"Stalled {job['job_type']} job {job['job_id']} ({job['episode_id']}/{job['shot_id']}): "
            f"{job['minutes_stalled']} min >= {job['threshold_minutes']} min; routed to reconciler")
    return stalled


async def list_stall_alerts(db_path: str, episode_id: str | None = None,
                            status: str | None = None) -> list[dict]:
    await _ensure_schema(db_path)
    sql = ("SELECT job_id, episode_id, shot_id, job_type, minutes_stalled, threshold_minutes, status, "
           "resolution, detected_at, updated_at FROM stall_alerts")
    clauses, args = [], []
    if episode_id:
        clauses.append("episode_id = ?")
        args.append(episode_id)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY detected_at, job_id"
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
    keys = ("job_id", "episode_id", "shot_id", "job_type", "minutes_stalled", "threshold_minutes",
            "status", "resolution", "detected_at", "updated_at")
    return [dict(zip(keys, r)) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Reconciler
# ─────────────────────────────────────────────────────────────────────────────

@activity.defn
async def get_pending_reconcile_jobs(max_jobs: int = 100) -> list[dict]:
    """Work list: ``pending_reconcile`` jobs (re-poll) plus committed late assets whose landing
    failed and may be retried (re-land, no re-poll, no second commit)."""
    db_path = _db_path()
    await _ensure_schema(db_path)
    jobs: list[dict] = []
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("""
            SELECT job_id, episode_id, shot_id, model, line_name, usd_micros, status
            FROM job_ledger WHERE status = 'pending_reconcile' ORDER BY created_at, rowid LIMIT ?
        """, (max_jobs,)) as cur:
            for job_id, episode_id, shot_id, model, line_name, usd, status in await cur.fetchall():
                jobs.append({"job_id": job_id, "episode_id": episode_id, "shot_id": shot_id,
                             "model": model, "line_name": line_name, "usd_micros": usd,
                             "job_type": job_type_of(job_id), "phase": "poll"})
        remaining = max_jobs - len(jobs)
        if remaining > 0:
            async with db.execute("""
                SELECT la.job_id, la.episode_id, la.shot_id, la.job_type, la.output_url, la.attempts
                FROM late_assets la JOIN job_ledger jl ON jl.job_id = la.job_id
                WHERE la.status = 'land_failed' AND la.attempts < ? AND jl.status = 'committed'
                ORDER BY la.updated_at LIMIT ?
            """, (MAX_LAND_ATTEMPTS, remaining)) as cur:
                for job_id, episode_id, shot_id, job_type, url, attempts in await cur.fetchall():
                    jobs.append({"job_id": job_id, "episode_id": episode_id, "shot_id": shot_id,
                                 "job_type": job_type, "output_url": url, "attempts": attempts,
                                 "phase": "land"})
    return jobs


_STILL_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))


def _sniff(job_type: str, data: bytes) -> str:
    """Return the canonical extension for the bytes, or raise ValueError (not a usable asset)."""
    if not data:
        raise ValueError("empty asset")
    if job_type == "still":
        for magic, ext in _STILL_MAGIC:
            if data.startswith(magic):
                return ext
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        raise ValueError("still is not a PNG/JPEG/WebP image")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    raise ValueError("clip is not an MP4/WebM video")


def _normalize(job_type: str, data: bytes) -> tuple[bytes, str]:
    """Normalize a landed asset. Stills: decoded with Pillow and re-encoded as RGB PNG (the
    harness still format; also proves the file decodes). Clips: container verified, kept as-is."""
    ext = _sniff(job_type, data)
    if job_type == "still":
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            out = io.BytesIO()
            img.convert("RGB").save(out, format="PNG")
        return out.getvalue(), ".png"
    return data, ext


async def _download(url: str) -> bytes:
    if not url.startswith(("http://", "https://")):
        path = Path(url)
        if not path.exists():
            raise FileNotFoundError(f"late asset {url} does not exist")
        return path.read_bytes()
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _attach(db, job: dict, url: str, local_path: str, sha: str) -> bool:
    """Attach the late asset to its shots row only when the shot has no asset of that kind yet
    (a newer landed asset always wins); the asset is listed under qc_results.late_assets either way."""
    shot_id = job["shot_id"] or ""
    is_end = shot_id.endswith("_end")
    base_shot = shot_id[:-4] if is_end else shot_id
    record_id = f"{job['episode_id']}-{base_shot}"
    column = "clip_url" if job["job_type"] == "clip" else "still_url"
    async with db.execute(f"SELECT {column}, qc_results, status FROM shots WHERE id = ?", (record_id,)) as cur:
        row = await cur.fetchone()
    attach = not is_end and (row is None or not row[0])
    qc: dict[str, Any] = {}
    if row and row[1]:
        try:
            qc = json.loads(row[1]) or {}
        except ValueError:
            qc = {}
    late = [a for a in qc.get("late_assets", []) if a.get("job_id") != job["job_id"]]
    late.append({"job_id": job["job_id"], "kind": job["job_type"], "url": url, "local_path": local_path,
                 "sha256": sha, "attached": attach, "shot_id": shot_id})
    qc["late_assets"] = late
    if row is None:
        await db.execute(f"""
            INSERT INTO shots (id, episode_id, shot_id, status, {column}, qc_results, created_at, updated_at)
            VALUES (?, ?, ?, 'needs_review', ?, ?, datetime('now'), datetime('now'))
        """, (record_id, job["episode_id"], base_shot, url if attach else None, json.dumps(qc)))
    elif attach:
        new_status = "needs_review" if row[2] in ("failed", "pending", "in_progress") else row[2]
        await db.execute(f"""
            UPDATE shots SET {column} = ?, qc_results = ?, status = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (url, json.dumps(qc), new_status, record_id))
    else:
        await db.execute("UPDATE shots SET qc_results = ?, updated_at = datetime('now') WHERE id = ?",
                         (json.dumps(qc), record_id))
    return attach


def _output_root() -> Path:
    return Path(os.getenv("HFVG_EPISODES_DIR", "data/episodes"))


@activity.defn
async def land_late_asset(job: dict, output_url: str) -> dict:
    """Download -> normalize -> attach a late (reconciled) asset. Idempotent per job id: a landed
    asset is never re-downloaded. Never raises for a bad asset: the failure is recorded in
    ``late_assets`` (status land_failed, attempts+1) and retried by a later reconciler run."""
    db_path = _db_path()
    await _ensure_schema(db_path)
    job_id = job["job_id"]
    now = _now().isoformat()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status, local_path, attached, attempts FROM late_assets WHERE job_id = ?",
                              (job_id,)) as cur:
            existing = await cur.fetchone()
    if existing and existing[0] == "landed":
        return {"job_id": job_id, "landed": True, "local_path": existing[1], "attached": bool(existing[2]),
                "idempotent": True}
    attempts = (existing[3] if existing else 0) + 1
    try:
        data = await _download(output_url)
        normalized, ext = _normalize(job["job_type"], data)
        out_dir = _output_root() / job["episode_id"] / "outputs" / (job["shot_id"] or "unknown") / "late"
        out_dir.mkdir(parents=True, exist_ok=True)
        local_path = out_dir / f"{job_id}{ext}"
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        tmp.write_bytes(normalized)
        shutil.move(str(tmp), str(local_path))
        sha = hashlib.sha256(normalized).hexdigest()
    except Exception as e:  # noqa: BLE001 - recorded, retried by the next run
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                INSERT INTO late_assets (job_id, episode_id, shot_id, job_type, output_url, status,
                                         attempts, error, updated_at)
                VALUES (?, ?, ?, ?, ?, 'land_failed', ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET status = 'land_failed', attempts = excluded.attempts,
                    error = excluded.error, output_url = excluded.output_url, updated_at = excluded.updated_at
            """, (job_id, job["episode_id"], job["shot_id"], job["job_type"], output_url, attempts,
                  f"{type(e).__name__}: {e}", now))
            await _audit(db, job["episode_id"], "late_asset_land_failed", {
                "job_id": job_id, "attempt": attempts, "error": f"{type(e).__name__}: {e}",
                "will_retry": attempts < MAX_LAND_ATTEMPTS})
            await db.commit()
        await BudgetLedger(db_path).set_job_verdict(job_id, "late_land_failed")
        return {"job_id": job_id, "landed": False, "error": f"{type(e).__name__}: {e}", "attempts": attempts}

    async with aiosqlite.connect(db_path) as db:
        attached = await _attach(db, job, output_url, str(local_path), sha)
        await db.execute("""
            INSERT INTO late_assets (job_id, episode_id, shot_id, job_type, output_url, local_path, sha256,
                                     attached, status, attempts, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'landed', ?, NULL, ?)
            ON CONFLICT(job_id) DO UPDATE SET local_path = excluded.local_path, sha256 = excluded.sha256,
                attached = excluded.attached, status = 'landed', attempts = excluded.attempts,
                error = NULL, updated_at = excluded.updated_at
        """, (job_id, job["episode_id"], job["shot_id"], job["job_type"], output_url, str(local_path), sha,
              int(attached), attempts, now))
        await _audit(db, job["episode_id"], "late_asset_landed", {
            "job_id": job_id, "local_path": str(local_path), "sha256": sha, "attached": attached})
        await db.commit()
    await BudgetLedger(db_path).set_job_verdict(job_id, "late_landed")
    return {"job_id": job_id, "landed": True, "local_path": str(local_path), "attached": attached,
            "sha256": sha}


@activity.defn
async def record_reconcile_outcome(job: dict, outcome: str, detail: str = "") -> None:
    """Audit one reconciler decision and resolve the job's stall alert on a terminal outcome."""
    db_path = _db_path()
    await _ensure_schema(db_path)
    async with aiosqlite.connect(db_path) as db:
        await _audit(db, job["episode_id"], f"reconcile_{outcome}", {
            "job_id": job["job_id"], "shot_id": job.get("shot_id"), "job_type": job.get("job_type"),
            "detail": detail})
        if outcome in ("committed", "released", "landed", "land_abandoned"):
            await db.execute(
                "UPDATE stall_alerts SET status = 'resolved', resolution = ?, updated_at = ? "
                "WHERE job_id = ? AND status != 'resolved'",
                (outcome, _now().isoformat(), job["job_id"]))
        await db.commit()

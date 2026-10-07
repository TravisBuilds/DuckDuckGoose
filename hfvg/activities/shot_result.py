"""
Shot result recording activity.
"""

import aiosqlite
import json
from typing import Any

from temporalio import activity


@activity.defn
async def record_shot_result(
    db_path: str,
    episode_id: str,
    shot_id: str,
    status: str,
    still_url: str | None = None,
    clip_url: str | None = None,
    qc_results: dict[str, Any] | None = None,
    retries: int = 0,
    prompt: str | None = None,
    clear_clip: bool = False,
) -> None:
    """
    Record shot workflow result in shots table.
    
    Args:
        db_path: Path to SQLite database
        episode_id: Episode identifier
        shot_id: Shot identifier (e.g., "A01")
        status: Shot status (pending, in_progress, complete, failed)
        still_url: URL to still image
        clip_url: URL to clip video
        qc_results: QC verdicts dict with gates (duck_identity, motion, stillgate, etc.)
        retries: Number of retries
        prompt: Generation prompt
        clear_clip: If True, explicitly clear clip_url (for when a new still is generated)
    """
    async with aiosqlite.connect(db_path) as db:
        record_id = f"{episode_id}-{shot_id}"
        
        qc_json = json.dumps(qc_results) if qc_results else None
        
        # Insert or update
        # Use COALESCE for still_url and prompt to preserve existing values
        # For clip_url, clear it explicitly if clear_clip=True, otherwise use COALESCE
        if clear_clip:
            # Clear clip_url when starting a new still generation
            await db.execute(
                """INSERT INTO shots 
                   (id, episode_id, shot_id, status, still_url, clip_url, 
                    qc_results, retries, prompt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                       status = excluded.status,
                       still_url = COALESCE(excluded.still_url, still_url),
                       clip_url = NULL,
                       qc_results = COALESCE(excluded.qc_results, qc_results),
                       retries = excluded.retries,
                       prompt = COALESCE(excluded.prompt, prompt),
                       updated_at = datetime('now')
                """,
                (record_id, episode_id, shot_id, status, still_url, clip_url, qc_json, retries, prompt)
            )
        else:
            # Normal update with COALESCE for all optional fields
            await db.execute(
                """INSERT INTO shots 
                   (id, episode_id, shot_id, status, still_url, clip_url, 
                    qc_results, retries, prompt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                       status = excluded.status,
                       still_url = COALESCE(excluded.still_url, still_url),
                       clip_url = COALESCE(excluded.clip_url, clip_url),
                       qc_results = COALESCE(excluded.qc_results, qc_results),
                       retries = excluded.retries,
                       prompt = COALESCE(excluded.prompt, prompt),
                       updated_at = datetime('now')
                """,
                (record_id, episode_id, shot_id, status, still_url, clip_url, qc_json, retries, prompt)
            )
        
        await db.commit()
        
        activity.logger.info(
            f"Recorded shot result: {episode_id}/{shot_id} status={status} "
            f"still={still_url is not None} clip={clip_url is not None} clear_clip={clear_clip}"
        )

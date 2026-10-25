"""Activity: prepare a pre-approved start still (see hfvg.pre_approved)."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg.pre_approved import STILL_EXTENSIONS
from hfvg.shot_data import shots_json_path

MAX_STILL_BYTES = 50 * 1024 * 1024


def resolve_pre_approved_path(episode_id: str, path: str) -> Path:
    """Absolute path of a pre-approved still; relative paths are relative to the episode dir
    (the directory holding shots.json)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = shots_json_path(episode_id).parent / p
    return p.resolve()


def outputs_dir(episode_id: str, shot_id: str) -> Path:
    """Same layout as the G2 anchor lookup in the API: <DATA_DIR>/episodes/<ep>/outputs/<shot>/."""
    return Path(os.getenv("DATA_DIR", "./data")) / "episodes" / episode_id / "outputs" / shot_id


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@activity.defn
async def prepare_pre_approved_still_activity(
    episode_id: str, shot_id: str, spec: dict[str, Any], version: int = 1
) -> dict[str, Any]:
    """Verify the pre-approved still and copy it to outputs/<shot>/still_v<version>.<ext>.

    Free (no provider call, no reservation). Fails closed (non-retryable) when the file is missing,
    not an image file type, too large, or does not match the declared sha256.
    Returns {"local_path", "sha256", "source_path", "approved_by", "source"}."""
    src = resolve_pre_approved_path(episode_id, str(spec.get("path") or ""))
    if not src.is_file():
        raise ApplicationError(f"pre-approved still for {shot_id} not found: {src}",
                               type="PreApprovedStillError", non_retryable=True)
    ext = src.suffix.lower()
    if ext not in STILL_EXTENSIONS:
        raise ApplicationError(f"pre-approved still for {shot_id} must be one of {STILL_EXTENSIONS}: {src.name}",
                               type="PreApprovedStillError", non_retryable=True)
    size = src.stat().st_size
    if size == 0 or size > MAX_STILL_BYTES:
        raise ApplicationError(f"pre-approved still for {shot_id} has an invalid size ({size} bytes)",
                               type="PreApprovedStillError", non_retryable=True)
    sha = _sha256(src)
    want = spec.get("sha256")
    if want and want.lower() != sha:
        raise ApplicationError(
            f"pre-approved still for {shot_id} sha256 mismatch: expected {want}, got {sha} ({src})",
            type="PreApprovedStillError", non_retryable=True)
    dest_dir = outputs_dir(episode_id, shot_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"still_v{int(version)}{'.jpg' if ext == '.jpeg' else ext}").resolve()
    if dest != src:
        shutil.copyfile(src, dest)
    if _sha256(dest) != sha:
        raise ApplicationError(f"pre-approved still for {shot_id}: copy to {dest} is corrupt",
                               type="PreApprovedStillError", non_retryable=True)
    activity.logger.info(f"Pre-approved still for {episode_id}/{shot_id}: {src} -> {dest} (no still spend)")
    return {"local_path": str(dest), "sha256": sha, "source_path": str(src),
            "approved_by": spec.get("approved_by") or "owner", "source": spec.get("source") or "shots.json"}

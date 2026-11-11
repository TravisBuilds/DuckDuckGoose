"""Filesystem roots for episode inputs and generated outputs, resolved from the environment.

The hosted worker and API run with different working directories, so nothing may resolve a
relative ``data/...`` path against the cwd: every episode, output and dry-run path goes through
these helpers.

- ``data_dir()``: ``DATA_DIR``, else ``./data`` (local dev).
- ``episodes_dir()``: ``EPISODES_DIR``, else ``<data_dir()>/episodes``.
- ``episode_dir(ep)``: an episode's inputs (BEATMAP.md, shots.json, refs.json, prompt_kit.json, ...).
- ``outputs_dir(ep, shot)``: generated assets, ``<data_dir()>/episodes/<ep>/outputs/<shot>``, the
  directory the API serves stills and clips from.
- ``dry_run_outputs_dir()``: dry-run placeholders and scratch refs, ``<data_dir()>/dry_run_outputs``.

Values are read on every call (tests and the CLI set the environment after import). Only
activities, the API and the CLI may call these: workflow code must stay free of filesystem access.
"""

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def data_dir() -> Path:
    return _env_path("DATA_DIR") or Path("data")


def episodes_dir() -> Path:
    return _env_path("EPISODES_DIR") or data_dir() / "episodes"


def episode_dir(episode_id: str) -> Path:
    return episodes_dir() / episode_id


def outputs_dir(episode_id: str, shot_id: str | None = None) -> Path:
    root = data_dir() / "episodes" / episode_id / "outputs"
    return root / shot_id if shot_id else root


def dry_run_outputs_dir() -> Path:
    return data_dir() / "dry_run_outputs"

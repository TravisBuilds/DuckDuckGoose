"""Shared helpers for the automated QC judge tests (docs/QC-JUDGES.md): a live-mode budget ledger in
the test's tmp dir, the judge registries reset, plain generated images and the ledger rows of the
qc_judges line. Every judge call in these tests goes to FakeJudgeClient: none makes a network call or
pays for one. Images are flat shapes on a plain background (no real people, no named IP)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

JUDGE_ENV = (
    "HFVG_QC_JUDGES_ENABLED", "HFVG_QC_JUDGE_MODEL", "HFVG_QC_JUDGE_TIMEOUT_S", "HFVG_QC_JUDGE_RETRY_AFTER_S",
    "HFVG_QC_LINE_CAP_USD", "HFVG_QC_JUDGE_USD_PER_MTOK_IN", "HFVG_QC_JUDGE_USD_PER_MTOK_OUT",
    "HFVG_QC_JUDGE_IMAGE_TOKENS", "HFVG_QC_JUDGE_MAX_OUTPUT_TOKENS", "HFVG_QC_JUDGE_TEMPERATURE",
    "HFVG_QC_REVIEW_DEADLINE_S", "OPENAI_API_KEY", "EPISODE_CAP_USD", "EPISODE_STOP_PERCENT", "GC01_HEADROOM_USD",
)


def reset_registries() -> None:
    """No still reviewer, counter, clip checker or identity judge registered (the flag-off state)."""
    from hfvg.qc.animal_count import set_animal_counter
    from hfvg.qc.clip_constraints import set_constraint_checker
    from hfvg.qc.identity import set_identity_judge
    from hfvg.qc.still_review import set_still_reviewer

    set_animal_counter(None)
    set_constraint_checker(None)
    set_identity_judge(None)
    set_still_reviewer(None)


async def live_ledger(tmp_path: Path, monkeypatch) -> str:
    """DRY_RUN off, every judge setting at its default, a fresh studio DB with the budget tables."""
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import init_studio_db

    db = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("DRY_RUN", "false")
    for name in JUDGE_ENV:
        monkeypatch.delenv(name, raising=False)
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    return db


def image(path: Path, color: tuple[int, int, int] = (90, 140, 200), size: tuple[int, int] = (640, 960),
          seed: int = 0) -> Path:
    """A placeholder figure (a head and a body as plain shapes) saved at ``path``; ``seed`` changes
    the bytes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (235, 235, 235))
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse([w * 0.35, h * 0.08, w * 0.65, h * 0.28], fill=color)
    draw.rectangle([w * 0.3, h * 0.3, w * 0.7, h * 0.85], fill=color)
    draw.rectangle([0, 0, 4 + seed % 50, 4], fill=((seed * 37) % 256, (seed * 11) % 256, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def qc_line(db: str, episode_id: str) -> dict | None:
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT provider, cap_usd_micros, stop_usd_micros, spent_usd_micros, reserved_usd_micros "
                        "FROM budget_lines WHERE line_id = ?", (f"{episode_id}:qc_judges",)).fetchone()
    if row is None:
        return None
    return dict(zip(("provider", "cap", "stop", "spent", "reserved"), row))


def qc_transactions(db: str, episode_id: str) -> list[tuple[str, int, str]]:
    """(txn_type, amount, reason) of the episode's qc_judges line, oldest first."""
    with sqlite3.connect(db) as c:
        return c.execute("SELECT txn_type, amount, reason FROM budget_transactions WHERE line_id = ? "
                         "ORDER BY txn_id", (f"{episode_id}:qc_judges",)).fetchall()


def verdict_rows(db: str) -> list[tuple]:
    """(kind, answer, error, usd_micros, cost_source) of every cached judge verdict."""
    with sqlite3.connect(db) as c:
        try:
            return c.execute("SELECT kind, answer, error, usd_micros, cost_source FROM qc_judge_verdicts "
                             "ORDER BY created_at").fetchall()
        except sqlite3.OperationalError:
            return []

"""Shared helpers for the automated QC judge tests (docs/QC-JUDGES.md): a live-mode budget ledger in
the test's tmp dir, the judge registries reset, plain generated images and the ledger rows of the
qc_judges line. Every judge call in these tests goes to FakeJudgeClient: none makes a network call or
pays for one. Images are flat shapes on a plain background (no real people, no named IP)."""

from __future__ import annotations

import json
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


async def live_episode(tmp_path: Path, monkeypatch, episode_id: str) -> str:
    """live_ledger plus the episode row in live mode with G1.08 approved (review_still / review_clip
    then reach their judges) and a private cast store in the tmp dir."""
    import aiosqlite

    from hfvg.studio_db import create_episode

    db = await live_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    await create_episode(db, episode_id)
    async with aiosqlite.connect(db) as conn:
        await conn.execute("UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?", (episode_id,))
        await conn.commit()
    return db


def locked_character(episode_id: str, name: str = "Pip", seed: int = 100,
                     description: str = "a small round robot with a teal shell and one yellow antenna") -> dict:
    """A character of the episode's cast library with a complete, locked sheet; returns the shot_meta
    cast entry the shot planner writes for it."""
    from hfvg import cast_store
    from tests.cast_helpers import complete_sheet, new_character

    character = new_character(("episode", episode_id), name=name, description=description)
    complete_sheet(character, 1, seed=seed)
    cast_store.approve_version(character["character_id"], 1, "tester")
    character = cast_store.get_character(character["character_id"])
    version = next(v for v in character["versions"] if v["version"] == 1)
    return {"character": character["slug"], "version": 1, "name": character["name"],
            "clause": cast_store.version_clause(character, version), "locked": True}


STILL_PASS = {"verdict": "pass", "confidence": 0.93, "issues": []}
STILL_FAIL = {"verdict": "fail", "confidence": 0.9, "issues": ["two robots in frame, the prompt has one"]}


def detect_answer(*slugs: str, found: bool = True, confidence: float = 0.9,
                  box: dict | None = None) -> dict:
    """The detector's answer for the given characters (fractions of the image size)."""
    box = box or {"x": 0.3, "y": 0.1, "w": 0.4, "h": 0.8}
    head = {"x": box["x"] + box["w"] * 0.15, "y": box["y"], "w": box["w"] * 0.7, "h": box["h"] * 0.25}
    return {"characters": {s: {"found": found, "confidence": confidence, "box": box if found else None,
                               "head": head if found else None} for s in slugs}}


def identity_answer(scores: dict | None = None, verdict: str = "PASS", confidence: float = 0.9,
                    signs: tuple[str, ...] | None = None, **extra) -> dict:
    """The identity judge's answer: every sign of the generic profile at 0 unless ``scores`` says."""
    from hfvg.qc.identity import GENERIC_PROFILE

    signs = signs or tuple(GENERIC_PROFILE.signs)
    return {"signs": {s: {"score": (scores or {}).get(s, 0), "confidence": confidence, "evidence": "as on the sheet"}
                      for s in signs},
            "verdict": verdict, "reason": "compared with the sheet", "confidence": confidence, **extra}


def judge_of(system: str) -> str:
    """Which judge a request is for (by its instructions)."""
    from hfvg.qc import identity, still_review, vision_constraints, vision_counter

    if system == still_review.SYSTEM:
        return "still"
    if system == identity.DETECT_SYSTEM:
        return "detect"
    if system.startswith("You check that a character"):
        return "identity"
    if system == vision_counter.SYSTEM:
        return "headcount"
    if system == vision_constraints.SYSTEM:
        return "fail_if"
    raise AssertionError(f"unknown judge request: {system[:60]!r}")


def routed_client(**answers):
    """A FakeJudgeClient whose answers are per judge (still, detect, identity, headcount, fail_if),
    each a list consumed in order; ``client.calls[i]["judge"]`` says which judge was asked."""
    from hfvg.qc.judges import FakeJudgeClient, JudgeReply

    class RoutedJudgeClient(FakeJudgeClient):
        def __init__(self):
            super().__init__()
            self.queues = {k: list(v) if isinstance(v, list) else [v] for k, v in answers.items()}

        async def complete(self, *, model, system, text, images, max_output_tokens):
            judge = judge_of(system)
            self.calls.append({"judge": judge, "model": model, "system": system, "text": text,
                               "images": list(images), "max_output_tokens": max_output_tokens})
            queue = self.queues.get(judge) or []
            if not queue:
                raise RuntimeError(f"no {judge} answer left")
            answer = queue.pop(0)
            if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
                raise answer
            return JudgeReply(answer if isinstance(answer, str) else json.dumps(answer), 1000, 100)

        def asked(self) -> list[str]:
            return [c["judge"] for c in self.calls]

    return RoutedJudgeClient()


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

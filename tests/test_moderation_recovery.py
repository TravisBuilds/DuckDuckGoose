"""Tests for moderation recovery ladder (G-REC-1)."""

import os
import pytest

from temporalio import activity
from temporalio.testing import ActivityEnvironment

from hfvg.activities.moderation_recovery import (
    attempt_moderation_recovery,
    sanitize_prompt,
    get_fallback_model,
)
from hfvg.budget import BudgetLedger, RoundBudgetError


def test_sanitize_prompt_removes_trigger_words():
    """Test that sanitize_prompt removes common trigger words."""
    # Test removing trigger words
    prompt = "A bare moose standing in wet mud"
    sanitized, removed = sanitize_prompt(prompt)
    
    assert "bare" not in sanitized.lower()
    assert "wet" not in sanitized.lower()
    assert "mud" not in sanitized.lower()
    assert "moose" in sanitized.lower()
    assert len(removed) == 3
    assert "bare" in removed
    assert "wet" in removed
    assert "mud" in removed


def test_sanitize_prompt_preserves_clean_text():
    """Test that sanitize_prompt preserves clean prompts."""
    prompt = "A moose standing in a forest"
    sanitized, removed = sanitize_prompt(prompt)
    
    assert sanitized == prompt
    assert len(removed) == 0


def test_sanitize_prompt_case_insensitive():
    """Test that sanitize_prompt is case-insensitive."""
    prompt = "BARE moose in WET environment"
    sanitized, removed = sanitize_prompt(prompt)
    
    assert "bare" not in sanitized.lower()
    assert "wet" not in sanitized.lower()
    assert len(removed) == 2


def test_get_fallback_model_seedance_to_kling():
    """Test that Seedance falls back to Kling."""
    assert get_fallback_model("seedance-2.5") == "kling-3.0-pro"
    assert get_fallback_model(
        "bytedance/seedance-2.5/image-to-video"
    ) == "kling-video/v3.0/pro/image-to-video"
    assert get_fallback_model("seedance") == "kling-3.0-pro"


def test_get_fallback_model_kling_has_none():
    """Test that Kling has no fallback (it's already the fallback)."""
    assert get_fallback_model("kling-3.0-pro") is None
    assert get_fallback_model("kling-video/v3.0/pro/image-to-video") is None
    assert get_fallback_model("kling") is None


@pytest.mark.asyncio
async def test_recovery_sanitize_success(tmp_path, monkeypatch):
    """Test recovery succeeds with sanitization step."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    # Initialize DB and ledger
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    ledger = BudgetLedger(str(db_path))
    await ledger.init_db()
    
    # Initialize episode budget
    await ledger.init_episode_budget("ep01")
    
    # Test prompt with trigger words
    original_prompt = "A bare moose in wet mud"
    
    # Run recovery activity
    env = ActivityEnvironment()
    result = await env.run(
        attempt_moderation_recovery,
        episode_id="ep01",
        shot_id="A01",
        original_prompt=original_prompt,
        original_model="seedance",
        provider_message="Content blocked: NSFW",
        round_id="G3_CLIPS",
        duration=5.0,
        dry_run=True,
    )
    
    assert result["success"] is True
    assert result["step"] == "sanitize"
    assert result["sanitized_prompt"] is not None
    assert "bare" not in result["sanitized_prompt"].lower()
    assert "wet" not in result["sanitized_prompt"].lower()
    assert "mud" not in result["sanitized_prompt"].lower()
    assert "moose" in result["sanitized_prompt"].lower()
    assert result["escalate"] is False
    assert len(result["recovery_log"]) >= 2


@pytest.mark.asyncio
async def test_recovery_fallback_when_no_trigger_words(tmp_path, monkeypatch):
    """Test recovery tries fallback when sanitization doesn't help."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    # Initialize DB and ledger
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    ledger = BudgetLedger(str(db_path))
    await ledger.init_db()
    
    # Initialize episode budget
    await ledger.init_episode_budget("ep01")
    
    # Test prompt without trigger words
    original_prompt = "A moose standing in a forest"
    
    # Run recovery activity
    env = ActivityEnvironment()
    result = await env.run(
        attempt_moderation_recovery,
        episode_id="ep01",
        shot_id="A01",
        original_prompt=original_prompt,
        original_model="seedance",
        provider_message="Content blocked: NSFW",
        round_id="G3_CLIPS",
        duration=5.0,
        dry_run=True,
    )
    
    assert result["success"] is True
    assert result["step"] == "fallback"
    assert result["fallback_model"] == "kling-3.0-pro"
    assert result["escalate"] is False
    assert len(result["recovery_log"]) >= 2


@pytest.mark.asyncio
async def test_recovery_escalates_when_budget_exhausted(tmp_path, monkeypatch):
    """Test recovery escalates when budget is exhausted."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    # Initialize DB and ledger with very low budget
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    ledger = BudgetLedger(str(db_path))
    await ledger.init_db()
    
    # Initialize episode budget
    await ledger.init_episode_budget("ep01")
    
    # Set very low round cap
    monkeypatch.setenv("ROUND_CAP_USD", "0.00001")
    
    original_prompt = "A bare moose in wet mud"
    
    # Run recovery activity
    env = ActivityEnvironment()
    result = await env.run(
        attempt_moderation_recovery,
        episode_id="ep01",
        shot_id="A01",
        original_prompt=original_prompt,
        original_model="seedance",
        provider_message="Content blocked: NSFW",
        round_id="G3_CLIPS",
        duration=5.0,
        dry_run=True,
    )
    
    # Should escalate because budget check fails
    assert result["success"] is False
    assert result["step"] is None
    assert result["escalate"] is True
    assert "recovery exhausted" in result["recovery_log"][-1].lower() or "budget" in result["recovery_log"][-1].lower()


@pytest.mark.asyncio
async def test_recovery_logs_audit_trail(tmp_path, monkeypatch):
    """Test that recovery logs audit trail to database."""
    import aiosqlite
    
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    # Initialize DB and ledger
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    ledger = BudgetLedger(str(db_path))
    await ledger.init_db()
    
    # Initialize episode budget
    await ledger.init_episode_budget("ep01")
    
    original_prompt = "A bare moose in wet mud"
    
    # Run recovery activity
    env = ActivityEnvironment()
    result = await env.run(
        attempt_moderation_recovery,
        episode_id="ep01",
        shot_id="A01",
        original_prompt=original_prompt,
        original_model="seedance",
        provider_message="Content blocked: NSFW",
        round_id="G3_CLIPS",
        duration=5.0,
        dry_run=True,
    )
    
    # Check audit log
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT action, details FROM audit_log WHERE episode_id = ? ORDER BY created_at",
            ("ep01",)
        ) as cursor:
            rows = await cursor.fetchall()
    
    # Should have at least one recovery entry
    actions = [row[0] for row in rows]
    assert "moderation_recovery_sanitize" in actions or "moderation_recovery_fallback" in actions

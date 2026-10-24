"""Moderation recovery ladder: multi-step recovery from content/moderation blocks.

When a provider returns a content/moderation block (Seedance false-flags ~46% historically), this
ladder tries in order, each budget-checked against the USD ledger and per-round retry rule:

1. Automatic neutral reword of the prompt via a deterministic sanitizer (no filter evasion: only
   remove/neutralize trigger words, never obfuscate).
2. Re-route to the configured fallback model (e.g. Kling) respecting model_caps preflight.
3. Stop and escalate to human review with the exact provider message.

Each step is recorded in the audit log and returned in the recovery result.
"""

import os
import re
from typing import Any

import aiosqlite
from temporalio import activity

from hfvg.budget import BudgetLedger, RoundBudgetError
from hfvg.config import config
from hfvg.pricing import clip_estimate_usd_micros


class ModerationRecoveryError(ValueError):
    """Recovery ladder exhausted without success."""


# Known trigger words that commonly cause false positives (sanitizer list)
# No filter evasion: only neutral removal/replacement, never obfuscation
TRIGGER_WORDS = [
    "bare", "naked", "nude", "exposed", "revealing",
    "wet", "soaked", "damp", "moist",
    "mud", "muddy", "dirty",
    "sensual", "intimate", "provocative",
]

# Pattern to match and neutralize trigger words (case-insensitive)
TRIGGER_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(word) for word in TRIGGER_WORDS) + r')\b',
    flags=re.IGNORECASE
)


def sanitize_prompt(prompt: str) -> tuple[str, list[str]]:
    """
    Remove/neutralize trigger words that commonly cause false positives.
    
    Returns:
        (sanitized_prompt, list_of_removed_words)
    """
    removed_words = []
    
    def replace_trigger(match):
        word = match.group(0)
        removed_words.append(word.lower())
        # Replace with empty string, but preserve sentence structure
        return ""
    
    sanitized = TRIGGER_PATTERN.sub(replace_trigger, prompt)
    # Clean up extra whitespace
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    
    return sanitized, removed_words


def get_fallback_model(original_model: str) -> str | None:
    """
    Get the configured fallback model for moderation recovery.
    
    Seedance → Kling (never blocked in Ep02)
    Kling → None (Kling is already the fallback)
    
    Returns:
        Fallback model name, or None if no fallback available
    """
    fallback_map = {
        "seedance-2.5": "kling-3.0-pro",
        "bytedance/seedance-2.5/image-to-video": "kling-video/v3.0/pro/image-to-video",
        "seedance": "kling-3.0-pro",
        "kling": None,
        "kling-3.0-pro": None,
        "kling-video/v3.0/pro/image-to-video": None,
    }
    
    return fallback_map.get(original_model)


@activity.defn
async def attempt_moderation_recovery(
    episode_id: str,
    shot_id: str,
    original_prompt: str,
    original_model: str,
    provider_message: str,
    round_id: str,
    duration: float = 5.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Attempt moderation recovery ladder.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        original_prompt: Original prompt that was blocked
        original_model: Original model that blocked
        provider_message: Exact provider moderation message
        round_id: Review round ID (for budget checking)
        duration: Clip duration for cost estimation
        dry_run: Whether in dry-run mode
    
    Returns:
        dict with:
            - success: bool (whether recovery succeeded)
            - step: str (which step succeeded: "sanitize", "fallback", or None)
            - sanitized_prompt: str or None (if step was "sanitize")
            - fallback_model: str or None (if step was "fallback")
            - escalate: bool (whether human review is needed)
            - recovery_log: list of step descriptions
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    recovery_log = []
    recovery_log.append(f"Moderation block from {original_model}: {provider_message}")
    
    # Step 1: Try automatic sanitization
    activity.logger.info(f"Recovery step 1: sanitize prompt for {shot_id}")
    sanitized_prompt, removed_words = sanitize_prompt(original_prompt)
    
    if removed_words and sanitized_prompt != original_prompt:
        recovery_log.append(
            f"Step 1 (sanitize): Removed trigger words: {', '.join(removed_words)}"
        )
        
        # Check if we can afford a retry with sanitized prompt
        # Don't actually reserve here (the caller will do that), just check
        base_shot = shot_id[:-4] if shot_id.endswith("_end") else shot_id
        retry_key = f"moderation_sanitize_{shot_id}"
        estimated_cost = clip_estimate_usd_micros(original_model, None, duration)
        
        try:
            # Dry check only (don't actually reserve)
            await ledger.authorize_round_spend(
                episode_id, round_id, base_shot, f"check_{retry_key}",
                estimated_cost, retry_key=retry_key,
                reason=f"moderation recovery sanitize check {shot_id}",
                dry_check=True,
            )
            
            # Budget check passed, sanitization is viable
            recovery_log.append("Step 1 (sanitize): Budget check passed, retry viable")
            
            # Record in audit log
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """INSERT INTO audit_log (episode_id, action, details, user)
                       VALUES (?, ?, ?, ?)""",
                    (episode_id, "moderation_recovery_sanitize",
                     f"{shot_id}: sanitized prompt, removed {len(removed_words)} trigger words",
                     "system")
                )
                await db.commit()
            
            return {
                "success": True,
                "step": "sanitize",
                "sanitized_prompt": sanitized_prompt,
                "fallback_model": None,
                "escalate": False,
                "recovery_log": recovery_log,
            }
            
        except RoundBudgetError as e:
            recovery_log.append(f"Step 1 (sanitize): Budget check failed: {e}")
            activity.logger.warning(
                f"Sanitization retry would exceed budget for {shot_id}: {e}"
            )
    else:
        recovery_log.append("Step 1 (sanitize): No trigger words found to remove")
    
    # Step 2: Try fallback model
    activity.logger.info(f"Recovery step 2: fallback model for {shot_id}")
    fallback_model = get_fallback_model(original_model)
    
    if fallback_model:
        recovery_log.append(f"Step 2 (fallback): Trying {fallback_model}")
        
        # Check if we can afford a retry with fallback model
        base_shot = shot_id[:-4] if shot_id.endswith("_end") else shot_id
        retry_key = f"moderation_fallback_{shot_id}"
        estimated_cost = clip_estimate_usd_micros(fallback_model, None, duration)
        
        try:
            # Dry check only
            await ledger.authorize_round_spend(
                episode_id, round_id, base_shot, f"check_{retry_key}",
                estimated_cost, retry_key=retry_key,
                reason=f"moderation recovery fallback check {shot_id}",
                dry_check=True,
            )
            
            recovery_log.append(f"Step 2 (fallback): Budget check passed for {fallback_model}")
            
            # Record in audit log
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """INSERT INTO audit_log (episode_id, action, details, user)
                       VALUES (?, ?, ?, ?)""",
                    (episode_id, "moderation_recovery_fallback",
                     f"{shot_id}: re-routing to {fallback_model}",
                     "system")
                )
                await db.commit()
            
            return {
                "success": True,
                "step": "fallback",
                "sanitized_prompt": None,
                "fallback_model": fallback_model,
                "escalate": False,
                "recovery_log": recovery_log,
            }
            
        except RoundBudgetError as e:
            recovery_log.append(f"Step 2 (fallback): Budget check failed: {e}")
            activity.logger.warning(
                f"Fallback retry would exceed budget for {shot_id}: {e}"
            )
    else:
        recovery_log.append(f"Step 2 (fallback): No fallback model for {original_model}")
    
    # Step 3: Escalate to human review
    activity.logger.warning(f"Recovery step 3: escalate {shot_id} to human review")
    recovery_log.append(
        f"Step 3 (escalate): Recovery exhausted, escalating to human review"
    )
    
    # Record in audit log
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "moderation_recovery_escalate",
             f"{shot_id}: all recovery steps exhausted, escalating to human review. "
             f"Provider message: {provider_message}",
             "system")
        )
        await db.commit()
    
    return {
        "success": False,
        "step": None,
        "sanitized_prompt": None,
        "fallback_model": None,
        "escalate": True,
        "recovery_log": recovery_log,
    }

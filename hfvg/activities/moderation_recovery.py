"""Moderation recovery ladder (G-REC-1): bounded, budget-checked recovery from a provider CONTENT BLOCK.

Only a content block (provider status ``nsfw``/``blocked`` -> ``failure_class == "content_block"``)
enters this ladder. ``canceled`` and ``failed`` are terminal provider failures and never do.

The ladder is decided ONE rung at a time by :func:`attempt_moderation_recovery` (an activity, so
the decision is recorded in workflow history and replays deterministically):

1. **Neutral reword** (``reword``): a deterministic, whole-word, case-insensitive pass that only
   REMOVES (or swaps for a plain neutral word) the benign descriptors listed in :data:`NEUTRALIZE`
   that are known to trip provider filters on harmless scenes (a duck in rain is "wet"). It never
   adds spelling tricks, euphemisms, codes or new content: every output word is either an input
   word or one of the plain replacement words in :data:`NEUTRALIZE` (tested property). Prompts
   containing a :data:`HARD_STOP` term are NEVER rewritten - rewording those would be rephrasing
   prohibited content to get past the filter (docs/CONTENT-POLICY.md "Filter Evasion") - the
   ladder escalates straight to a human. If no listed word is present, the rung is skipped (no
   paid resubmit of an identical prompt).
2. **Fallback model** (``fallback``, clips only): re-route to the configured fallback
   (``MODERATION_FALLBACK_MODELS``, default ``seedance:kling``), validated with the routing
   planner (tags/duration/multi-animal rules) and the model_caps preflight for the clip phase.
   Any routing/preflight error refuses the rung (audited) and the ladder escalates.
3. **Escalate** (``escalate``): stop; the shot is flagged ``needs_review`` in the shots table with
   the provider's EXACT message, and the decision carries it back to the workflow/API.

Every paid rung is pre-checked against the Round-1 USD round ledger with a dry
``authorize_round_spend`` (round 80% stop, episode 80% stop, max paid retries per shot per round,
retry key ``m{n}``); the real hold is taken by the submit activity right before the provider call
(``moderation_retry`` arg). A workflow-side count of rungs already used enforces the same per-shot
limit in dry runs (where no holds are written). Every decision is written to ``audit_log``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import aiosqlite
from temporalio import activity

from hfvg.budget import BudgetLedger, RoundBudgetError, get_budget_settings
from hfvg.pricing import clip_estimate_usd_micros

ROUND_FOR_KIND = {"still": "G2_STILLS", "clip": "G3_CLIPS"}

# Benign descriptors that trip provider filters on harmless scenes. Value "" = remove the word;
# otherwise the plain neutral word that replaces it. Keep this list SHORT, documented and boring:
# nothing here may be a euphemism or obfuscation of prohibited content.
NEUTRALIZE: dict[str, str] = {
    # water / weather (rain, ponds, baths: the most common duck false positives)
    "wet": "", "soaked": "", "soaking": "", "drenched": "", "dripping": "",
    "damp": "", "moist": "", "sweaty": "", "steamy": "misty",
    # body-ish descriptors with harmless meanings (bare feet, exposed rock, a revealing angle)
    "bare": "", "barefoot": "", "exposed": "", "revealing": "", "skin-tight": "",
    # violence-ish words with harmless meanings (a film shoot, a "killer" view)
    "bloody": "", "gory": "", "killer": "", "shoot": "film", "shooting": "filming",
}

# Terms the ladder NEVER rewrites: their presence means a human must look at the prompt.
HARD_STOP: frozenset[str] = frozenset({
    "nude", "naked", "nudity", "topless", "undressed", "sexual", "sexy", "erotic", "sensual",
    "seductive", "provocative", "lingerie", "explicit", "intimate", "fetish",
})

REPLACEMENT_VOCAB: frozenset[str] = frozenset(v for v in NEUTRALIZE.values() if v)

_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")

DEFAULT_FALLBACK_MODELS = "seedance:kling"


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def hard_stop_terms(prompt: str) -> list[str]:
    """HARD_STOP terms present in the prompt (whole words, case-insensitive), sorted."""
    return sorted({w for w in _words(prompt) if w in HARD_STOP})


def sanitize_prompt(prompt: str) -> tuple[str, list[dict[str, str]]]:
    """Deterministic neutral reword. Returns ``(text, changes)``; ``changes`` lists
    ``{"word": original, "replacement": neutral-or-""}`` in order. Only whole words in
    :data:`NEUTRALIZE` are touched; everything else (punctuation, casing) is kept."""
    changes: list[dict[str, str]] = []

    def _sub(m: re.Match) -> str:
        word = m.group(0)
        key = word.lower()
        if key not in NEUTRALIZE:
            return word
        repl = NEUTRALIZE[key]
        changes.append({"word": word, "replacement": repl})
        return repl

    text = _WORD.sub(_sub, prompt or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*(?:and|or)\s*(?=[,.;!?])", "", text)  # "x, and, y" -> "x, y"
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])\1+", r"\1", text)
    text = re.sub(r"^[\s,;:]+", "", text)
    text = re.sub(r"[,;:]+(?=[.!?]|$)", "", text)
    return text.strip(), changes


def fallback_map() -> dict[str, str]:
    """``MODERATION_FALLBACK_MODELS`` = ``"from:to,from:to"`` (routing aliases, e.g. seedance:kling)."""
    raw = os.getenv("MODERATION_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            src, dst = (p.strip() for p in pair.split(":", 1))
            if src and dst and src != dst:
                out[src] = dst
    return out


def get_fallback_model(kind: str, model: str | None) -> str | None:
    """Fallback routing alias for a blocked clip model, or None (stills have no fallback)."""
    if kind != "clip":
        return None
    return fallback_map().get(model or "kling")


def validate_fallback(request: dict[str, Any], fallback: str) -> list[str]:
    """Routing plan + model_caps preflight (clip phase) for the fallback model. [] = OK."""
    from hfvg import model_caps, preflight
    from hfvg.routing import RoutingError, plan_route

    errors: list[str] = []
    try:
        plan_route({
            "model": fallback,
            "tier": "draft" if request.get("tier") == "draft" else "final",
            "duration": request.get("duration", 5.0),
            "tags": request.get("tags") or [],
            "count": int(request.get("count") or 1),
        })
    except RoutingError as e:
        errors.append(f"routing: {e}")
        return errors
    try:
        violations = preflight.preflight_shot(
            {"id": request["shot_id"], "clip_prompt": request.get("prompt") or "",
             "duration": request.get("duration", 5.0),
             "start_frame": request.get("start_image_url") or None,
             "end_frame": request.get("end_image_url") or None},
            {"clip_model": preflight.clip_model_path(fallback)},
            model_caps.get_registry(), phase="clip",
        )
    except Exception as e:  # registry unreadable etc.: fail closed
        errors.append(f"model_caps: {e}")
        return errors
    errors.extend(preflight.format_violation(v) for v in violations if v.severity == "error")
    return errors


async def _audit(db_path: str, episode_id: str, action: str, details: dict[str, Any]) -> None:
    from hfvg.studio_db import init_studio_db
    await init_studio_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, action, json.dumps(details, sort_keys=True), "moderation_ladder"),
        )
        await db.commit()


async def _flag_needs_review(db_path: str, episode_id: str, shot_id: str, info: dict[str, Any]) -> None:
    """shots row -> status needs_review, qc_results.moderation = escalation info (merged)."""
    record_id = f"{episode_id}-{shot_id}"
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT qc_results FROM shots WHERE id = ?", (record_id,)) as cur:
            row = await cur.fetchone()
        qc: dict[str, Any] = {}
        if row and row[0]:
            try:
                qc = json.loads(row[0]) or {}
            except ValueError:
                qc = {}
        qc["moderation"] = info
        if row:
            await db.execute(
                "UPDATE shots SET status = 'needs_review', qc_results = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(qc), record_id))
        else:
            await db.execute(
                """INSERT INTO shots (id, episode_id, shot_id, status, qc_results, created_at, updated_at)
                   VALUES (?, ?, ?, 'needs_review', ?, datetime('now'), datetime('now'))""",
                (record_id, episode_id, shot_id, json.dumps(qc)))
        await db.commit()


async def _budget_check(request: dict[str, Any], n: int, amount: int) -> str | None:
    """Dry round-ledger check for paid moderation retry m{n}. Returns a refusal reason or None."""
    settings = get_budget_settings()
    used = int(request.get("paid_retries_used") or 0)
    if used >= settings.max_paid_retries_per_shot_per_round:
        return (f"shot {request['shot_id']} already used {used} paid moderation retr"
                f"{'y' if used == 1 else 'ies'} this round (max "
                f"{settings.max_paid_retries_per_shot_per_round} per shot per round)")
    if amount <= 0:
        return "no positive cost estimate for the paid retry"
    ledger = BudgetLedger(request["db_path"])
    base_shot = request["shot_id"][:-4] if request["shot_id"].endswith("_end") else request["shot_id"]
    try:
        await ledger.authorize_round_spend(
            request["episode_id"], ROUND_FOR_KIND[request["kind"]], base_shot,
            f"{request.get('blocked_job_id') or request['kind']}-m{n}", amount,
            retry_key=f"m{n}", reason="moderation retry pre-check", dry_check=True,
        )
    except RoundBudgetError as e:
        return str(e)
    return None


async def decide_next_step(request: dict[str, Any]) -> dict[str, Any]:
    """Pure ladder decision (no Temporal context needed). See module docstring for the request."""
    kind = request["kind"]
    if kind not in ROUND_FOR_KIND:
        raise ValueError(f"kind must be still or clip, got {kind!r}")
    episode_id, shot_id = request["episode_id"], request["shot_id"]
    db_path = request["db_path"]
    steps_done = list(request.get("steps_done") or [])
    provider_message = request.get("provider_message") or ""
    prompt = request.get("prompt") or ""
    model = request.get("model")
    n = int(request.get("next_n") or (len(steps_done) + 1))
    log: list[dict[str, Any]] = []
    base = {"shot_id": shot_id, "kind": kind, "blocked_job_id": request.get("blocked_job_id"),
            "provider_message": provider_message, "model": model}

    if not steps_done:
        entry = dict(base, action="moderation_block", failure_class="content_block")
        await _audit(db_path, episode_id, "moderation_block", entry)
        log.append(entry)

    async def escalate(reason: str) -> dict[str, Any]:
        info = dict(base, reason=reason, steps_done=steps_done, prompt=prompt)
        await _audit(db_path, episode_id, "moderation_escalated", info)
        await _flag_needs_review(db_path, episode_id, shot_id, info)
        log.append(dict(info, action="moderation_escalated"))
        return {"action": "escalate", "reason": reason, "provider_message": provider_message,
                "log": log, "prompt": prompt, "model": model}

    if not request.get("attempts_left", 1):
        return await escalate("no workflow attempts left for a moderation retry")

    terms = hard_stop_terms(prompt)
    if terms:
        return await escalate(f"prompt contains terms the ladder never rewrites: {terms}")

    # Rung 1: neutral reword
    if "reword" not in steps_done and "fallback" not in steps_done:
        new_prompt, changes = sanitize_prompt(prompt)
        if changes and new_prompt:
            refusal = await _budget_check(request, n, int(request.get("amount_usd_micros") or 0))
            if refusal:
                entry = dict(base, step="reword", reason=refusal)
                await _audit(db_path, episode_id, "moderation_budget_refused", entry)
                log.append(dict(entry, action="moderation_budget_refused"))
                return await escalate(f"paid reword retry refused by the round ledger: {refusal}")
            entry = dict(base, step="reword", n=n, changes=changes, prompt_before=prompt,
                         prompt_after=new_prompt)
            await _audit(db_path, episode_id, "moderation_reword", entry)
            log.append(dict(entry, action="moderation_reword"))
            return {"action": "reword", "prompt": new_prompt, "model": model, "changes": changes,
                    "retry": {"n": n, "step": "reword"}, "provider_message": provider_message,
                    "log": log}
        entry = dict(base, step="reword", reason="no listed trigger word in the prompt; rung skipped")
        await _audit(db_path, episode_id, "moderation_reword_skipped", entry)
        log.append(dict(entry, action="moderation_reword_skipped"))

    # Rung 2: fallback model (clips only)
    if "fallback" not in steps_done:
        fallback = get_fallback_model(kind, model)
        if fallback:
            errors = validate_fallback(request, fallback)
            if errors:
                entry = dict(base, step="fallback", fallback_model=fallback, errors=errors)
                await _audit(db_path, episode_id, "moderation_fallback_refused", entry)
                log.append(dict(entry, action="moderation_fallback_refused"))
                return await escalate(f"fallback {fallback} refused by routing/model_caps: {errors}")
            amount = clip_estimate_usd_micros(fallback, request.get("tier"), request.get("duration", 5.0))
            refusal = await _budget_check(request, n, amount)
            if refusal:
                entry = dict(base, step="fallback", fallback_model=fallback, reason=refusal)
                await _audit(db_path, episode_id, "moderation_budget_refused", entry)
                log.append(dict(entry, action="moderation_budget_refused"))
                return await escalate(f"paid fallback retry refused by the round ledger: {refusal}")
            entry = dict(base, step="fallback", n=n, fallback_model=fallback, estimate_usd_micros=amount)
            await _audit(db_path, episode_id, "moderation_fallback", entry)
            log.append(dict(entry, action="moderation_fallback"))
            return {"action": "fallback", "prompt": prompt, "model": fallback,
                    "retry": {"n": n, "step": "fallback"}, "provider_message": provider_message,
                    "log": log}

    return await escalate("recovery ladder exhausted")


@activity.defn
async def attempt_moderation_recovery(request: dict[str, Any]) -> dict[str, Any]:
    """Decide the next moderation-ladder rung for a content-blocked still/clip.

    request: episode_id, shot_id, kind ("still"|"clip"), prompt (the prompt that was blocked),
    model (clip routing alias), duration, tags, count, tier, start_image_url, end_image_url,
    provider_message (exact), blocked_job_id, amount_usd_micros (the blocked job's estimate),
    steps_done (rungs already taken), next_n, paid_retries_used, attempts_left.
    Returns {"action": "reword"|"fallback"|"escalate", "prompt", "model", "retry", "log", ...}.
    """
    request = dict(request)
    request.setdefault("db_path", os.getenv("DATABASE_PATH", "./data/studio.db"))
    decision = await decide_next_step(request)
    activity.logger.info(
        f"Moderation ladder {request['episode_id']}/{request['shot_id']} {request['kind']}: "
        f"{decision['action']} ({decision.get('reason', '')})")
    return decision

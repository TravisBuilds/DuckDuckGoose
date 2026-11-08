"""Story shaping activities using OpenAI Agents SDK.

Generates StoryShape from user idea with genre presets, then critiques and validates it.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from temporalio import activity

from hfvg.models import Act, StoryBeat, StoryShape, StoryShapeCritique
from hfvg.config import config


# Test stub for deterministic testing
_test_stub_story_shape: StoryShape | None = None


def _get_cache_db_path() -> Path:
    """Get the path to the story shape cache database."""
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "story_shape_cache.db"


def _init_cache_db() -> None:
    """Initialize the cache database with schema if needed."""
    db_path = _get_cache_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_shape_cache (
                episode_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                story_shape_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (episode_id, input_hash)
            )
        """)
        conn.commit()


def _get_cached_story_shape(episode_id: str, input_hash: str) -> dict | None:
    """Retrieve cached story shape from database."""
    db_path = _get_cache_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT story_shape_json FROM story_shape_cache WHERE episode_id = ? AND input_hash = ?",
                (episode_id, input_hash)
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except sqlite3.Error:
        pass
    return None


def _cache_story_shape(episode_id: str, input_hash: str, story_shape: dict) -> None:
    """Store story shape in cache database."""
    db_path = _get_cache_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO story_shape_cache 
                   (episode_id, input_hash, story_shape_json) VALUES (?, ?, ?)""",
                (episode_id, input_hash, json.dumps(story_shape))
            )
            conn.commit()
    except sqlite3.Error:
        pass


def set_test_stub_story_shape(story_shape: StoryShape | None) -> None:
    """Set a test stub for deterministic testing."""
    global _test_stub_story_shape
    _test_stub_story_shape = story_shape


def _compute_input_hash(idea: str, genre_preset: str | None, target_duration: float) -> str:
    """Compute deterministic hash of inputs for caching."""
    content = f"{idea}|{genre_preset or ''}|{target_duration}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _build_story_prompt(idea: str, genre_preset: str | None, target_duration: float) -> str:
    """Build the prompt for the story shaping agent."""
    genre_guidance = ""
    if genre_preset:
        presets = {
            "comedy": "Focus on comedic timing, setup/punchline structure, and character reactions.",
            "adventure": "Build tension through obstacles, discoveries, and escalating stakes.",
            "slice-of-life": "Emphasize relatable moments, small victories, and emotional resonance.",
            "mystery": "Create intrigue with clues, red herrings, and a satisfying reveal.",
            "action": "Structure around set pieces, rising danger, and a climactic confrontation.",
        }
        genre_guidance = presets.get(genre_preset.lower(), "")
    
    max_beat_s = config.STORY_SHAPING_MAX_BEAT_DURATION_S
    
    return f"""You are a story structure expert for short-form video content (~2 minutes).

Given this idea:
{idea}

{f"Genre guidance ({genre_preset}): {genre_guidance}" if genre_guidance else ""}

Create a story structure with:
1. A compelling logline (one sentence)
2. A hook that grabs attention in the first 5 seconds
3. A 3-act beat sheet with {int(target_duration)}s total duration:
   - Act 1 (setup): ~25% of time
   - Act 2 (conflict): ~50% of time
   - Act 3 (resolution): ~25% of time
4. A clear midpoint turn (usually end of Act 1 or middle of Act 2)
5. A payoff that explicitly references or completes the hook
6. Emotional arc per beat (tone/feeling)
7. Each beat should be 5-{max_beat_s}s (no longer than {max_beat_s}s)

Return JSON with this structure:
{{
  "logline": "one sentence story summary",
  "hook": "attention-grabbing opening moment",
  "hook_deadline_s": 5.0,
  "acts": [
    {{
      "act_number": 1,
      "beats": [
        {{
          "beat_id": "beat_1_1",
          "description": "what happens in this beat",
          "emotional_tone": "excitement|curiosity|tension|relief|joy|etc",
          "target_duration_s": 15.0,
          "shot_ids": []
        }}
      ]
    }}
  ],
  "midpoint_beat_id": "beat_2_1",
  "payoff": "callback or resolution that explicitly references the hook",
  "payoff_references_hook": true,
  "total_duration_s": {target_duration}
}}

IMPORTANT: The payoff MUST explicitly reference a key element from the hook (object, action, character, or setting). Make the connection clear."""


def _flatten_acts_to_beats(acts: list[dict]) -> list[StoryBeat]:
    """Flatten act structure to beats list."""
    beats = []
    for act in acts:
        for beat_data in act.get("beats", []):
            beats.append(StoryBeat(
                beat_id=beat_data["beat_id"],
                description=beat_data["description"],
                emotional_tone=beat_data["emotional_tone"],
                target_duration_s=beat_data["target_duration_s"],
                shot_ids=beat_data.get("shot_ids", []),
            ))
    return beats


def _check_payoff_hook_reference(hook: str, payoff: str) -> bool:
    """Check if payoff explicitly references the hook using keyword matching."""
    # Extract significant words (>3 chars, not common words)
    common_words = {"the", "and", "that", "this", "with", "from", "have", "they", "what", "will"}
    
    hook_words = set(
        word.lower() for word in hook.split() 
        if len(word) > 3 and word.lower() not in common_words
    )
    payoff_words = set(
        word.lower() for word in payoff.split()
        if len(word) > 3 and word.lower() not in common_words
    )
    
    # Check for shared significant words (minimum 1)
    common_significant = hook_words & payoff_words
    return len(common_significant) > 0


def _critique_story_shape(story: StoryShape) -> StoryShapeCritique:
    """Rule-based critique of a story shape."""
    issues = story.validate_structure()
    score = 100
    
    hook_present_early = story.hook_deadline_s <= 5.0
    if not hook_present_early:
        issues.append(f"Hook deadline ({story.hook_deadline_s}s) exceeds 5s")
        score -= 15
    
    # Check payoff-hook reference with actual keyword matching
    payoff_refs_hook = _check_payoff_hook_reference(story.hook, story.payoff)
    if not payoff_refs_hook:
        issues.append("Payoff doesn't explicitly reference the hook (no shared keywords)")
        score -= 20
    
    beat_durations = sum(beat.target_duration_s for beat in story.beats)
    durations_fit = abs(beat_durations - story.total_duration_s) <= 5.0
    if not durations_fit:
        score -= 20
    
    all_beats_have_shots = all(beat.shot_ids for beat in story.beats)
    
    # Dead beats: use configurable threshold
    max_beat_duration = config.STORY_SHAPING_MAX_BEAT_DURATION_S
    dead_beats = [b for b in story.beats if b.target_duration_s > max_beat_duration]
    no_dead_beats = not dead_beats
    if not no_dead_beats:
        score -= 10 * len(dead_beats)
    
    suggestions = []
    if not hook_present_early:
        suggestions.append("Move hook to first 5 seconds")
    if not payoff_refs_hook:
        suggestions.append("Add explicit reference to hook in payoff (shared keyword or motif)")
    if not durations_fit:
        suggestions.append("Adjust beat durations to sum to target")
    if dead_beats:
        suggestions.append(f"Split long beats (>{max_beat_duration}s): {', '.join(b.beat_id for b in dead_beats)}")
    if not all_beats_have_shots and not issues:
        suggestions.append("Shots will be assigned from beatmap after approval")
    
    passes = score >= 70 and not issues
    
    return StoryShapeCritique(
        score=max(0, score),
        issues=issues,
        suggestions=suggestions,
        hook_present_early=hook_present_early,
        payoff_references_hook=payoff_refs_hook,
        durations_fit=durations_fit,
        all_beats_have_shots=all_beats_have_shots,
        no_dead_beats=no_dead_beats,
        passes=passes,
    )


@activity.defn
async def generate_story_shape_activity(
    episode_id: str,
    idea: str,
    genre_preset: str | None = None,
    target_duration: float = 120.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Generate a StoryShape from user idea using OpenAI async client.
    
    Uses config.STORY_SHAPING_MODEL and config.OPENAI_API_KEY.
    Generation is idempotent: cached per episode_id and input hash.
    
    Args:
        episode_id: Episode identifier for caching
        idea: User's story idea
        genre_preset: Optional genre hint (comedy, adventure, slice-of-life, mystery, action)
        target_duration: Target episode duration in seconds (default 120s)
        dry_run: If True, use test stub (no paid API calls)
        
    Returns:
        dict with:
            - story_shape: StoryShape dict
            - dry_run: bool
            - model_used: str (if not dry run)
            - from_cache: bool (if retrieved from cache)
    """
    input_hash = _compute_input_hash(idea, genre_preset, target_duration)
    
    # Initialize cache DB if needed
    _init_cache_db()
    
    # Check cache first (idempotent generation)
    cached = _get_cached_story_shape(episode_id, input_hash)
    if cached is not None:
        return {
            "story_shape": cached,
            "dry_run": dry_run,
            "from_cache": True,
        }
    
    if dry_run or _test_stub_story_shape:
        if _test_stub_story_shape:
            story = _test_stub_story_shape
        else:
            story = StoryShape(
                logline="Test story logline",
                hook="An exciting opening moment",
                hook_deadline_s=5.0,
                acts=[
                    Act(
                        act_number=1,
                        description="Setup",
                        target_duration_s=30.0,
                        beat_ids=["beat_1"],
                    ),
                    Act(
                        act_number=2,
                        description="Confrontation",
                        target_duration_s=60.0,
                        beat_ids=["beat_2"],
                    ),
                    Act(
                        act_number=3,
                        description="Resolution",
                        target_duration_s=30.0,
                        beat_ids=["beat_3"],
                    ),
                ],
                beats=[
                    StoryBeat(
                        beat_id="beat_1",
                        description="Setup and introduction",
                        emotional_tone="curiosity",
                        target_duration_s=30.0,
                        shot_ids=[],
                    ),
                    StoryBeat(
                        beat_id="beat_2",
                        description="Conflict emerges",
                        emotional_tone="tension",
                        target_duration_s=60.0,
                        shot_ids=[],
                    ),
                    StoryBeat(
                        beat_id="beat_3",
                        description="Resolution and payoff",
                        emotional_tone="relief",
                        target_duration_s=30.0,
                        shot_ids=[],
                    ),
                ],
                midpoint_beat_id="beat_2",
                payoff="The exciting moment pays off with a satisfying conclusion",
                payoff_references_hook=True,
                total_duration_s=target_duration,
            )
        
        result = {
            "story_shape": story.model_dump(),
            "dry_run": True,
        }
        story_dict = story.model_dump()
        _cache_story_shape(episode_id, input_hash, story_dict)
        return result
    
    # Live mode: use OpenAI async client with config
    api_key = config.OPENAI_API_KEY
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY required for live story shaping. "
            "Run in dry mode or set the API key."
        )
    
    try:
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(api_key=api_key)
        prompt = _build_story_prompt(idea, genre_preset, target_duration)
        
        response = await client.chat.completions.create(
            model=config.STORY_SHAPING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a story structure expert. Return only valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        
        result_data = json.loads(response.choices[0].message.content)
        
        # Handle act structure or flat beats
        if "acts" in result_data:
            beats = _flatten_acts_to_beats(result_data["acts"])
        else:
            beats = [
                StoryBeat(
                    beat_id=beat["beat_id"],
                    description=beat["description"],
                    emotional_tone=beat["emotional_tone"],
                    target_duration_s=beat["target_duration_s"],
                    shot_ids=beat.get("shot_ids", []),
                )
                for beat in result_data["beats"]
            ]
        
        story = StoryShape(
            logline=result_data["logline"],
            hook=result_data["hook"],
            hook_deadline_s=result_data.get("hook_deadline_s", 5.0),
            beats=beats,
            midpoint_beat_id=result_data["midpoint_beat_id"],
            payoff=result_data["payoff"],
            payoff_references_hook=result_data.get("payoff_references_hook", False),
            total_duration_s=result_data.get("total_duration_s", target_duration),
        )
        
        result = {
            "story_shape": story.model_dump(),
            "dry_run": False,
            "model_used": config.STORY_SHAPING_MODEL,
        }
        story_dict = story.model_dump()
        _cache_story_shape(episode_id, input_hash, story_dict)
        return result
    
    except Exception as e:
        activity.logger.error(f"Story shaping failed: {e}")
        raise


@activity.defn
async def critique_story_shape_activity(story_shape_dict: dict) -> dict[str, Any]:
    """
    Critique a story shape with rule-based scoring.
    
    Args:
        story_shape_dict: StoryShape as dict
        
    Returns:
        dict with:
            - critique: StoryShapeCritique dict
            - needs_revision: bool
    """
    story = StoryShape(**story_shape_dict)
    critique = _critique_story_shape(story)
    
    return {
        "critique": critique.model_dump(),
        "needs_revision": not critique.passes,
    }


@activity.defn
async def revise_story_shape_activity(
    story_shape_dict: dict,
    critique_dict: dict,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Revise a story shape based on critique (automatic revision, max 1 attempt).
    
    Args:
        story_shape_dict: Original StoryShape as dict
        critique_dict: StoryShapeCritique as dict
        dry_run: If True, use deterministic fixes (no paid API calls)
        
    Returns:
        dict with:
            - story_shape: revised StoryShape dict
            - changes_made: list of changes
            - dry_run: bool
    """
    story = StoryShape(**story_shape_dict)
    critique = StoryShapeCritique(**critique_dict)
    
    if dry_run or _test_stub_story_shape:
        revised = story.model_copy(deep=True)
        changes = []
        
        # Fix duration mismatch
        if not critique.durations_fit:
            beat_durations = sum(b.target_duration_s for b in revised.beats)
            diff = story.total_duration_s - beat_durations
            if revised.beats and abs(diff) > 0.5:
                # Distribute difference across beats proportionally
                for beat in revised.beats:
                    adjustment = (beat.target_duration_s / beat_durations) * diff
                    beat.target_duration_s += adjustment
                changes.append(f"Adjusted beat durations by {diff:.1f}s total")
        
        # Fix payoff-hook reference (deterministic: extract first significant word from hook)
        if not critique.payoff_references_hook:
            hook_words = [w for w in story.hook.split() if len(w) > 3]
            if hook_words and hook_words[0].lower() not in revised.payoff.lower():
                revised.payoff = f"{revised.payoff} [{hook_words[0]}]"
                revised.payoff_references_hook = True
                changes.append(f"Added hook reference '{hook_words[0]}' to payoff")
        
        return {
            "story_shape": revised.model_dump(),
            "changes_made": changes or ["No automatic fixes applied"],
            "dry_run": True,
        }
    
    api_key = config.OPENAI_API_KEY
    if not api_key:
        raise ValueError("OPENAI_API_KEY required for live revision")
    
    try:
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(api_key=api_key)
        
        prompt = f"""Revise this story structure based on critique.

Original:
{json.dumps(story.model_dump(), indent=2)}

Critique issues:
{json.dumps(critique.issues, indent=2)}

Suggestions:
{json.dumps(critique.suggestions, indent=2)}

Return the revised structure as JSON with the same schema. Keep what works, fix the issues."""
        
        response = await client.chat.completions.create(
            model=config.STORY_SHAPING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a story structure expert. Return only valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        
        result_data = json.loads(response.choices[0].message.content)
        
        # Handle act structure or flat beats
        if "acts" in result_data:
            beats = _flatten_acts_to_beats(result_data["acts"])
        else:
            beats = [StoryBeat(**beat) for beat in result_data["beats"]]
        
        revised = StoryShape(
            logline=result_data["logline"],
            hook=result_data["hook"],
            hook_deadline_s=result_data.get("hook_deadline_s", 5.0),
            beats=beats,
            midpoint_beat_id=result_data["midpoint_beat_id"],
            payoff=result_data["payoff"],
            payoff_references_hook=result_data.get("payoff_references_hook", False),
            total_duration_s=result_data.get("total_duration_s", story.total_duration_s),
        )
        
        changes = ["Revised based on critique"]
        if critique.issues:
            changes.append(f"Addressed {len(critique.issues)} issues")
        
        return {
            "story_shape": revised.model_dump(),
            "changes_made": changes,
            "dry_run": False,
        }
    
    except Exception as e:
        activity.logger.error(f"Story revision failed: {e}")
        raise


@activity.defn
async def update_story_shape_activity(
    episode_id: str,
    story_shape_dict: dict,
) -> dict[str, Any]:
    """
    Update and re-critique a story shape after edits.
    
    Args:
        episode_id: Episode identifier
        story_shape_dict: Updated StoryShape as dict
        
    Returns:
        dict with:
            - story_shape: StoryShape dict (unchanged)
            - critique: new StoryShapeCritique dict
            - needs_revision: bool
    """
    story = StoryShape(**story_shape_dict)
    critique = _critique_story_shape(story)
    
    return {
        "story_shape": story.model_dump(),
        "critique": critique.model_dump(),
        "needs_revision": not critique.passes,
    }


@activity.defn
async def assign_shots_to_beats_activity(
    story_shape_dict: dict,
    shots: list[dict],
) -> dict[str, Any]:
    """
    Assign shots from beatmap to story beats based on timing, and enrich shots with beat context.
    
    Updates each shot with:
    - beat_id: Which story beat this shot belongs to
    - beat_purpose: The beat's description (narrative purpose within the story)
    - beat_emotional_tone: The beat's emotional arc
    
    Args:
        story_shape_dict: StoryShape as dict
        shots: Parsed beatmap shots with screen_time and purpose
        
    Returns:
        dict with:
            - story_shape: updated StoryShape dict with shot assignments
            - assignments: shot_id -> {beat_id, beat_purpose, beat_emotional_tone} mapping
            - enriched_shots: shots list with beat_id, beat_purpose, beat_emotional_tone added
    """
    story = StoryShape(**story_shape_dict)
    
    # Build beat lookup by id
    beats_by_id = {beat.beat_id: beat for beat in story.beats}
    
    assignments = {}
    beat_end_times = []
    current_time = 0.0
    
    for beat in story.beats:
        current_time += beat.target_duration_s
        beat_end_times.append((current_time, beat.beat_id))
    
    cumulative_shot_time = 0.0
    enriched_shots = []
    
    for shot in shots:
        shot_id = shot["shot_id"]
        shot_duration = shot.get("screen_time", 5.0)
        
        shot_start = cumulative_shot_time
        shot_end = cumulative_shot_time + shot_duration
        shot_mid = (shot_start + shot_end) / 2
        
        assigned_beat_id = None
        for beat_end, beat_id in beat_end_times:
            if shot_mid <= beat_end:
                assigned_beat_id = beat_id
                break
        
        if assigned_beat_id:
            beat = beats_by_id[assigned_beat_id]
            assignments[shot_id] = {
                "beat_id": assigned_beat_id,
                "beat_purpose": beat.description,
                "beat_emotional_tone": beat.emotional_tone,
            }
            
            # Enrich shot with beat context
            enriched_shot = shot.copy()
            enriched_shot["beat_id"] = assigned_beat_id
            enriched_shot["beat_purpose"] = beat.description
            enriched_shot["beat_emotional_tone"] = beat.emotional_tone
            # Keep original purpose, augment with beat context
            original_purpose = shot.get("purpose", "")
            if original_purpose:
                enriched_shot["story_purpose"] = f"{beat.description}: {original_purpose}"
            else:
                enriched_shot["story_purpose"] = beat.description
            enriched_shots.append(enriched_shot)
        else:
            enriched_shots.append(shot)
        
        cumulative_shot_time = shot_end
    
    for beat in story.beats:
        beat.shot_ids = [
            shot_id for shot_id, assignment in assignments.items()
            if assignment["beat_id"] == beat.beat_id
        ]
    
    return {
        "story_shape": story.model_dump(),
        "assignments": assignments,
        "enriched_shots": enriched_shots,
    }

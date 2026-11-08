"""Story shaping activities using OpenAI Agents SDK.

Generates StoryShape from user idea with genre presets, then critiques and validates it.
"""

import json
import os
from typing import Any

from temporalio import activity

from hfvg.models import StoryBeat, StoryShape, StoryShapeCritique


# Test stub for deterministic testing
_test_stub_story_shape: StoryShape | None = None


def set_test_stub_story_shape(story_shape: StoryShape | None) -> None:
    """Set a test stub for deterministic testing."""
    global _test_stub_story_shape
    _test_stub_story_shape = story_shape


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
5. A payoff that references or completes the hook
6. Emotional arc per beat (tone/feeling)
7. Each beat should be 5-20 seconds

Return JSON with this structure:
{{
  "logline": "one sentence story summary",
  "hook": "attention-grabbing opening moment",
  "hook_deadline_s": 5.0,
  "beats": [
    {{
      "beat_id": "beat_1",
      "description": "what happens in this beat",
      "emotional_tone": "excitement|curiosity|tension|relief|joy|etc",
      "target_duration_s": 15.0,
      "shot_ids": []
    }}
  ],
  "midpoint_beat_id": "beat_2",
  "payoff": "callback or resolution of the hook",
  "payoff_references_hook": true,
  "total_duration_s": {target_duration}
}}

Focus on strong storytelling fundamentals. Make every beat earn its screen time."""


def _critique_story_shape(story: StoryShape) -> StoryShapeCritique:
    """Rule-based critique of a story shape."""
    issues = story.validate_structure()
    score = 100
    
    hook_present_early = story.hook_deadline_s <= 5.0
    if not hook_present_early:
        issues.append(f"Hook deadline ({story.hook_deadline_s}s) exceeds 5s")
        score -= 15
    
    payoff_refs_hook = story.payoff_references_hook
    if not payoff_refs_hook:
        # Simple heuristic: check if key words from hook appear in payoff
        hook_words = set(story.hook.lower().split())
        payoff_words = set(story.payoff.lower().split())
        common_significant = [w for w in hook_words & payoff_words if len(w) > 4]
        if common_significant:
            payoff_refs_hook = True
    
    if not payoff_refs_hook:
        issues.append("Payoff doesn't clearly reference the hook")
        score -= 20
    
    beat_durations = sum(beat.target_duration_s for beat in story.beats)
    durations_fit = abs(beat_durations - story.total_duration_s) <= 5.0
    if not durations_fit:
        score -= 20
    
    all_beats_have_shots = all(beat.shot_ids for beat in story.beats)
    
    # Dead beats: no single beat should be longer than 1/3 of total duration
    max_beat_duration = story.total_duration_s / 3.0
    dead_beats = [b for b in story.beats if b.target_duration_s > max_beat_duration]
    no_dead_beats = not dead_beats
    if not no_dead_beats:
        score -= 10 * len(dead_beats)
    
    suggestions = []
    if not hook_present_early:
        suggestions.append("Move hook to first 5 seconds")
    if not payoff_refs_hook:
        suggestions.append("Add explicit callback to hook in payoff")
    if not durations_fit:
        suggestions.append("Adjust beat durations to sum to target")
    if dead_beats:
        suggestions.append(f"Split long beats: {', '.join(b.beat_id for b in dead_beats)}")
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
    idea: str,
    genre_preset: str | None = None,
    target_duration: float = 120.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Generate a StoryShape from user idea using OpenAI Agents SDK.
    
    Args:
        idea: User's story idea
        genre_preset: Optional genre hint (comedy, adventure, slice-of-life, mystery, action)
        target_duration: Target episode duration in seconds (default 120s)
        dry_run: If True, use test stub (no paid API calls)
        
    Returns:
        dict with:
            - story_shape: StoryShape dict
            - dry_run: bool
            - model_used: str (if not dry run)
    """
    if dry_run or _test_stub_story_shape:
        if _test_stub_story_shape:
            story = _test_stub_story_shape
        else:
            story = StoryShape(
                logline="Test story logline",
                hook="An exciting opening moment",
                hook_deadline_s=5.0,
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
                payoff="The hook pays off with a satisfying conclusion",
                payoff_references_hook=True,
                total_duration_s=target_duration,
            )
        
        return {
            "story_shape": story.model_dump(),
            "dry_run": True,
        }
    
    # Live mode: use OpenAI Agents SDK
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY required for live story shaping. "
            "Run in dry mode or set the API key."
        )
    
    try:
        from openai import OpenAI
        
        client = OpenAI(api_key=api_key)
        prompt = _build_story_prompt(idea, genre_preset, target_duration)
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
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
        
        result = json.loads(response.choices[0].message.content)
        
        story = StoryShape(
            logline=result["logline"],
            hook=result["hook"],
            hook_deadline_s=result.get("hook_deadline_s", 5.0),
            beats=[
                StoryBeat(
                    beat_id=beat["beat_id"],
                    description=beat["description"],
                    emotional_tone=beat["emotional_tone"],
                    target_duration_s=beat["target_duration_s"],
                    shot_ids=beat.get("shot_ids", []),
                )
                for beat in result["beats"]
            ],
            midpoint_beat_id=result["midpoint_beat_id"],
            payoff=result["payoff"],
            payoff_references_hook=result.get("payoff_references_hook", False),
            total_duration_s=result.get("total_duration_s", target_duration),
        )
        
        return {
            "story_shape": story.model_dump(),
            "dry_run": False,
            "model_used": "gpt-4o-mini",
        }
    
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
        dry_run: If True, use test stub (no paid API calls)
        
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
        
        if not critique.durations_fit:
            beat_durations = sum(b.target_duration_s for b in revised.beats)
            diff = story.total_duration_s - beat_durations
            if revised.beats:
                revised.beats[0].target_duration_s += diff
                changes.append(f"Adjusted beat_1 duration by {diff:.1f}s")
        
        if not critique.payoff_references_hook and "callback" not in revised.payoff.lower():
            revised.payoff = f"{revised.payoff} [Callback to: {revised.hook[:30]}...]"
            revised.payoff_references_hook = True
            changes.append("Added explicit hook callback to payoff")
        
        return {
            "story_shape": revised.model_dump(),
            "changes_made": changes or ["No automatic fixes needed"],
            "dry_run": True,
        }
    
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY required for live revision")
    
    try:
        from openai import OpenAI
        
        client = OpenAI(api_key=api_key)
        
        prompt = f"""Revise this story structure based on critique.

Original:
{json.dumps(story.model_dump(), indent=2)}

Critique issues:
{json.dumps(critique.issues, indent=2)}

Suggestions:
{json.dumps(critique.suggestions, indent=2)}

Return the revised structure as JSON with the same schema. Keep what works, fix the issues."""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
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
        
        result = json.loads(response.choices[0].message.content)
        revised = StoryShape(**result)
        
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
async def assign_shots_to_beats_activity(
    story_shape_dict: dict,
    shots: list[dict],
) -> dict[str, Any]:
    """
    Assign shots from beatmap to story beats based on timing.
    
    Args:
        story_shape_dict: StoryShape as dict
        shots: Parsed beatmap shots with screen_time
        
    Returns:
        dict with:
            - story_shape: updated StoryShape dict with shot assignments
            - assignments: shot_id -> beat_id mapping
    """
    story = StoryShape(**story_shape_dict)
    
    assignments = {}
    beat_end_times = []
    current_time = 0.0
    
    for beat in story.beats:
        current_time += beat.target_duration_s
        beat_end_times.append((current_time, beat.beat_id))
    
    cumulative_shot_time = 0.0
    for shot in shots:
        shot_id = shot["shot_id"]
        shot_duration = shot.get("screen_time", 5.0)
        
        shot_start = cumulative_shot_time
        shot_end = cumulative_shot_time + shot_duration
        shot_mid = (shot_start + shot_end) / 2
        
        for beat_end, beat_id in beat_end_times:
            if shot_mid <= beat_end:
                assignments[shot_id] = beat_id
                break
        
        cumulative_shot_time = shot_end
    
    for beat in story.beats:
        beat.shot_ids = [
            shot_id for shot_id, beat_id in assignments.items()
            if beat_id == beat.beat_id
        ]
    
    return {
        "story_shape": story.model_dump(),
        "assignments": assignments,
    }

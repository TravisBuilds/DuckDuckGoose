"""Preset-driven drafting for the create wizard: shot plan, test drafts, audio plan and voice slots.

Pure functions over a resolved preset profile (``hfvg.genre_presets.resolve_profile``), shared by
the draft endpoint, the DRAFT_LLM_FAKE test drafts and the tests, so "what this card's draft looks
like" has one definition. The audio plan uses the real ``hfvg.audio_mix.normalize_plan`` schema.
"""

from __future__ import annotations

import math
from typing import Any

from hfvg.config import config
from hfvg.story_structures import build_layout, hook_and_keyword

KLING_MIN_S = 3
KLING_MAX_S = 15
MAX_DRAFT_SHOTS = 30
VO_MIN_SLOT_S = 6.0
VO_START_PAD_S = 0.3
VO_CHARS_PER_S = 15.0  # speaking rate the dry stand-ins assume (hfvg.activities.audio.DRY_VO_CHARS_PER_S)
MUSIC_BED_DB = -30.2  # under a voice-over (normalize_plan default)
MUSIC_ONLY_DB = -18.0  # the music carries the soundtrack
VO_MODEL = "eleven_v3"

NARRATOR = "Narrator"
VOICE_HINTS = {
    NARRATOR: "calm, warm storyteller voice",
    "Host": "energetic, upbeat presenter voice",
    "Guide": "older, gentle and patient voice",
    "Lead": "bright, curious voice",
    "Friend": "relaxed, friendly voice",
}

TEMPLATE_RULES = {
    "mood_loop": "Mood loop: 1-3 slow shots, gentle ambient motion only, the last frame matches the first so "
                 "the video loops seamlessly. No plot acts, no voice-over.",
    "music_montage": "Music montage: cut-to-music shots that build a visual journey, end on a title image. "
                     "No dialogue.",
    "dialogue_episode": "Dialogue episode: shot-reverse-shot coverage of the speakers; the script is "
                        "speaker-tagged dialogue; end on a cliffhanger.",
    "continuous_pov": "Continuous POV: one unbroken camera journey; each shot continues from the previous "
                      "shot's last frame (no hard cuts in the action).",
    "cozy_micro": "Cozy micro moment: a tiny wish-effort-reveal arc with macro inserts and a strong, "
                  "consistent style.",
    "action_sequence": "Action set piece: standoff, escalating clash, decisive finisher; stylized, no gore.",
    "gag_short": "Gag short: setup, escalation, punchline; the punchline lands on a caption card.",
    "freeform": "Short story video with a clear beginning, middle and end.",
}


def _clean(text: str, limit: int = 160) -> str:
    """One line of table/prompt-safe text: no pipes or newlines, at most ``limit`` characters."""
    out = " ".join(str(text or "").replace("|", "/").split())
    if len(out) <= limit:
        return out
    return out[:limit].rsplit(" ", 1)[0]


def shot_lengths(profile: dict, target_s: float) -> list[int]:
    """Whole-second Kling shot lengths (3-15 s) from the card's pacing, summing exactly to target_s."""
    total = int(round(target_s))
    cuts = float(profile.get("cuts_per_10s") or 0)
    ideal = KLING_MAX_S if cuts <= 0 else min(KLING_MAX_S, max(KLING_MIN_S, round(10.0 / cuts)))
    n = min(max(1, round(total / ideal)), MAX_DRAFT_SHOTS, max(1, total // KLING_MIN_S))
    n = max(n, math.ceil(total / KLING_MAX_S))
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def speakers_for(profile: dict) -> list[str]:
    """Speaker names a draft uses: narrator for narration, two roles for dialogue, none otherwise."""
    mode = profile.get("vo_mode")
    if mode == "narration":
        return [NARRATOR]
    if mode == "dialogue":
        return ["Host", "Guide"] if "host" in (profile.get("brief_fields") or []) else ["Lead", "Friend"]
    return []


def vo_slots(lengths: list[float]) -> list[dict]:
    """Windows for one VO line each: consecutive shots grouped to at least VO_MIN_SLOT_S."""
    slots, start, acc = [], 0.0, 0.0
    t = 0.0
    for d in lengths:
        acc += d
        t += d
        if acc >= VO_MIN_SLOT_S:
            slots.append({"start": start, "dur": acc})
            start, acc = t, 0.0
    if acc > 0:
        if slots and acc < VO_MIN_SLOT_S:
            slots[-1]["dur"] += acc
        else:
            slots.append({"start": start, "dur": acc})
    for s in slots:
        s["max_chars"] = max(12, min(160, int((s["dur"] - VO_START_PAD_S - 0.7) * VO_CHARS_PER_S * 0.8)))
    return slots


def fit_line(text: str, max_chars: int) -> tuple[str, bool]:
    """The line cut at a word boundary so it fits its slot (True when it had to be shortened)."""
    text = _clean(text, 400)
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or text[:max_chars]), True


def build_audio_plan(profile: dict, screen_times: list[float], script: list[dict] | None) -> tuple[dict, list[str]]:
    """An audio plan in the normalize_plan schema for the card's sound mode, plus warnings.

    - none / caption_only: music only (plus template SFX); captions stay in the picture.
    - narration: one voice for every line (``vo.voice_id``, chosen in the wizard).
    - dialogue: ``vo.speakers`` (name -> voice id, chosen in the wizard) and speaker-tagged lines.
    ``script`` is [{"speaker", "text"}] in order; lines are placed one per VO slot."""
    warnings: list[str] = []
    total = float(sum(screen_times))
    mode = profile.get("vo_mode") or "none"
    speakers = speakers_for(profile)
    plan: dict[str, Any] = {}
    lines = []
    if mode in ("narration", "dialogue") and script:
        slots = vo_slots(screen_times)
        if len(script) > len(slots):
            warnings.append(f"The script has {len(script)} lines but the shots fit {len(slots)}; "
                            f"the last {len(script) - len(slots)} were left out.")
        named = set()
        for i, (entry, slot) in enumerate(zip(script, slots), start=1):
            text, cut = fit_line(entry.get("text") or "", slot["max_chars"])
            if not text:
                continue
            if cut:
                warnings.append(f"VO line L{i:02d} was shortened to fit its {slot['dur']:g}s of picture.")
            line = {"id": f"L{i:02d}", "at": round(slot["start"] + VO_START_PAD_S, 2), "text": text}
            if mode == "dialogue":
                speaker = _clean(entry.get("speaker") or speakers[(i - 1) % len(speakers)], 60)
                line["speaker"] = speaker
                named.add(speaker)
            lines.append(line)
        if mode == "dialogue":
            order = [s for s in speakers if s in named] + sorted(named - set(speakers))
            plan["vo"] = {"voice_id": None, "model": VO_MODEL, "speakers": {s: "" for s in order},
                          "lines": lines}
        else:
            plan["vo"] = {"voice_id": None, "model": VO_MODEL, "lines": lines}
    elif mode in ("narration", "dialogue"):
        warnings.append("No voice-over lines were drafted; the video will have music only.")
    music = str(profile.get("music") or "").strip()
    if music and profile.get("music_mode") != "none":
        plan["music"] = {"prompt": _clean(f"{music} instrumental music for a {profile.get('name', 'short')} video", 200),
                         "loop": True, "level_db": MUSIC_BED_DB if lines else MUSIC_ONLY_DB}
    sfx = _template_sfx(profile, screen_times, total)
    if sfx:
        plan["sfx"] = sfx
    if not plan.get("music") and not lines and not sfx:
        plan["music"] = {"prompt": "gentle instrumental underscore", "loop": True, "level_db": MUSIC_ONLY_DB}
    return plan, warnings


def _template_sfx(profile: dict, screen_times: list[float], total: float) -> list[dict]:
    template = profile.get("template")
    last_at = round(total - screen_times[-1], 2) if screen_times else 0.0
    last_dur = min(3.0, screen_times[-1]) if screen_times else 1.0
    if template == "continuous_pov":
        return [{"id": "S01", "at": 0.0, "dur": round(total, 2), "loop": True, "level_db": -32.0,
                 "prompt": "soft outdoor ambience, gentle wind and distant movement"}]
    if template == "action_sequence":
        return [{"id": "S01", "at": last_at, "dur": last_dur, "prompt": "stylized impact whoosh and heavy hit"}]
    if template == "cozy_micro":
        return [{"id": "S01", "at": 0.0, "dur": min(3.0, screen_times[0]), "level_db": -30.0,
                 "prompt": "soft cozy foley, gentle clinks and rustles"}]
    if template == "gag_short" or "sfx" in str(profile.get("music") or "").lower():
        return [{"id": "S01", "at": last_at, "dur": min(1.5, last_dur), "prompt": "short comedic sting"}]
    return []


def voice_slots(plan: dict, profile: dict) -> list[dict]:
    """What the wizard asks a voice for: the narrator (lines without a speaker), each dialogue speaker."""
    vo = plan.get("vo") or {}
    lines = vo.get("lines") or []
    slots = []
    narrated = [ln for ln in lines if not ln.get("speaker")]
    if narrated:
        slots.append({"key": "narrator", "role": "narrator", "label": NARRATOR, "speaker": None,
                      "lines": len(narrated), "suggestion": VOICE_HINTS[NARRATOR]})
    for name in vo.get("speakers") or {}:
        count = sum(1 for ln in lines if ln.get("speaker") == name)
        slots.append({"key": f"speaker:{name}", "role": "speaker", "label": name, "speaker": name,
                      "lines": count, "suggestion": VOICE_HINTS.get(name, f"voice for {name}")})
    return slots


def llm_instructions(profile: dict, target_s: int) -> str:
    """Card-specific drafting rules for the LLM prompt: format, shots, sound and script shape."""
    lengths = shot_lengths(profile, target_s)
    mode = profile.get("vo_mode") or "none"
    lo, hi = min(lengths), max(lengths)
    lines = [
        f"Card: {profile.get('name')} ({profile.get('template')}). {TEMPLATE_RULES.get(profile.get('template'), '')}",
        f"Aspect ratio of the card: {profile.get('aspect')}",
        f"Length: {target_s}s in total. Plan {len(lengths)} shots of {lo}-{hi}s each (3-15s per shot), "
        f"so the shot durations add up to {target_s}s.",
    ]
    if profile.get("shot_grammar"):
        lines.append("Shot grammar: " + ", ".join(profile["shot_grammar"]))
    if profile.get("brief_fields"):
        lines.append("Cover these brief fields: " + ", ".join(profile["brief_fields"]))
    if mode == "narration":
        lines.append('Sound: one narrator. Add "audio_script": a list of {"speaker": "Narrator", "text": "..."} '
                     "lines, one per 6-10 seconds of picture, each under 120 characters.")
    elif mode == "dialogue":
        names = " and ".join(speakers_for(profile))
        lines.append('Sound: speaker-tagged dialogue. Add "audio_script": a list of {"speaker": "<name>", '
                     f'"text": "..."}} lines alternating between {names} (or your own speaker names), one '
                     "per 6-10 seconds of picture, each under 120 characters.")
    elif mode == "caption_only":
        lines.append("Sound: music only; any words appear as on-screen captions, never as voice-over.")
    else:
        lines.append("Sound: music and sound effects only, no voice-over.")
    return "\n".join(lines)


def fake_draft(profile: dict, idea: str, episode_id: str, target_s: int, style: str) -> dict:
    """Deterministic DRAFT_LLM_FAKE draft for any card (test/e2e only): files that pass the real
    validators, shot lengths from the card's pacing and a script for voice cards."""
    lengths = shot_lengths(profile, target_s)
    layout = build_layout(profile, idea, float(target_s), config.STORY_SHAPING_MAX_BEAT_DURATION_S)
    _, keyword = hook_and_keyword(idea)
    short = _clean(idea, 90)
    grammar = list(profile.get("shot_grammar") or []) or ["cinematic shot"]
    speakers = speakers_for(profile)
    two_hander = profile.get("vo_mode") == "dialogue"
    acts = _act_for_times(layout, lengths)
    rows, shots = [], []
    for i, d in enumerate(lengths, start=1):
        sid = f"S{i:02d}"
        chars = ["A", "B"] if two_hander and i == 1 else (["B"] if two_hander and i % 2 == 0 else ["A"])
        frame = grammar[(i - 1) % len(grammar)]
        action = _clean(f"{frame}: {short}", 120)
        rows.append(f"| {i} | {sid} | {d:.1f} ({d:.1f}) | Scene | Day | {' '.join(chars)} | {action} | "
                    f"{acts[i - 1]} | - | - | K |")
        shots.append({"id": sid, "prompt": _clean(f"{frame}, {short}, {style}", 400), "count": 1,
                      "characters": chars, "wet": False, "model": "kling", "duration": d,
                      "end_frame": False, "ref_keys": []})
    beatmap = (f"# Episode {episode_id} Beatmap\n\n"
               "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
               "|---|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
    characters = {"A": {"description": f"original lead character of the story: {short}"}}
    if two_hander:
        characters["B"] = {"description": "original companion character who talks with the lead"}
    script = None
    if profile.get("vo_mode") in ("narration", "dialogue"):
        templates = [
            f"Here it begins, with the {keyword}.",
            "Look closer, something is changing.",
            f"Nobody expected this from the {keyword}.",
            "Stay with me, it gets better.",
            "And just like that, everything shifts.",
            f"This is where the {keyword} story turns.",
        ]
        script = [{"speaker": speakers[(k % len(speakers))], "text": templates[k % len(templates)]}
                  for k in range(len(vo_slots(lengths)))]
    return {
        "beatmap_md": beatmap,
        "shots_json": {"shots": shots},
        "refs_json": {"per_shot": {s["id"]: [] for s in shots}},
        "prompt_kit_json": {"style": style, "aspect_ratio": "9:16", "characters": characters,
                            "sets": {"Scene": f"original setting for: {short}"}},
        "audio_script": script,
    }


def _act_for_times(layout: dict, lengths: list[int]) -> list[str]:
    """Each shot's story section (by the shot's midpoint), for the beatmap Purpose column."""
    ends, t = [], 0.0
    for act in layout["acts"]:
        t += act["target_duration_s"]
        ends.append((t, act["description"]))
    out, start = [], 0.0
    for d in lengths:
        mid = start + d / 2
        out.append(next((name for end, name in ends if mid <= end + 1e-6), ends[-1][1]))
        start += d
    return out


def script_from_llm(result: dict) -> list[dict] | None:
    """The optional "audio_script" of an LLM draft as [{"speaker", "text"}] (None when absent)."""
    raw = result.get("audio_script")
    if not isinstance(raw, list):
        return None
    out = []
    for entry in raw:
        if isinstance(entry, dict) and str(entry.get("text") or "").strip():
            out.append({"speaker": str(entry.get("speaker") or "").strip() or None,
                        "text": str(entry["text"])})
        elif isinstance(entry, str) and entry.strip():
            out.append({"speaker": None, "text": entry})
    return out or None

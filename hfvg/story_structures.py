"""Story structures per genre template: section layout, beat plan and prompt guidance.

Pure functions, no I/O. Story shaping uses them for the LLM prompt and for the deterministic
dry-run shape, so a 12 s mood loop gets a 3-beat loop and a 120 s episode gets a full arc, instead
of every card being forced into the same 3-act 25/50/25 sheet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

COMMON_WORDS = {"the", "and", "that", "this", "with", "from", "have", "they", "what", "will"}


@dataclass(frozen=True)
class Section:
    name: str
    share: float
    purpose: str
    beats: tuple[tuple[str, str], ...]  # (description template, emotional tone); last = section closer


@dataclass(frozen=True)
class Structure:
    name: str
    title: str
    guidance: str
    sections: tuple[Section, ...]
    payoff: str  # template; must contain " {keyword} " so the payoff shares a word with the hook


STRUCTURES: dict[str, Structure] = {s.name: s for s in (
    Structure(
        "three_act", "Three acts",
        "Classic arc: setup, confrontation, resolution.",
        (Section("Setup", 0.25, "introduce the world, the character and what they want",
                 (("Open on the world of the idea: {idea}", "curiosity"),
                  ("The main character commits to a goal", "anticipation"))),
         Section("Confrontation", 0.5, "obstacles and rising stakes, the midpoint turn",
                 (("An obstacle blocks the way", "tension"),
                  ("Stakes rise around the {keyword} quest", "urgency"),
                  ("The lowest point before the turn", "doubt"))),
         Section("Resolution", 0.25, "the turn pays off and the hook comes back",
                 (("A new approach finally works", "relief"),
                  ("Payoff: the {keyword} moment comes full circle", "joy")))),
        "The ending returns to the {keyword} from the opening and resolves it",
    ),
    Structure(
        "loop", "Seamless loop",
        "One continuous mood, no plot acts. Gentle motion only; the last frame must match the first "
        "frame so the video loops without a visible seam.",
        (Section("Loop", 1.0, "one mood held from first to last frame",
                 (("Opening frame: {idea}", "calm"),
                  ("Ambient motion drifts: light, weather and small gestures", "wonder"),
                  ("Settle back into the opening frame so the end meets the start", "calm"))),),
        "The final frame matches the opening {keyword} frame so the loop is seamless",
    ),
    Structure(
        "montage", "Music montage",
        "Connected images cut to the music, no dialogue acts: a visual journey that builds to an "
        "emotional peak and ends on a closing title image.",
        (Section("Montage", 0.85, "cut-to-music images that build the journey",
                 (("Opening image: {idea}", "wonder"),
                  ("Cut-to-music moments move the journey forward", "joy"),
                  ("The emotional peak of the montage", "longing"))),
         Section("Closing image", 0.15, "a closing title image that answers the opening",
                 (("Closing title image: the {keyword} journey resolved", "warmth"),))),
        "The closing title image echoes the opening {keyword} image",
    ),
    Structure(
        "continuous_journey", "Continuous journey",
        "One unbroken camera journey (single-take feel, no hard cuts): start moving, the view "
        "opens up, arrive. Consecutive shots chain from the previous shot's last frame.",
        (Section("Continuous take", 1.0, "one unbroken move from start to arrival",
                 (("Start moving: {idea}", "anticipation"),
                  ("The view opens up without a cut", "awe"),
                  ("Arrive at the destination in the same take", "release"))),),
        "The journey arrives where the {keyword} was heading from the first second",
    ),
    Structure(
        "escalation_punchline", "Setup, escalation, punchline",
        "A single gag: establish the normal situation, escalate it twice, land the punchline on a "
        "caption card.",
        (Section("Setup", 0.3, "the normal situation",
                 (("Setup: the normal situation, {idea}", "curiosity"),)),
         Section("Escalation", 0.5, "the situation escalates",
                 (("It escalates", "amusement"),
                  ("It escalates even further", "disbelief"))),
         Section("Punchline", 0.2, "the punchline card",
                 (("Punchline card: the {keyword} twist lands", "laughter"),))),
        "The punchline flips the {keyword} setup on its head",
    ),
    Structure(
        "episode_cliffhanger", "Episode with cliffhanger",
        "A dialogue episode: cold-open hook, characters talk and act through the problem with "
        "speaker-tagged lines, end on a cliffhanger.",
        (Section("Cold open", 0.15, "a hook before the titles",
                 (("Cold open hook: {idea}", "intrigue"),)),
         Section("Episode", 0.65, "the characters talk and act through the problem",
                 (("The characters talk through the problem", "tension"),
                  ("A reveal changes the plan", "surprise"),
                  ("The new plan in motion", "momentum"))),
         Section("Cliffhanger", 0.2, "an unresolved turn that sets up the next episode",
                 (("Cliffhanger: the {keyword} is not what it seemed", "suspense"),))),
        "The cliffhanger turns the {keyword} from the cold open into a new question",
    ),
    Structure(
        "micro_arc", "Micro arc",
        "A tiny cozy arc in a few seconds: a small wish, a small effort, a sweet reveal.",
        (Section("Micro arc", 1.0, "wish, effort, reveal",
                 (("A small wish: {idea}", "warmth"),
                  ("A tiny effort", "focus"),
                  ("Cozy reveal: the {keyword} moment", "delight"))),),
        "The reveal delivers the {keyword} moment the opening promised",
    ),
    Structure(
        "escalation", "Set-piece escalation",
        "An action set piece: a standoff, an escalating clash, a decisive finisher. Keep it "
        "stylized and free of gore.",
        (Section("Standoff", 0.2, "the opponents size each other up",
                 (("Standoff: {idea}", "tension"),)),
         Section("Escalation", 0.6, "the clash escalates",
                 (("First clash", "adrenaline"),
                  ("The fight escalates", "intensity"))),
         Section("Finisher", 0.2, "the decisive move",
                 (("Finisher: the {keyword} decides it", "triumph"),))),
        "The finisher pays off the {keyword} set up in the standoff",
    ),
)}


def structure_for(genre_preset: dict | str | None) -> Structure:
    """The preset profile's story_structure; legacy genre strings and no preset get three acts."""
    if isinstance(genre_preset, dict):
        return STRUCTURES.get(str(genre_preset.get("story_structure") or ""), STRUCTURES["three_act"])
    return STRUCTURES["three_act"]


def beat_counts(structure: Structure, total_s: float, max_beat_s: float) -> list[int]:
    """Beats per section: at least 3 overall, at least 1 per section, no beat over max_beat_s."""
    n_min = max(3, math.ceil(total_s / max_beat_s))
    counts = [max(1, round(s.share * n_min)) for s in structure.sections]
    while sum(counts) < n_min:
        i = max(range(len(counts)), key=lambda k: structure.sections[k].share / counts[k])
        counts[i] += 1
    for i, s in enumerate(structure.sections):
        while s.share * total_s / counts[i] > max_beat_s:
            counts[i] += 1
    return counts


def _durations(total_s: float, shares: list[float]) -> list[float]:
    """Split total_s by shares at 0.1 s resolution; the last part absorbs rounding (exact sum)."""
    parts = [round(total_s * s, 1) for s in shares[:-1]]
    return [*parts, round(total_s - sum(parts), 1)]


def _short_idea(idea: str, limit: int = 90) -> str:
    text = " ".join(str(idea or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


def hook_and_keyword(idea: str) -> tuple[str, str]:
    """A hook of at least 10 characters plus a word from it the payoff will repeat."""
    short = _short_idea(idea)
    for word in short.split():
        if re.fullmatch(r"[A-Za-z][A-Za-z'-]{3,}", word) and word.lower() not in COMMON_WORDS:
            return f"Opening: {short}", word
    return f"Opening: {short} in the first frame", "frame"


def build_layout(genre_preset: dict | str | None, idea: str, total_s: float,
                 max_beat_s: float) -> dict:
    """Deterministic story layout for a structure: acts (sections) and beats with exact durations."""
    structure = structure_for(genre_preset)
    hook, keyword = hook_and_keyword(idea)
    short = _short_idea(idea)
    counts = beat_counts(structure, total_s, max_beat_s)
    section_s = _durations(total_s, [s.share for s in structure.sections])
    acts, beats = [], []
    last_section = len(structure.sections)
    for a, (section, count, dur) in enumerate(zip(structure.sections, counts, section_s), start=1):
        templates = list(section.beats)
        if count == 1:
            picked = [templates[-1] if a == last_section else templates[0]]
        elif count <= len(templates):
            picked = templates[:count - 1] + [templates[-1]]
        else:
            middle = templates[1:-1] or templates[:1]
            picked = [templates[0], *[middle[k % len(middle)] for k in range(count - 2)], templates[-1]]
        beat_s = _durations(dur, [1.0 / count] * count)
        ids = []
        for k, ((text, tone), seconds) in enumerate(zip(picked, beat_s), start=1):
            beat_id = f"beat_{a}_{k}"
            ids.append(beat_id)
            description = text.format(idea=short, keyword=keyword)
            if k > 1 and picked[k - 2][0] == text:
                description += " (continued)"
            beats.append({"beat_id": beat_id, "description": description,
                          "emotional_tone": tone, "target_duration_s": seconds, "shot_ids": []})
        acts.append({"act_number": a, "description": section.name, "target_duration_s": dur,
                     "beat_ids": ids})
    return {
        "structure": structure.name,
        "logline": short,
        "hook": hook,
        "acts": acts,
        "beats": beats,
        "midpoint_beat_id": beats[len(beats) // 2]["beat_id"],
        "payoff": structure.payoff.format(keyword=keyword),
        "total_duration_s": float(total_s),
    }


def prompt_guidance(genre_preset: dict | str | None, total_s: float, max_beat_s: float) -> str:
    """Prompt text: the structure, its sections as acts with their share of the length."""
    structure = structure_for(genre_preset)
    counts = beat_counts(structure, total_s, max_beat_s)
    section_s = _durations(total_s, [s.share for s in structure.sections])
    lines = [f"Story structure: {structure.title}. {structure.guidance}",
             f"Use exactly {len(structure.sections)} act(s), in this order, as the \"acts\" list:"]
    for a, (section, count, dur) in enumerate(zip(structure.sections, counts, section_s), start=1):
        lines.append(f"   - Act {a} \"{section.name}\": about {dur:g}s in {count} beat(s); {section.purpose}")
    return "\n".join(lines)

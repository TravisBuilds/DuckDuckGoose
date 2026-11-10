"""Pick an ElevenLabs voice from a plain-words description ("warm, older storyteller, calm").

Creators describe each voice in the create wizard instead of browsing voice ids. A pick ranks the
voices the ElevenLabs key can use (its own and the premade ones, ElevenLabsProvider.list_voices)
and lets the LLM choose among the best candidates when OPENAI_API_KEY is set; without an LLM, or
when its answer is not one of the candidates, the description is scored against each voice's
labels. "Try another voice" sends the ids already shown, which are left out of the next pick.

Dry runs use FAKE_VOICES (placeholder ids, generated preview tones) and never touch the network.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import wave
from dataclasses import dataclass
from typing import Any

FAKE_VOICE_PREFIX = "dryrun-voice-"
LLM_CANDIDATES = 40

FAKE_VOICES: tuple[dict[str, Any], ...] = (
    {"voice_id": "dryrun-voice-maple", "name": "Maple", "category": "premade",
     "labels": {"gender": "female", "age": "middle aged", "accent": "american", "descriptive": "warm",
                "use_case": "narration"},
     "description": "Warm, calm storyteller"},
    {"voice_id": "dryrun-voice-cedar", "name": "Cedar", "category": "premade",
     "labels": {"gender": "male", "age": "old", "accent": "american", "descriptive": "deep",
                "use_case": "narration"},
     "description": "Deep, slow voice of an older storyteller"},
    {"voice_id": "dryrun-voice-pebble", "name": "Pebble", "category": "premade",
     "labels": {"gender": "female", "age": "young", "accent": "american", "descriptive": "playful",
                "use_case": "characters"},
     "description": "Bright, playful and quick"},
    {"voice_id": "dryrun-voice-harbor", "name": "Harbor", "category": "premade",
     "labels": {"gender": "male", "age": "middle aged", "accent": "british", "descriptive": "calm",
                "use_case": "conversational"},
     "description": "Relaxed, friendly host"},
    {"voice_id": "dryrun-voice-ember", "name": "Ember", "category": "premade",
     "labels": {"gender": "female", "age": "young", "accent": "australian", "descriptive": "energetic",
                "use_case": "announcer"},
     "description": "Excited, punchy announcer"},
    {"voice_id": "dryrun-voice-juniper", "name": "Juniper", "category": "premade",
     "labels": {"gender": "female", "age": "old", "accent": "irish", "descriptive": "soft",
                "use_case": "whisper"},
     "description": "Soft, hushed voice for quiet, cosy moments"},
    {"voice_id": "dryrun-voice-atlas", "name": "Atlas", "category": "premade",
     "labels": {"gender": "male", "age": "young", "accent": "american", "descriptive": "confident",
                "use_case": "presenter"},
     "description": "Upbeat young presenter"},
    {"voice_id": "dryrun-voice-willow", "name": "Willow", "category": "premade",
     "labels": {"gender": "neutral", "age": "middle aged", "accent": "american", "descriptive": "even",
                "use_case": "documentary"},
     "description": "Clear, even documentary voice"},
)

# Description words -> the concept they ask for. Voices are tagged with the same concepts from
# their name, labels and description, so "an older man with a low voice" meets age=old, deep.
CONCEPT_WORDS: dict[str, tuple[str, ...]] = {
    "female": ("female", "woman", "women", "girl", "lady", "feminine", "she", "her", "mother", "mom",
               "grandmother", "grandma", "aunt", "sister", "queen"),
    "male": ("male", "man", "men", "boy", "guy", "masculine", "he", "his", "father", "dad",
             "grandfather", "grandpa", "uncle", "brother", "king"),
    "neutral": ("neutral", "androgynous", "nonbinary", "ungendered"),
    "old": ("old", "older", "elderly", "senior", "grandmother", "grandfather", "grandma", "grandpa", "wise",
            "ancient", "veteran"),
    "young": ("young", "younger", "youthful", "teen", "teenage", "teenager", "kid", "child", "childlike",
              "boy", "girl", "youth"),
    "middle": ("middle", "adult", "mature", "midlife"),
    "deep": ("deep", "low", "bass", "baritone", "booming", "rumbling"),
    "calm": ("calm", "soothing", "gentle", "soft", "relaxed", "quiet", "peaceful", "slow", "measured",
             "even", "steady"),
    "warm": ("warm", "friendly", "kind", "cozy", "cosy", "inviting", "caring", "tender"),
    "energetic": ("energetic", "excited", "upbeat", "lively", "hyper", "bright", "enthusiastic", "fast",
                  "loud", "hype", "punchy", "bubbly"),
    "playful": ("playful", "silly", "funny", "goofy", "cheeky", "comic", "comedic", "cartoon", "cartoony",
                "quirky", "characters"),
    "whisper": ("whisper", "whispery", "whispering", "asmr", "breathy", "hushed"),
    "narration": ("narrator", "narration", "narrative", "storyteller", "storytelling", "audiobook",
                  "narrate", "documentary"),
    "conversational": ("conversational", "casual", "chatty", "natural", "everyday", "host", "podcast"),
    "announcer": ("announcer", "commentator", "sports", "broadcast", "presenter", "trailer", "dramatic",
                  "epic"),
    "confident": ("confident", "bold", "strong", "authoritative", "commanding", "powerful"),
    "raspy": ("raspy", "husky", "rough", "gravelly", "hoarse", "gritty"),
    "british": ("british", "english", "uk", "london"),
    "american": ("american", "usa"),
    "australian": ("australian", "aussie"),
    "irish": ("irish",),
    "scottish": ("scottish",),
    "indian": ("indian",),
}
_WORD_CONCEPTS: dict[str, set[str]] = {}
for _concept, _words in CONCEPT_WORDS.items():
    for _word in _words:
        _WORD_CONCEPTS.setdefault(_word, set()).add(_concept)

WEIGHTS = {"female": 3.0, "male": 3.0, "neutral": 3.0, "old": 2.0, "young": 2.0, "middle": 1.5}
CONFLICTS = (({"female", "male", "neutral"}, 3.0), ({"old", "young", "middle"}, 2.0))


class NoVoiceLeftError(Exception):
    """Every available voice was already shown for this description."""


@dataclass(frozen=True)
class VoicePick:
    voice_id: str
    name: str
    preview_url: str | None
    labels: dict[str, str]
    description: str
    method: str  # "llm" or "label_match"
    reason: str
    remaining: int  # voices not shown yet after this one


def is_fake_voice_id(voice_id: str | None) -> bool:
    return bool(voice_id) and str(voice_id).startswith(FAKE_VOICE_PREFIX)


def _tone_data_uri(freq: float, seconds: float = 0.6, rate: int = 8000) -> str:
    """A short 8-bit WAV tone as a data: URI (the dry-run stand-in for a voice preview)."""
    n = int(seconds * rate)
    fade = rate // 20
    frames = bytes(
        int(128 + 60 * math.sin(2 * math.pi * freq * i / rate) * min(1.0, i / fade, (n - i) / fade))
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(frames)
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def fake_voices() -> list[dict[str, Any]]:
    """FAKE_VOICES with a distinct preview tone each (deterministic, no network)."""
    return [dict(v, labels=dict(v["labels"]), preview_url=_tone_data_uri(220.0 + 55.0 * i))
            for i, v in enumerate(FAKE_VOICES)]


def concepts(text: str) -> set[str]:
    found: set[str] = set()
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        found |= _WORD_CONCEPTS.get(word, set())
    return found


def voice_concepts(voice: dict[str, Any]) -> set[str]:
    labels = voice.get("labels") or {}
    text = " ".join([voice.get("name") or "", voice.get("description") or "", voice.get("category") or "",
                     *[str(v) for v in labels.values()]])
    return concepts(text)


def label_score(description: str, voice: dict[str, Any], role: str | None = None) -> float:
    """How well a voice's labels fit the description: shared concepts count (gender and age most),
    a different gender or age than asked for counts against it."""
    wanted = concepts(description)
    have = voice_concepts(voice)
    score = sum(WEIGHTS.get(c, 1.0) for c in wanted & have)
    for group, penalty in CONFLICTS:
        if wanted & group and have & group and not wanted & have & group:
            score -= penalty
    if (role or "").strip().lower() == "narrator" and "narration" in have:
        score += 0.5
    return score


def rank_voices(description: str, voices: list[dict[str, Any]], role: str | None = None) -> list[dict[str, Any]]:
    """Best label match first; ties keep the listing order (deterministic)."""
    scored = [(label_score(description, v, role), i, v) for i, v in enumerate(voices)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [v for _, _, v in scored]


async def _llm_choice(description: str, role: str | None, candidates: list[dict[str, Any]],
                      api_key: str, model: str) -> tuple[str, str] | None:
    """The LLM's pick among the candidates, or None when it fails or answers with another id."""
    from openai import AsyncOpenAI

    listing = [{"voice_id": v["voice_id"], "name": v.get("name"), "labels": v.get("labels") or {},
                "description": (v.get("description") or "")[:200]} for v in candidates]
    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": (
                "You match a description of a voice to the closest voice in a list. Reply with JSON "
                '{"voice_id": "<an id from the list>", "reason": "<one short sentence>"}.')},
            {"role": "user", "content": json.dumps(
                {"wanted_voice": description, "speaks_as": role or "narrator", "voices": listing})},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content or "{}")
    voice_id = data.get("voice_id")
    if voice_id not in {v["voice_id"] for v in candidates}:
        return None
    return str(voice_id), str(data.get("reason") or "")[:300]


def _match_reason(description: str, voice: dict[str, Any]) -> str:
    shared = sorted(concepts(description) & voice_concepts(voice))
    if not shared:
        return "No label matched the description closely; this is the nearest available voice."
    return "Matches: " + ", ".join(shared)


async def pick_voice(description: str, voices: list[dict[str, Any]], exclude: list[str] | tuple[str, ...] = (),
                     role: str | None = None, openai_api_key: str | None = None,
                     model: str = "gpt-4o-mini") -> VoicePick:
    """The voice that best fits the description, leaving out the ids in exclude."""
    excluded = set(exclude)
    pool = [v for v in voices if v.get("voice_id") and v["voice_id"] not in excluded]
    if not pool:
        raise NoVoiceLeftError("You have heard every available voice for this description. "
                               "Change the description or start over to hear them again.")
    ranked = rank_voices(description, pool, role)
    chosen, method, reason = ranked[0], "label_match", _match_reason(description, ranked[0])
    if openai_api_key:
        try:
            choice = await _llm_choice(description, role, ranked[:LLM_CANDIDATES], openai_api_key, model)
        except Exception as e:  # the label match below is the documented fallback
            choice = None
            reason = f"{reason} (voice matching assistant unavailable: {type(e).__name__})"
        if choice:
            chosen = next(v for v in pool if v["voice_id"] == choice[0])
            method, reason = "llm", choice[1]
    return VoicePick(
        voice_id=chosen["voice_id"],
        name=chosen.get("name") or chosen["voice_id"],
        preview_url=chosen.get("preview_url"),
        labels={str(k): str(v) for k, v in (chosen.get("labels") or {}).items()},
        description=chosen.get("description") or "",
        method=method,
        reason=reason,
        remaining=len(pool) - 1,
    )

"""Can every voice-over line of an episode's audio plan be spoken?

Checked when an episode starts (API), when an audio plan is uploaded into a reopened run (API), when
an episode is reopened (API) and again right after G1 approval, before the first paid ref or still of
a live run (approve_g108_activity). Workflow code never calls this: it reads files and the
environment.
"""

from __future__ import annotations


def plan_voice_problems(plan: dict, live: bool) -> list[str]:
    """Voice-over lines of a normalized audio plan with no voice to speak them (empty = OK).

    A line is spoken by its own or its speaker's voice, else the plan's voice_id, else
    ELEVENLABS_DEFAULT_VOICE_ID. In a live run a dry-run placeholder voice (hfvg.voice_picker) is no voice.
    """
    from hfvg import audio_mix
    from hfvg.activities.audio import default_voice_id
    from hfvg.voice_picker import is_fake_voice_id

    def usable(voice_id: str | None) -> str:
        return "" if live and is_fake_voice_id(voice_id) else (voice_id or "")

    vo = plan.get("vo") or {}
    checked = {"vo": {**vo, "voice_id": usable(vo.get("voice_id")),
                      "lines": [{**ln, "voice_id": usable(ln.get("voice_id"))} for ln in vo.get("lines") or []]}}
    missing = audio_mix.vo_lines_missing_voice(checked, usable(default_voice_id()))
    if not missing:
        return []
    placeholder = live and any(is_fake_voice_id(v) for v in
                               [vo.get("voice_id"), *[ln.get("voice_id") for ln in vo.get("lines") or []]])
    return [
        f"no voice chosen for the voice-over lines {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}"
        f"{' (dry-run preview voices cannot speak in a live run)' if placeholder else ''}. "
        "Describe the voice on the create page and pick one, or set ELEVENLABS_DEFAULT_VOICE_ID"
    ]


def audio_plan_voice_problems(episode_id: str, live: bool) -> list[str]:
    """plan_voice_problems of the episode's uploaded audio plan (empty = OK or no plan)."""
    from hfvg import audio_mix

    path = audio_mix.audio_plan_path(episode_id)
    if not path.is_file():
        return []
    try:
        plan = audio_mix.load_plan_file(path)
    except audio_mix.AudioInputError:
        return []  # the delivery stage reports an unreadable plan (load_audio_plan)
    return plan_voice_problems(plan, live)

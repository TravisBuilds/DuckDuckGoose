"""Activity implementations."""

from hfvg.activities.audio import generate_music, generate_sfx, generate_voiceover
from hfvg.activities.audio_stage import (
    burn_logo_overlay,
    create_parity_cli_rebuild,
    draft_vo_script,
    extract_frame_at_timecode,
    generate_music as generate_music_stage,
    generate_sfx_library,
    generate_vo_take,
    mix_audio_stems,
    slice_vo_lines,
)
from hfvg.activities.generation import await_job, submit_clip_job, submit_still_job
from hfvg.activities.media import mix_audio, render_edit, trim_clips
from hfvg.activities.posting import post_to_platform
from hfvg.activities.qc import review_clip, review_still

__all__ = [
    "submit_still_job",
    "submit_clip_job",
    "await_job",
    "review_still",
    "review_clip",
    "trim_clips",
    "render_edit",
    "mix_audio",
    "generate_voiceover",
    "generate_sfx",
    "generate_music",
    "post_to_platform",
    # Audio Stage (new)
    "draft_vo_script",
    "extract_frame_at_timecode",
    "generate_vo_take",
    "slice_vo_lines",
    "generate_music_stage",
    "generate_sfx_library",
    "mix_audio_stems",
    "burn_logo_overlay",
    "create_parity_cli_rebuild",
]

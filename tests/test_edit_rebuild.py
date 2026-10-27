"""GAP-29 (past run G_FINAL_CUT): an edit_plan.json written after the first edit (a reused
clip and a dissolve) could not re-assemble the mute edit, because the assembly signature only sees
the clip outcomes. request_edit_rebuild(reason) + POST /api/episodes/{id}/assembly/rebuild bump it.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio.testing import WorkflowEnvironment

from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
from tests.lint_helpers import lint_ok
from tests.test_audio_delivery_workflow import PROMPT, _delivered, write_audio
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (  # noqa: F401
    MOVING, ClipFactory, _to_final_cut, _worker, needs_ffmpeg, no_counter,
)


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_edit_rebuild_reassembles_then_redelivers(dry_env, tmp_path, no_counter):
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    write_audio(tmp_path, "ep95")
    h = Harness({})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
            _, handle, state = await _to_final_cut(env, h, tmp_path, "ep95", lint_ok(PROMPT))
            state = await _wait(handle, _delivered, timeout=180, what="delivery 1")
            assert state["assembly"]["edit_version"] == 1 and state["delivery"]["delivery_version"] == 1
            clip_calls = len(h.clip_calls) if hasattr(h, "clip_calls") else None
            await handle.signal("request_edit_rebuild", "   ")  # ignored: no reason
            await asyncio.sleep(1.0)
            state = await handle.query("get_state")
            assert state["assembly"]["edit_version"] == 1 and state["edit_rebuilds"] == []
            assert any("edit rebuild ignored" in r for r in state["final_cut_refusals"])
            await handle.signal("request_edit_rebuild", "Studio: V06 reuse + V23->V24 dissolve in edit_plan")
            state = await _wait(handle, lambda s: s["assembly"]["edit_version"] == 2
                                and s["delivery"]["delivery_version"] == 2 and s["delivery"]["phase"] == "done",
                                timeout=180, what="edit 2 + delivery 2")
            assert state["edit_rebuilds"][0]["after_edit_version"] == 1
            assert state["assembly"]["stale"] is False and state["delivery"]["due"] is False
            if clip_calls is not None:
                assert len(h.clip_calls) == clip_calls  # picture only: no new generation
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=60)

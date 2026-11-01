"""GAP-79b (ep05v2 r13 v11): after reopen-from-saved the v10 clips are clip take 0 with no
clip_url (the provider URL died with the Temporal history). Keeping that take after a failed
re-take must be allowed; a take with neither a URL nor a saved-state origin is still refused."""
from types import SimpleNamespace

import pytest

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


def _wf(take0):
    wf = EpisodeWorkflowV2.__new__(EpisodeWorkflowV2)
    wf.assembly_sig = "sig"
    wf.passed_gates = []
    wf.pending_clip_retries = {}
    wf.state = SimpleNamespace(shot_decisions={}, failed_shots=[])
    wf.shot_outcomes = {"V15": {"status": "needs_review", "clip_url": "https://x/c2.mp4",
                                "clip_path": "out/V15/clip_v2002.mp4"}}
    wf.clip_takes = {"V15": {0: take0, 1: {"status": "needs_review", "clip_url": "https://x/c1.mp4",
                                           "clip_path": "out/V15/clip_v1002.mp4"}}}
    return wf


SAVED = {"status": "completed", "clip_url": None, "clip_path": "out/V15/clip_v2.mp4",
         "restored_from": "saved_state"}


def test_saved_state_take_can_be_kept():
    assert _wf(dict(SAVED))._clip_review_problem("V15", "keep v10", None, 0) is None


@pytest.mark.parametrize("change", [
    {"restored_from": None},            # no URL and not from saved state
    {"status": "unknown"},              # a saved-state file that was never the accepted clip
    {"clip_path": None},                # no local file
])
def test_unlanded_take_still_refused(change):
    take = {**SAVED, **change}
    why = _wf(take)._clip_review_problem("V15", "keep", None, 0)
    assert why and "no landed clip" in why


def test_landed_take_unchanged():
    assert _wf(dict(SAVED))._clip_review_problem("V15", "keep", None, 1) is None

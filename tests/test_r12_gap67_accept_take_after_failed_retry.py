"""GAP-67 (ep05v2 r12, 28 Oct 2026): V22's clip retry 3 was refused inside the child (prompt over
Kling's 2500 chars after lock injection, no spend). The child returned 'failed' with no clip, so the
shot's outcome became failed: V22 dropped out of the auto edit (slots V22: shot not in this edit) and
accept_clip_review(take=2) for its earlier landed take was refused ("status is 'failed'").
Keeping an earlier landed take must be possible when the latest retry landed nothing."""

from __future__ import annotations

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


class _S:
    shot_decisions: dict = {}


def _wf(outcome: dict, takes: dict | None = None) -> EpisodeWorkflowV2:
    wf = object.__new__(EpisodeWorkflowV2)
    wf.assembly_sig = "sig"
    wf.passed_gates = set()
    wf.pending_clip_retries = {}
    wf.state = _S()
    wf.shot_outcomes = {"V22": outcome}
    wf.clip_takes = {"V22": takes or {}}
    return wf


TAKES = {2: {"clip_url": "https://x/2.mp4", "clip_path": "outputs/V22/clip_v2005.mp4", "status": "needs_review"}}
FAILED_NO_CLIP = {"status": "failed", "error": "Clip retry child error: prompt too long", "still_url": "s",
                  "clip_url": None}


def test_earlier_take_can_be_kept_after_a_retry_that_landed_nothing():
    wf = _wf(FAILED_NO_CLIP, TAKES)
    assert wf._clip_review_problem("V22", "restore take 2", None, 2) is None
    # an unknown take is still refused
    assert "no clip take 7" in wf._clip_review_problem("V22", "x", None, 7)


def test_failed_without_a_take_is_still_refused():
    wf = _wf(FAILED_NO_CLIP, TAKES)
    assert "not 'needs_review'" in wf._clip_review_problem("V22", "keep latest", None, None)


def test_failed_outcome_with_a_clip_is_still_refused_even_with_a_take():
    wf = _wf({**FAILED_NO_CLIP, "clip_url": "https://x/3.mp4"}, TAKES)
    assert "not 'needs_review'" in wf._clip_review_problem("V22", "x", None, 2)


def test_pre_patch_histories_keep_the_old_refusal():
    wf = _wf(FAILED_NO_CLIP, TAKES)
    assert "not 'needs_review'" in wf._clip_review_problem("V22", "x", None, 2, failed_retry_take=False)


def test_handler_gates_the_relaxation_behind_a_replay_patch():
    import inspect
    src = inspect.getsource(EpisodeWorkflowV2.accept_clip_review)
    assert 'workflow.patched("gap67-accept-take-after-failed-retry-v1")' in src

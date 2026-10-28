"""Env-gated per-shot Seedance override (HFVG_SEEDANCE_OVERRIDE_SHOTS) (owner-approved per-shot head-to-head test).

Default (unset) routing must be unchanged: multi-animal and ``wet`` shots are refused for Seedance.
Only listed shot ids may route to Seedance; duration rules are never overridden. Synthetic only.
"""

import httpx
import pytest
import respx

from hfvg.routing import RoutingError, plan_route, seedance_override_ids
from tests.lint_helpers import acapture, capture, lint_meta, lint_ok
from tests.test_seedance_routing import _body, _live_env

ENV = "HFVG_SEEDANCE_OVERRIDE_SHOTS"
SD_URL = "https://api.higgsfield.ai/bytedance/seedance-2.5/image-to-video"


def _shot(shot_id=None, count=3, tags=("wet",), duration=6, model="seedance"):
    s = {"model": model, "tier": "final", "duration": duration, "tags": list(tags), "count": count}
    if shot_id is not None:
        s["shot_id"] = shot_id
    return s


def test_override_unset_keeps_both_refusals(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert seedance_override_ids() == frozenset()
    multi = capture(RoutingError, plan_route, _shot("TA_S", count=3, tags=()))
    wet = capture(RoutingError, plan_route, _shot("TA_S", count=1, tags=("wet",)))
    assert multi is not None and "Multi-animal" in str(multi)
    assert wet is not None and "wet" in str(wet)


def test_override_empty_or_blank_is_off(monkeypatch):
    for raw in ("", " ", ",", " , ,"):
        monkeypatch.setenv(ENV, raw)
        assert seedance_override_ids() == frozenset()
        assert capture(RoutingError, plan_route, _shot("TA_S")) is not None


def test_override_set_lets_only_listed_ids_route_to_seedance(monkeypatch):
    monkeypatch.setenv(ENV, " TA_S , TB_S ")
    assert seedance_override_ids() == frozenset({"TA_S", "TB_S"})
    a = plan_route(_shot("TA_S", count=3, tags=("wet",), duration=6))
    b = plan_route(_shot("TB_S", count=2, tags=("wet",), duration=5))
    assert (a.model, a.resolution) == ("seedance", "1080p")
    assert (b.model, b.resolution) == ("seedance", "1080p")


def test_override_unlisted_shot_still_refused(monkeypatch):
    monkeypatch.setenv(ENV, "TA_S,TB_S")
    multi = capture(RoutingError, plan_route, _shot("V13", count=3, tags=()))
    wet = capture(RoutingError, plan_route, _shot("V14", count=1, tags=("wet",)))
    no_id = capture(RoutingError, plan_route, _shot(None, count=3, tags=("wet",)))
    near = capture(RoutingError, plan_route, _shot("ta_s", count=3, tags=("wet",)))  # exact match only
    prefix = capture(RoutingError, plan_route, _shot("TA_S2", count=3, tags=("wet",)))
    assert all(e is not None for e in (multi, wet, no_id, near, prefix))


def test_override_never_bypasses_duration_rule(monkeypatch):
    monkeypatch.setenv(ENV, "TA_S")
    short = capture(RoutingError, plan_route, _shot("TA_S", duration=3))
    assert short is not None and "under 4s" in str(short)


def test_override_does_not_change_kling_or_fallback_validation(monkeypatch):
    monkeypatch.setenv(ENV, "TA_S")
    assert plan_route(_shot("TA_S", model="kling")).model == "kling"
    # moderation_recovery.validate_fallback passes no shot_id -> never overridden
    from hfvg.activities.moderation_recovery import validate_fallback
    errs = validate_fallback({"shot_id": "TA_S", "tier": "final", "duration": 6, "tags": ["wet"], "count": 3,
                              "prompt": "x", "start_image_url": "https://example.com/s.jpg"}, "seedance")
    assert errs and errs[0].startswith("routing:")


@pytest.mark.asyncio
@respx.mock
async def test_clip_activity_passes_shot_id_override_on_and_off(tmp_path, monkeypatch):
    """The live clip activity forwards shot_id: listed id reaches Seedance at 1080p; unset refuses pre-spend."""
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_env(tmp_path, monkeypatch)
    sub = respx.post(SD_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-ovr-1"}))
    meta = lint_meta(count=3, characters=["R", "H", "M"])

    monkeypatch.delenv(ENV, raising=False)
    off = await acapture(RoutingError, submit_clip_job_enforced(
        "ep99", "TA_S", "https://example.com/s.jpg", lint_ok("Synthetic clip", 3), 6.0, 1,
        "final", meta, "seedance", ["wet"],
    ))
    assert off is not None and sub.call_count == 0
    assert (await BudgetLedger(db_path).get_line_status("ep99", "L4_video"))["reserved"] == 0

    monkeypatch.setenv(ENV, "TA_S")
    other = await acapture(RoutingError, submit_clip_job_enforced(
        "ep99", "V13", "https://example.com/s.jpg", lint_ok("Synthetic clip", 3), 6.0, 1,
        "final", meta, "seedance", ["wet"],
    ))
    assert other is not None and sub.call_count == 0

    result = await submit_clip_job_enforced(
        "ep99", "TA_S", "https://example.com/s.jpg", lint_ok("Synthetic clip", 3), 6.0, 1,
        "final", meta, "seedance", ["wet"],
    )
    assert sub.call_count == 1
    assert _body(sub)["resolution"] == "1080p"
    assert result["reserved_amount"] == 6_823_200  # 6 s x 1.1372 USD/s (rate table, confirmed live)

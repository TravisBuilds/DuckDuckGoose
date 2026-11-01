"""Ep11: per-shot Seedance resolution override (shots.json ``seedance_resolution``) + live Kling rate."""
import pytest

from hfvg.routing import RoutingError, plan_route, seedance_resolution_for
from hfvg.shot_data import ShotDataError, parse_shots


def _shot(**kw):
    base = {"id": "SH01", "count": 1, "characters": ["D"], "wet": False, "model": "seedance",
            "duration": 6, "end_frame": False, "prompt": "p", "ref_keys": []}
    base.update(kw)
    return base


def test_route_uses_override_and_default():
    r = plan_route({"model": "seedance", "tier": "final", "duration": 6, "count": 1, "resolution": "720p"})
    assert r.resolution == "720p"
    assert plan_route({"model": "seedance", "tier": "final", "duration": 6, "count": 1}).resolution == "1080p"
    assert plan_route({"model": "seedance", "tier": "draft", "duration": 6, "count": 1}).resolution == "480p"
    # Kling ignores it (fallback Seedance -> Kling keeps working)
    assert plan_route({"model": "kling", "tier": "final", "duration": 5, "count": 1, "resolution": "720p"}).model == "kling"
    with pytest.raises(RoutingError):
        seedance_resolution_for("final", "4k")


def test_shots_json_parses_and_validates():
    plans = parse_shots({"shots": [_shot(seedance_resolution="720p")]})
    assert plans["SH01"]["params"]["seedance_resolution"] == "720p"
    assert plans["SH01"]["shot_meta"]["seedance_resolution"] == "720p"
    assert parse_shots({"shots": [_shot()]})["SH01"]["params"]["seedance_resolution"] is None
    with pytest.raises(ShotDataError):
        parse_shots({"shots": [_shot(seedance_resolution="900p")]})
    with pytest.raises(ShotDataError):
        parse_shots({"shots": [_shot(model="kling", seedance_resolution="720p")]})


def test_estimates_follow_override():
    from hfvg.estimates import dry_shot_estimate, seedance_resolution
    from hfvg.pricing import clip_estimate_usd_micros
    assert clip_estimate_usd_micros("seedance", None, 6, "720p") == 6 * 462_200
    assert clip_estimate_usd_micros("seedance", None, 6) == 6 * 1_137_200
    assert seedance_resolution(None, "720p") == "720p"
    est = dry_shot_estimate({"shot_id": "SH01", "params": {"model": "seedance", "duration": 5,
                                                          "seedance_resolution": "720p"}}, pre_approved=True)
    assert est["clip_usd_micros"] == 5 * 462_200
    assert est["still_usd_micros"] == 0


def test_clip_expect_dims_follow_override():
    from hfvg.qc.clip_expect import expected_clip_dims
    d = expected_clip_dims({"model": "seedance", "aspect_ratio": "9:16", "seedance_resolution": "720p"})
    assert d["resolution"] == "720x1280"
    assert expected_clip_dims({"model": "seedance", "aspect_ratio": "9:16"})["resolution"] == "1080x1920"


def test_ep11_first_pass_total_matches_live_rate():
    from hfvg.pricing import clip_estimate_usd_micros
    durations = [6, 5, 5, 5, 5, 5, 5, 5, 5, 6]
    total = sum(clip_estimate_usd_micros("seedance", None, d, "720p") for d in durations)
    assert total == 52 * 462_200  # $24.0344


def test_kling_rate_table_is_live_rate(monkeypatch):
    monkeypatch.delenv("MODEL_USD_PER_SECOND_JSON", raising=False)
    from hfvg.config import _load_rate_table
    assert _load_rate_table()["kling-3.0-pro"]["default"] == 112_000  # live 2026-10-27: 5 s = $0.560


def test_beatmap_accepts_multi_letter_shot_ids(tmp_path):
    """Ep11 ids are SH01..SH10: the beatmap row regex took only one letter before the digits."""
    from hfvg.episode_parser import parse_beatmap
    md = tmp_path / "BEATMAP.md"
    md.write_text(
        "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | SH01 | 6.5 (6) | Forest | Day | D | cone | hook | Cut | Cut | S |\n"
        "| 2 | A02 | 5 (5) | Forest | Day | — | macro | pop | Cut | Cut | S |\n")
    shots = parse_beatmap(str(md))
    assert [s["shot_id"] for s in shots] == ["SH01", "A02"]
    assert shots[0]["gen_time"] == 6.0 and shots[0]["characters"] == ["D"]
    assert shots[1]["characters"] == []


class _Policy:
    def __init__(self, lines, usd):
        self._lines, self._usd = lines, usd

    def get_higgsfield_budget(self, episode):
        return {"lines": self._lines}

    def get_usd_line_caps(self, episode):
        return self._usd


def test_effective_line_caps_use_policy_usd_caps():
    from hfvg.budget import effective_line_caps_usd_micros, DEFAULT_LINE_CAPS_USD_MICROS
    pol = _Policy({"L1_refs": 1, "L2_drafts": 1, "L3_final_stills": 1, "L4_video": 1},
                  {"L1_refs": 0.25, "L2_drafts": 0.25, "L3_final_stills": 7.0, "L4_video": 30.9})
    caps = effective_line_caps_usd_micros(pol, "ep11")
    assert caps == {"L1_refs": 250_000, "L2_drafts": 250_000, "L3_final_stills": 7_000_000,
                    "L4_video": 30_900_000}
    assert sum(caps.values()) == 38_400_000
    # no policy lines -> the defaults
    assert effective_line_caps_usd_micros(_Policy({}, {}), "ep11") == DEFAULT_LINE_CAPS_USD_MICROS


def test_preflight_budget_check_uses_policy_caps(monkeypatch, tmp_path):
    """A $40 cap / 96% stop episode whose policy caps sum to $38.40 passes the cap check
    (it used to be refused because the unused $44 defaults exceeded the cap)."""
    import asyncio
    import hfvg.budget as budget
    from hfvg.activities import preflight as pf
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    monkeypatch.setenv("EPISODE_STOP_PERCENT", "96")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "s.db"))
    pol = _Policy({"L1_refs": 1, "L2_drafts": 1, "L3_final_stills": 1, "L4_video": 1},
                  {"L1_refs": 0.25, "L2_drafts": 0.25, "L3_final_stills": 7.0, "L4_video": 30.9})
    monkeypatch.setattr(budget, "load_policy", lambda *a, **k: pol)
    caps = budget.effective_line_caps_usd_micros(pol, "ep11")
    s = budget.get_budget_settings()
    assert sum(caps.values()) <= s.episode_stop_usd_micros <= s.episode_cap_usd_micros
    src = open(pf.__file__).read()
    assert "effective_line_caps_usd_micros(ledger.policy, episode_id)" in src

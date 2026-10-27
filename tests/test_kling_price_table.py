"""Kling 3.0 pro price table == the live provider estimate (re-checked 2026-10-27).

The table said 0.056 USD/s while POST /estimate/kling-video/v3.0/pro/image-to-video returned
0.112 USD/s (5 s = 0.560 USD, 8.960 credits). Every offline/dry/fallback Kling cost must follow
the live value so G1 dry tables and fallbacks never under-reserve by half again.
"""
import json
from pathlib import Path

import pytest

from hfvg.config import config, _load_rate_table
from hfvg.credit_pricing import estimate_clip_credits
from hfvg.pricing import DRY_CLIP_USD_PER_SECOND_MICROS, clip_estimate_usd_micros, rate_table_estimate

# Live POST /estimate responses (usd strings), 2026-10-27 03:57 CET
LIVE_USD = {3: "0.336", 4: "0.448", 5: "0.560", 6: "0.672", 12: "1.344"}
LIVE_CREDITS_PER_S = 1.792


def test_rate_table_is_live_kling_pro_rate(monkeypatch):
    monkeypatch.delenv("MODEL_USD_PER_SECOND_JSON", raising=False)
    assert _load_rate_table()["kling-3.0-pro"] == {"default": 112_000}
    assert config.MODEL_USD_PER_SECOND_MICROS["kling-3.0-pro"]["default"] == 112_000


@pytest.mark.parametrize("seconds,usd", sorted(LIVE_USD.items()))
def test_table_matches_live_estimate_per_duration(seconds, usd):
    from hfvg.pricing import usd_to_micros
    assert rate_table_estimate("kling-3.0-pro", None, seconds).usd_micros == usd_to_micros(usd)
    assert clip_estimate_usd_micros("kling", None, seconds) == usd_to_micros(usd)


def test_dry_constant_follows_table():
    assert DRY_CLIP_USD_PER_SECOND_MICROS == config.MODEL_USD_PER_SECOND_MICROS["kling-3.0-pro"]["default"]


def test_kling_credits_use_pro_rate():
    assert estimate_clip_credits("kling_3.0", 5) == pytest.approx(5 * LIVE_CREDITS_PER_S)


def test_model_caps_observed_cost_matches():
    caps = json.loads((Path(__file__).resolve().parents[1] / "hfvg/data/model_caps.json").read_text())
    found = []

    def walk(o):
        if isinstance(o, dict):
            obs = (o.get("cost") or {}).get("observed") if isinstance(o.get("cost"), dict) else None
            if obs and "kling" in json.dumps(o.get("cost")).lower() + json.dumps(o)[:0]:
                found.append(obs)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(caps)
    usd = [f["usd_per_s"] for f in found if "obs_kling_canary" in f.get("sources", [])]
    assert usd and all(u == 0.112 for u in usd)


def test_ep05full_first_pass_plus_reserve_under_cap():
    """Studio's Ep05 FULL plan: 94 s Kling + 20 stills first pass; $20 reserve; hard cap $40."""
    clip = sum(clip_estimate_usd_micros("kling", None, s)
               for s in (6, 4, 6, 4, 4, 6, 5, 4, 4, 3, 4, 4, 4, 5, 5, 5, 4, 5, 12))
    assert clip == 10_528_000
    first_pass = clip + 20 * 60_000
    assert first_pass + 20_000_000 < 40_000_000

"""GAP-19 (ep05full 2026-10-27): the phone copy was always encoded at the picture size. A 117 s
episode under a 5 MB cap leaves ~270 kbps of video, which bands badly at 1080x1920, so Studio's
plan asks for a 720x1280 phone copy. phone.width/height (even ints, both or none) scale the phone
encode AFTER the subtitle burn; the manifest records the size and delivery QC checks it."""

from __future__ import annotations

import pytest

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError, delivery_qc, mix_and_deliver, normalize_plan
from tests.test_audio_delivery import _ok, episode, pytestmark  # noqa: F401


def test_phone_dims_validated():
    assert normalize_plan({"phone": {"width": 720, "height": 1280}}, "/tmp")["phone"]["width"] == 720
    assert normalize_plan({}, "/tmp")["phone"].get("width") is None
    for bad in ({"width": 720}, {"width": 721, "height": 1280}, {"width": 720.5, "height": 1280},
                {"width": 8, "height": 1280}):
        with pytest.raises(AudioInputError):
            normalize_plan({"phone": bad}, "/tmp")


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_phone_copy_is_downscaled_and_checked(tmp_path):
    mute, plan = episode(tmp_path)  # 432x768 picture
    plan["phone"] = {**plan["phone"], "width": 216, "height": 384}
    m = await mix_and_deliver("ep01", str(mute), plan)
    v = [s for s in (await audio_mix.probe(m["paths"]["phone"]))["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (216, 384)
    assert (m["phone"]["width"], m["phone"]["height"]) == (216, 384)
    master = [s for s in (await audio_mix.probe(m["paths"]["nosubs"]))["streams"] if s["codec_type"] == "video"][0]
    assert (master["width"], master["height"]) == (432, 768), "only the phone copy is scaled"
    qc = await delivery_qc(m, plan)
    assert _ok(qc, "phone_dims")
    # a manifest claiming another size fails the check
    bad = {**m, "phone": {**m["phone"], "width": 720, "height": 1280}}
    assert not _ok(await delivery_qc(bad, plan), "phone_dims")

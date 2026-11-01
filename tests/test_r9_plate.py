"""GAP-52 / GAP-53 (ep05v2 r9, 2026-10-28): Studio option (d) -- the scale shots (V02/V03/V04/V06)
get a code-composited start plate (clean plate + capybara cutouts sized by a measured rail/counter
reference and the duck rule, contact shadows, exact headcount) and are re-animated from it.
hfvg.plate_prep makes cutouts / clean plates (rembg / LaMa injected here as mocks),
hfvg.plate_composite renders + checks the plate, and recompose_shot takes the passed plate as the
new composition's start still (no still spend; still parks for recompose_decision; clip only).
No network, no spend."""
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hfvg import plate_composite as pc
from hfvg import plate_prep as pp


# ---------------------------------------------------------------- fixtures

def _plate(tmp_path, size=(360, 640)):
    p = tmp_path / "plate.png"
    Image.new("RGB", size, (120, 110, 100)).save(p)
    return p


def _cutout(tmp_path, name, w, h, color=(150, 100, 60)):
    im = Image.new("RGBA", (w + 10, h + 10), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (w, h), color + (255,)), (5, 5))
    p = tmp_path / f"{name}.png"
    im.save(p)
    return p


def _ref(p):
    return {"path": str(p), "sha256": pc.sha256_file(p)}


def _spec(tmp_path, **over):
    plate = _plate(tmp_path)
    a = _cutout(tmp_path, "adult", 200, 100)
    k = _cutout(tmp_path, "pup", 100, 50)
    spec = {"shot_id": "A01", "plate": dict(_ref(plate), source="test"),
            # rail: top 300, base 500 at both ends; horizon 400 -> at foot_y 500 the rail is 200 px
            "reference": {"kind": "rail", "points": [[0, 300, 500], [360, 300, 500]], "horizon_y": 400},
            "duck_height_frac": 0.4, "count": 2,
            "layers": [{"name": "Rowan", "role": "adult", "cutout": _ref(a), "x": 120, "foot_y": 500,
                        "height_frac_of_duck": 2.0},
                       {"name": "Mochi", "role": "pup", "cutout": _ref(k), "x": 260, "foot_y": 500,
                        "height_frac_of_duck": 1.0}],
            "shadow": {"opacity": 0.6, "blur": 2}}
    spec.update(over)
    return spec


# ---------------------------------------------------------------- GAP-52 composite

def test_compose_sizes_layers_from_the_reference_and_passes(tmp_path):
    m = pc.compose(_spec(tmp_path), tmp_path / "out.png")
    assert m["passed"] and m["count_ok"]
    rowan, mochi = m["layers"]
    assert rowan["ref_h_at_depth"] == 200.0 and rowan["duck_h"] == 80.0
    assert rowan["height_px"] == 160.0 and rowan["ratio_to_duck"] == 2.0 and rowan["frac_of_ref"] == 0.8
    assert rowan["top_y"] == 340.0 and rowan["checks"]["back_below_ref_top_in_image"]
    assert mochi["height_px"] == 80.0 and mochi["ratio_to_duck"] == 1.0
    assert rowan["box"] == [-40, 340, 280, 500]  # 200x100 cutout -> 320x160, centred on x, feet on foot_y
    out = np.asarray(Image.open(tmp_path / "out.png"))
    assert out.shape == (640, 360, 3)
    assert tuple(out[420, 120]) == (150, 100, 60)          # the layer
    assert out[505, 300].sum() < np.array([120, 110, 100]).sum()   # contact shadow just under the feet
    assert tuple(out[50, 50]) == (120, 110, 100)           # plate untouched away from the layers
    assert m["sha256"] == pc.sha256_file(tmp_path / "out.png")
    saved = json.loads(Path(str(tmp_path / "out.png") + ".manifest.json").read_text())
    assert saved["passed"] and saved["sha256"] == m["sha256"]


def test_depth_scales_the_reference(tmp_path):
    rows = pc.plan_layers(pc.validate_spec(_spec(tmp_path, layers=[
        {"name": "Near", "role": "pup", "cutout": _ref(_cutout(tmp_path, "pup", 100, 50)), "x": 100,
         "foot_y": 600, "height_frac_of_duck": 1.0}], count=1)), {"Near": (100, 50)})
    assert rows[0]["ref_h_at_depth"] == 400.0 and rows[0]["height_px"] == 160.0  # twice as far below the horizon


def test_too_big_adult_or_wrong_headcount_does_not_pass(tmp_path):
    spec = _spec(tmp_path)
    spec["layers"][0]["height_frac_of_duck"] = 2.5     # outside adult_to_duck [1.8, 2.2] and above 0.9 of the rail
    m = pc.compose(spec, tmp_path / "big.png")
    assert not m["passed"] and not m["layers"][0]["passed"]
    assert not m["layers"][0]["checks"]["ratio_to_duck"] and not m["layers"][0]["checks"]["back_below_ref_at_depth"]
    m = pc.compose(_spec(tmp_path, count=3), tmp_path / "count.png")
    assert not m["passed"] and not m["count_ok"]
    spec = _spec(tmp_path)
    spec["layers"][1]["height_frac_of_duck"] = 1.3     # pup bigger than the duck
    assert not pc.compose(spec, tmp_path / "pup.png")["passed"]
    spec = _spec(tmp_path, rules={"adult_back_max_frac": 0.9, "adult_to_duck": [1.8, 2.2],
                                  "pup_to_duck": [0.85, 1.15]})
    spec["layers"][0]["cutout"] = _ref(_cutout(tmp_path, "tiny", 40, 20))   # would need 8x upscale
    m = pc.compose(spec, tmp_path / "soft.png")
    assert not m["passed"] and not m["layers"][0]["checks"]["upscale"]


def test_back_must_also_be_below_the_rail_top_in_image(tmp_path):
    spec = _spec(tmp_path, rules={"adult_back_max_frac": 0.9, "adult_to_duck": [1.8, 2.2],
                                  "pup_to_duck": [0.85, 1.15]})
    spec["layers"][0]["foot_y"] = 640   # close to camera: 0.8 of the rail at depth but its back pokes above the rail
    rows = pc.plan_layers(pc.validate_spec(spec), {"Rowan": (200, 100), "Mochi": (100, 50)})
    assert rows[0]["checks"]["back_below_ref_at_depth"] and not rows[0]["checks"]["back_below_ref_top_in_image"]
    assert not rows[0]["passed"]


def test_compose_verifies_input_shas(tmp_path):
    spec = _spec(tmp_path)
    spec["plate"]["sha256"] = "0" * 64
    with pytest.raises(pc.PlateSpecError, match="plate sha256"):
        pc.compose(spec, tmp_path / "x.png")
    spec = _spec(tmp_path)
    spec["layers"][1]["cutout"]["sha256"] = "f" * 64
    with pytest.raises(pc.PlateSpecError, match="cutout Mochi"):
        pc.compose(spec, tmp_path / "x.png")


@pytest.mark.parametrize("mutate,msg", [
    (lambda s: s.pop("count"), "headcount"),
    (lambda s: s["reference"].update(kind="wall"), "rail or counter"),
    (lambda s: s["reference"].update(points=[[0, 300, 500]]), "two"),
    (lambda s: s["reference"].update(points=[[0, 500, 300], [360, 300, 500]]), "below top"),
    (lambda s: s["reference"].update(horizon_y=600), "horizon"),
    (lambda s: s["layers"][0].update(role="kit"), "adult or pup"),
    (lambda s: s["layers"][1].update(name="Rowan"), "duplicate"),
    (lambda s: s["layers"][0].update(height_frac_of_duck=0), "> 0"),
    (lambda s: s["layers"][0]["cutout"].update(sha256="abc"), "64 hex"),
    (lambda s: s.update(duck_height_frac=3), "duck_height_frac"),
])
def test_spec_validation(tmp_path, mutate, msg):
    spec = _spec(tmp_path)
    mutate(spec)
    with pytest.raises(pc.PlateSpecError, match=msg):
        pc.validate_spec(spec)


def test_load_passed_manifest_guards(tmp_path):
    m = pc.compose(_spec(tmp_path), tmp_path / "ok.png")
    mp = Path(m["path"] + ".manifest.json")
    assert pc.load_passed_manifest(mp, "A01")["sha256"] == m["sha256"]
    with pytest.raises(pc.PlateSpecError, match="not A02"):
        pc.load_passed_manifest(mp, "A02")
    bad = _spec(tmp_path, count=5)
    mb = pc.compose(bad, tmp_path / "bad.png")
    with pytest.raises(pc.PlateSpecError, match="did not pass"):
        pc.load_passed_manifest(mb["path"] + ".manifest.json", "A01")
    Image.new("RGB", (360, 640), (0, 0, 0)).save(tmp_path / "ok.png")   # edited after the checks
    with pytest.raises(pc.PlateSpecError, match="changed"):
        pc.load_passed_manifest(mp, "A01")
    with pytest.raises(pc.PlateSpecError, match="no plate manifest"):
        pc.load_passed_manifest(tmp_path / "nope.json", "A01")


# ---------------------------------------------------------------- GAP-52 prep (mocked matte / inpaint)

def _two_blobs_still(tmp_path):
    im = Image.new("RGB", (200, 100), (10, 10, 10))
    im.paste((200, 0, 0), (20, 20, 80, 80))     # big subject
    im.paste((0, 200, 0), (120, 40, 130, 50))   # small stray blob
    p = tmp_path / "still.png"
    im.save(p)
    return p


def _remover(crop):
    a = np.asarray(crop).max(axis=2) > 100
    return Image.fromarray((a * 255).astype(np.uint8), "L")


def test_matte_keeps_the_largest_subject_and_trims(tmp_path):
    s = _two_blobs_still(tmp_path)
    m = pp.matte(s, "0,0,200,100", tmp_path / "cut.png", "test", remover=_remover)
    assert m["bbox_in_still"][:2] == [20, 20] and m["size"][0] <= 70   # stray blob dropped
    assert m["still_sha256"] == pp.sha256_file(s) and m["sha256"] == pp.sha256_file(tmp_path / "cut.png")
    assert json.loads((tmp_path / "cut.json").read_text())["source"] == "test"


def test_matte_exclude_polygon_removes_a_neighbour(tmp_path):
    s = _two_blobs_still(tmp_path)
    m = pp.matte(s, "0,0,200,100", tmp_path / "cut.png", "t", remover=_remover,
                 exclude=[[[0, 0], [60, 0], [60, 100], [0, 100]]])
    assert m["bbox_in_still"][0] >= 60 and m["exclude"]


def test_union_mask_requires_cutouts_from_the_same_still(tmp_path):
    s = _two_blobs_still(tmp_path)
    m = pp.matte(s, "0,0,200,100", tmp_path / "cut.png", "t", remover=_remover)
    mask = pp.union_mask((200, 100), [m], grow_px=4, still_sha256=pp.sha256_file(s))
    a = np.asarray(mask)
    assert a[50, 50] == 255 and a[5, 190] == 0 and a[19, 50] == 255   # grown by the dilation
    with pytest.raises(pp.PlatePrepError, match="not cut from this still"):
        pp.union_mask((200, 100), [m], still_sha256="0" * 64)


def test_clean_keeps_unmasked_pixels_bit_exact(tmp_path):
    s = _two_blobs_still(tmp_path)
    mask = Image.new("L", (200, 100), 0)
    mask.paste(255, (10, 10, 90, 90))
    mask.save(tmp_path / "mask.png")
    m = pp.clean(s, tmp_path / "mask.png", tmp_path / "clean.png", "t",
                 painter=lambda im, mk: Image.new("RGB", im.size, (1, 2, 3)))
    a, src = np.asarray(Image.open(tmp_path / "clean.png")), np.asarray(Image.open(s))
    assert tuple(a[50, 50]) == (1, 2, 3)
    keep = np.asarray(mask) < 128
    assert (a[keep] == src[keep]).all()
    assert 0 < m["masked_frac"] < 1


# ---------------------------------------------------------------- GAP-53 workflow argument

def test_recompose_plate_problem():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W
    assert W.recompose_plate_problem(None) is None
    assert W.recompose_plate_problem({"path": "/x.png", "sha256": "a" * 64}) is None
    assert "object" in W.recompose_plate_problem("x")
    assert "path" in W.recompose_plate_problem({"sha256": "a" * 64})
    assert "sha256" in W.recompose_plate_problem({"path": "/x", "sha256": "A" * 64})
    assert "unknown" in W.recompose_plate_problem({"path": "/x", "sha256": "a" * 64, "prompt": "y"})


from tests.test_recompose_and_edit import _to_final_cut, _worker  # noqa: E402
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: E402,F401
from tests.test_picture_assembly_round3 import MOVING, ClipFactory, needs_ffmpeg, no_counter  # noqa: E402,F401
from tests.lint_helpers import lint_ok  # noqa: E402


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_recompose_with_plate_spends_no_still_and_parks(dry_env, tmp_path, no_counter):
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.testing import WorkflowEnvironment
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "rowan.png"
    ref.write_bytes(b"png")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep96")
            plate_png = tmp_path / "data" / "episodes" / "ep96" / "plates" / "A01" / "plate.png"
            plate_png.parent.mkdir(parents=True)
            Image.new("RGB", (72, 128), (90, 80, 70)).save(plate_png)
            plate = {"path": str(plate_png), "sha256": pc.sha256_file(plate_png), "approved_by": "Studio r9"}
            prompt = lint_ok("A duck waddles through the snow")
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("recompose_shot", args=[
                    "A01", "r", prompt, [str(ref)], ["rowan"], None, None, None, {"path": "x", "sha256": "q"}])
            assert "sha256" in str(refused.value.cause)
            stills_before = len(h.still_calls)
            await handle.execute_update("recompose_shot", args=[
                "A01", "Studio r9 option (d): composited plate", prompt, [str(ref)], ["rowan"], None,
                None, None, plate])
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="plate still parked")
            assert len(h.still_calls) == stills_before   # no still job: the plate is the start still
            plans = await handle.query("recompose_still_plans")
            v = plans["A01"]["version"]
            landed_still = tmp_path / "data" / "episodes" / "ep96" / "outputs" / "A01" / f"still_v{v}.png"
            assert landed_still.is_file() and pc.sha256_file(landed_still) == plate["sha256"]
            await handle.execute_update("recompose_decision", args=["A01", True, "plate scale ok"])
            landed = await _child_result(env, "ep96-shot-A01-recompose1", timeout=90)
            assert landed.get("clip_url"), landed
            assert len(h.still_calls) == stills_before
            await handle.terminate("test done")


# ---------------------------------------------------------------- GAP-53 API route

from tests.test_recompose_meta_patch import META, _Client, _Handle, episode  # noqa: E402,F401
from tests.test_retry_prompt_preflight import client  # noqa: E402,F401


def _episode_plate(episode, shot="A01", **over):
    d = episode / "eps" / "ep77" / "plates" / shot
    d.mkdir(parents=True, exist_ok=True)
    spec = _spec(d, shot_id=shot, **over)
    return pc.compose(spec, d / "plate.png")


def test_route_passes_a_passed_plate_as_ninth_arg(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(dict(META))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    m = _episode_plate(episode)
    body = {"reason": "Studio r9", "prompt": lint_ok("One animal"), "ref_keys": ["rowan"],
            "plate_manifest": "plates/A01/plate.png.manifest.json", "plate_approved_by": "Studio r9"}
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 200, r.text
    name, args = h.updates[-1]
    assert name == "recompose_shot" and len(args) == 9 and args[6] is None and args[7] is None
    assert args[8]["sha256"] == m["sha256"] and args[8]["path"] == m["path"]
    assert args[8]["approved_by"] == "Studio r9" and args[8]["checks"]["passed"]


def test_route_refuses_bad_plates_before_anything(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(dict(META))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    base = {"reason": "S", "prompt": lint_ok("One animal"), "ref_keys": ["rowan"]}
    _episode_plate(episode, "A02")
    r = c.post("/api/episodes/ep77/shots/A01/recompose",
               json=dict(base, plate_manifest="plates/A02/plate.png.manifest.json"))
    assert r.status_code == 409 and "not A01" in r.json()["detail"]
    _episode_plate(episode, count=4)
    r = c.post("/api/episodes/ep77/shots/A01/recompose",
               json=dict(base, plate_manifest="plates/A01/plate.png.manifest.json"))
    assert r.status_code == 409 and "did not pass" in r.json()["detail"]
    outside = episode / "outside.json"
    outside.write_text("{}")
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=dict(base, plate_manifest=str(outside)))
    assert r.status_code == 422 and "inside the episode dir" in r.json()["detail"]
    assert h.updates == []


# ---------------------------------------------------------------- GAP-54 GAP-48 behind workflow.patched

@pytest.mark.asyncio
async def test_gap48_numbering_is_patched_for_replay(monkeypatch):
    """r9 restart: the unpatched GAP-48 fix renumbered ep05v2's already-recorded V04 recompose1 (v2 -> v5),
    so retry_still_ready(V04, 2) no longer matched and the replayed recompose_decision emitted no
    still_rejected signal -> TMPRL1100 nondeterminism at event 566.  Old histories (no marker) must keep
    the old numbering; new executions skip G2 still-child versions."""
    import hfvg.workflows.episode_v2 as mod
    from tests.test_r8_reuse_route_grant import _start_child, _wf
    for patched, want in ((False, 2), (True, 5)):
        wf, plans = _wf(), []
        _start_child(monkeypatch, plans)
        monkeypatch.setattr(mod.workflow, "patched", lambda _id, p=patched: p)
        wf.still_version_children = {"V03": [{"version": v, "workflow_id": f"ep-shot-V03-r{v}"} for v in (1, 2, 3, 4)]}
        await wf.recompose_shot("V03", "Studio", "new lobby still", ["/r/lobby.png"], ["lobby"])
        assert plans[-1]["version"] == want, (patched, plans[-1]["version"])
        for t in wf.background_tasks:
            t.cancel()

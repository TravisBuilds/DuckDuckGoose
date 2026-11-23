"""GAP-52 (r9): code-composited start plates for scale shots.

A *plate spec* (JSON) names a clean background plate, a measured scale reference in it (a rail or a
countertop: two points ``[x, top_y, base_y]`` interpolated in x, plus a horizon for depth), and the
cutout layers to paste back to front.  Every layer is sized from the rule, not by eye:

    ref_h(x, foot_y) = ref_h(x) * (foot_y - horizon) / (base_y(x) - horizon)   # reference at that depth
    scale_ref_h      = scale_ref_height_frac * ref_h(x, foot_y)                  # the scale character there
    layer_h          = height_frac_of_scale_ref * scale_ref_h

The scale character is the show's unit of height (show profile ``plate.scale_reference``). Each layer
has a role, and the spec's rules give each role its range of heights relative to the scale character
(``rules.role_to_scale_ref``: {role: [lo, hi]}) and, for the roles named in ``rules.back_max_frac``,
how far up the reference its top may reach at its depth (also kept below the reference's top edge in
the image); the headcount is exact.  Each layer gets a blurred-ellipse contact shadow under its feet.
Specs in the first series' key spelling are read as before (hfvg.compat.legacy_plate); a spec that
mixes the two spellings is refused. For one release the rows and the manifest carry each key in both
spellings, so readers of the old names keep working.

The result is a PNG at plate size plus a manifest (input/output sha256, per-layer numbers and
checks, ``passed``).  A failing spec still renders (for the sheet) but ``passed`` is False, and
the recompose route refuses a plate whose manifest did not pass (GAP-53).
PIL + numpy only, so it runs in the harness venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from hfvg.compat import legacy_plate

SCALE_FRAC_KEY = "scale_ref_height_frac"
LAYER_FRAC_KEY = "height_frac_of_scale_ref"
NEW_KEYS = {"top": (SCALE_FRAC_KEY,), "rules": ("role_to_scale_ref", "back_max_frac"), "layer": (LAYER_FRAC_KEY,)}
DEFAULT_SHADOW = {"opacity": 0.5, "width_frac": 0.9, "height_frac": 0.1, "blur": 6.0, "lift_frac": 0.02}
MAX_UPSCALE = 1.6  # a cutout may not be blown up more than this (it would go soft)


class PlateSpecError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PlateSpecError(f"{what} must be a number")
    return float(v)


def _file_ref(d: Any, what: str) -> dict[str, str]:
    if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not isinstance(d.get("sha256"), str):
        raise PlateSpecError(f"{what} needs path and sha256")
    if len(d["sha256"]) != 64:
        raise PlateSpecError(f"{what}.sha256 must be 64 hex chars")
    return d


def _new_keys_used(spec: dict[str, Any]) -> list[str]:
    rules = spec.get("rules") if isinstance(spec.get("rules"), dict) else {}
    layers = [L for L in spec.get("layers") or [] if isinstance(L, dict)] if isinstance(spec.get("layers"), list) else []
    used = [k for k in NEW_KEYS["top"] if k in spec] + [f"rules.{k}" for k in NEW_KEYS["rules"] if k in rules]
    return used + sorted({f"layers[].{k}" for L in layers for k in NEW_KEYS["layer"] if k in L})


def is_old_spelling(spec: dict[str, Any]) -> bool:
    """A spec that uses none of the scale-reference keys is read in the first series' spelling
    (hfvg.compat.legacy_plate); one that mixes the two is refused."""
    new, old = _new_keys_used(spec), legacy_plate.old_keys_used(spec)
    if new and old:
        raise PlateSpecError(f"the spec mixes {new} with their old spelling {old}; use one spelling")
    return not new


def _role_rules(rules: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validated (role -> [lo, hi], role -> back max fraction) of a spec in the current spelling."""
    ranges = rules.get("role_to_scale_ref")
    if not isinstance(ranges, dict) or not ranges:
        raise PlateSpecError("rules.role_to_scale_ref must map each layer role to [lo, hi]")
    for role, rng in ranges.items():
        if not isinstance(rng, list) or len(rng) != 2:
            raise PlateSpecError(f"rules.role_to_scale_ref.{role} must be [lo, hi]")
        lo, hi = (_num(v, f"rules.role_to_scale_ref.{role}") for v in rng)
        if not 0 < lo <= hi:
            raise PlateSpecError(f"rules.role_to_scale_ref.{role} must be [lo, hi]")
    back = rules.get("back_max_frac", {})
    if not isinstance(back, dict) or any(role not in ranges for role in back):
        raise PlateSpecError("rules.back_max_frac must map roles of rules.role_to_scale_ref to a fraction")
    for role, frac in back.items():
        _num(frac, f"rules.back_max_frac.{role}")
    return ranges, back


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Shape checks (no file access).  Returns a normalised copy."""
    if not isinstance(spec, dict):
        raise PlateSpecError("spec must be an object")
    old = is_old_spelling(spec)
    out = dict(spec)
    if not isinstance(spec.get("shot_id"), str) or not spec["shot_id"]:
        raise PlateSpecError("shot_id required")
    _file_ref(spec.get("plate"), "plate")
    ref = spec.get("reference")
    if not isinstance(ref, dict) or ref.get("kind") not in ("rail", "counter"):
        raise PlateSpecError("reference.kind must be rail or counter")
    pts = ref.get("points")
    if not isinstance(pts, list) or len(pts) != 2 or any(not isinstance(p, list) or len(p) != 3 for p in pts):
        raise PlateSpecError("reference.points must be two [x, top_y, base_y]")
    for p in pts:
        x, top, base = (_num(v, "reference point") for v in p)
        if base <= top:
            raise PlateSpecError("reference base_y must be below top_y")
    if pts[0][0] == pts[1][0]:
        raise PlateSpecError("reference points need different x")
    hz = _num(ref.get("horizon_y"), "reference.horizon_y")
    if any(hz >= p[2] for p in pts):
        raise PlateSpecError("horizon_y must be above the reference base")
    frac_key = legacy_plate.FRAC_KEY if old else SCALE_FRAC_KEY
    frac = _num(spec.get(frac_key), frac_key)
    if not 0.05 <= frac <= 1.5:
        raise PlateSpecError(f"{frac_key} out of range")
    if old:
        rules = dict(legacy_plate.DEFAULT_RULES)
        rules.update(spec.get("rules") or {})
        for k in legacy_plate.ROLE_RULE_KEYS.values():
            lo, hi = (_num(v, k) for v in rules[k])
            if not 0 < lo <= hi:
                raise PlateSpecError(f"rules.{k} must be [lo, hi]")
        _num(rules[legacy_plate.BACK_RULE_KEY], legacy_plate.BACK_RULE_KEY)
        roles = legacy_plate.ROLES
    else:
        if not isinstance(spec.get("rules"), dict):
            raise PlateSpecError("rules must be an object with role_to_scale_ref")
        rules = dict(spec["rules"])
        roles, back = _role_rules(rules)
        rules["back_max_frac"] = dict(back)
    out["rules"] = rules
    layer_frac = legacy_plate.LAYER_FRAC_KEY if old else LAYER_FRAC_KEY
    layers = spec.get("layers")
    if not isinstance(layers, list) or not layers:
        raise PlateSpecError("layers required")
    count = spec.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise PlateSpecError("count (exact headcount) required")
    names = set()
    for i, L in enumerate(layers):
        if not isinstance(L, dict) or not isinstance(L.get("name"), str):
            raise PlateSpecError(f"layer {i} needs a name")
        if L["name"] in names:
            raise PlateSpecError(f"duplicate layer {L['name']}")
        names.add(L["name"])
        if L.get("role") not in roles:
            raise PlateSpecError(f"layer {L['name']}: " + (legacy_plate.ROLE_MESSAGE if old else
                                 f"role must be one of {sorted(roles)} (rules.role_to_scale_ref)"))
        _file_ref(L.get("cutout"), f"layer {L['name']}.cutout")
        for k in ("x", "foot_y", layer_frac):
            _num(L.get(k), f"layer {L['name']}.{k}")
        if L[layer_frac] <= 0:
            raise PlateSpecError(f"layer {L['name']}: {layer_frac} must be > 0")
    sh = dict(DEFAULT_SHADOW)
    sh.update(spec.get("shadow") or {})
    if not 0 <= _num(sh["opacity"], "shadow.opacity") <= 1:
        raise PlateSpecError("shadow.opacity must be 0..1")
    out["shadow"] = sh
    return out


def ref_at(ref: dict[str, Any], x: float) -> tuple[float, float]:
    """(top_y, base_y) of the reference at image column x (linear in x, extrapolated)."""
    (x0, t0, b0), (x1, t1, b1) = ref["points"]
    u = (x - x0) / (x1 - x0)
    return t0 + u * (t1 - t0), b0 + u * (b1 - b0)


def ref_height_at_depth(ref: dict[str, Any], x: float, foot_y: float) -> float:
    top, base = ref_at(ref, x)
    hz = float(ref["horizon_y"])
    if foot_y <= hz:
        raise PlateSpecError(f"foot_y {foot_y} is at/above the horizon {hz}")
    return (base - top) * (foot_y - hz) / (base - hz)


def _alpha_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    a = np.asarray(im.split()[-1])
    ys, xs = np.nonzero(a >= 16)
    if not len(xs):
        raise PlateSpecError("cutout is empty")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def plan_layers(spec: dict[str, Any], cutout_sizes: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    """Pure geometry + rule checks for each layer (no pixels).  ``cutout_sizes[name]`` = trimmed (w, h)."""
    ref, rules = spec["reference"], spec["rules"]
    old = is_old_spelling(spec)
    ranges, back = legacy_plate.rules_view(rules) if old else (rules["role_to_scale_ref"], rules["back_max_frac"])
    frac = float(spec[legacy_plate.FRAC_KEY if old else SCALE_FRAC_KEY])
    layer_frac = legacy_plate.LAYER_FRAC_KEY if old else LAYER_FRAC_KEY
    rows = []
    for L in spec["layers"]:
        x, fy = float(L["x"]), float(L["foot_y"])
        rh = ref_height_at_depth(ref, x, fy)
        scale_ref_h = frac * rh
        h = float(L[layer_frac]) * scale_ref_h
        cw, ch = cutout_sizes[L["name"]]
        scale = h / ch
        w = cw * scale
        top_img = fy - h
        rail_top_img = ref_at(ref, x)[0]
        ratio = h / scale_ref_h
        lo, hi = ranges[L["role"]]
        checks = {"ratio_to_scale_ref": lo <= ratio + 1e-9 and ratio <= hi + 1e-9, "upscale": scale <= MAX_UPSCALE}
        if L["role"] in back:
            checks["back_below_ref_at_depth"] = h / rh <= float(back[L["role"]]) + 1e-9
            checks["back_below_ref_top_in_image"] = top_img > rail_top_img
        checks = legacy_plate.with_old_keys(checks, legacy_plate.CHECK_ALIASES)
        rows.append(legacy_plate.with_old_keys({
            "name": L["name"], "role": L["role"], "x": x, "foot_y": fy, "ref_h_at_depth": round(rh, 2),
            "scale_ref_h": round(scale_ref_h, 2), "height_px": round(h, 2), "width_px": round(w, 2),
            "scale": round(scale, 4), "ratio_to_scale_ref": round(ratio, 4),
            "frac_of_ref": round(h / rh, 4), "top_y": round(top_img, 2),
            "ref_top_y_in_image": round(rail_top_img, 2), "checks": checks,
            "passed": all(checks.values())}))
    return rows


def _shadow(size: tuple[int, int], cx: float, fy: float, w: float, h: float, sh: dict[str, Any]) -> np.ndarray:
    m = Image.new("L", size, 0)
    ew, eh = w * float(sh["width_frac"]) / 2, max(2.0, h * float(sh["height_frac"]) / 2)
    cy = fy - h * float(sh.get("lift_frac", 0.0))
    ImageDraw.Draw(m).ellipse([cx - ew, cy - eh, cx + ew, cy + eh], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(float(sh["blur"])))
    return np.asarray(m, dtype=np.float32) / 255.0 * float(sh["opacity"])


def compose(spec: dict[str, Any], out_png: str | Path, spec_path: str | None = None) -> dict[str, Any]:
    """Render the plate and write ``out_png`` + ``<out>.manifest.json``.  Returns the manifest."""
    spec = validate_spec(spec)
    plate_ref = spec["plate"]
    if sha256_file(plate_ref["path"]) != plate_ref["sha256"]:
        raise PlateSpecError("plate sha256 mismatch")
    plate = Image.open(plate_ref["path"]).convert("RGB")
    cutouts, sizes = {}, {}
    for L in spec["layers"]:
        c = L["cutout"]
        if sha256_file(c["path"]) != c["sha256"]:
            raise PlateSpecError(f"cutout {L['name']} sha256 mismatch")
        im = Image.open(c["path"]).convert("RGBA")
        im = im.crop(_alpha_bbox(im))
        if L.get("flip"):
            im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        cutouts[L["name"]], sizes[L["name"]] = im, im.size
    rows = plan_layers(spec, sizes)
    canvas = np.asarray(plate, dtype=np.float32).copy()
    for L, row in zip(spec["layers"], rows):
        im = cutouts[L["name"]]
        w, h = max(1, round(row["width_px"])), max(1, round(row["height_px"]))
        im = im.resize((w, h), Image.Resampling.LANCZOS)
        r, g, b, a = im.split()
        a = a.filter(ImageFilter.GaussianBlur(0.6))
        x0, y0 = round(row["x"] - w / 2), round(row["foot_y"] - h)
        row["box"] = [x0, y0, x0 + w, y0 + h]
        s = _shadow(plate.size, row["x"], row["foot_y"], w, h, spec["shadow"])
        canvas *= (1.0 - s)[..., None]
        layer = Image.new("RGBA", plate.size, (0, 0, 0, 0))
        layer.paste(Image.merge("RGBA", (r, g, b, a)), (x0, y0))
        la = np.asarray(layer, dtype=np.float32)
        al = la[..., 3:4] / 255.0
        canvas = canvas * (1 - al) + la[..., :3] * al
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(canvas + 0.5, 0, 255).astype(np.uint8)).save(out)
    count_ok = len(spec["layers"]) == spec["count"]
    frac = spec[legacy_plate.FRAC_KEY if is_old_spelling(spec) else SCALE_FRAC_KEY]
    manifest = {"kind": "plate_composite", "gap": "GAP-52", "shot_id": spec["shot_id"],
                "spec_path": spec_path, "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                "plate": plate_ref, "reference": spec["reference"],
                **legacy_plate.with_old_keys({SCALE_FRAC_KEY: frac}, {SCALE_FRAC_KEY: legacy_plate.FRAC_KEY}),
                "rules": spec["rules"], "count": spec["count"], "count_ok": count_ok,
                "layers": [dict(row, cutout=L["cutout"]) for L, row in zip(spec["layers"], rows)],
                "path": str(out.resolve()), "sha256": sha256_file(out), "size": list(plate.size),
                "notes": spec.get("notes", "")}
    manifest["passed"] = bool(count_ok and all(r["passed"] for r in rows))
    Path(str(out) + ".manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def load_passed_manifest(path: str | Path, shot_id: str) -> dict[str, Any]:
    """For the recompose route (GAP-53): a plate is usable only if its manifest passed, is for this
    shot, and the PNG on disk still matches the recorded sha256."""
    p = Path(path)
    if not p.is_file():
        raise PlateSpecError(f"no plate manifest at {p}")
    m = json.loads(p.read_text(encoding="utf-8"))
    if m.get("kind") != "plate_composite":
        raise PlateSpecError("not a plate_composite manifest")
    if m.get("shot_id") != shot_id:
        raise PlateSpecError(f"plate is for {m.get('shot_id')}, not {shot_id}")
    if not m.get("passed"):
        raise PlateSpecError("plate manifest did not pass its scale checks")
    if not Path(m["path"]).is_file() or sha256_file(m["path"]) != m.get("sha256"):
        raise PlateSpecError("plate PNG missing or changed since the manifest")
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hfvg.plate_composite")
    ap.add_argument("spec")
    ap.add_argument("out")
    ns = ap.parse_args(argv)
    m = compose(json.loads(Path(ns.spec).read_text(encoding="utf-8")), ns.out, spec_path=str(Path(ns.spec).resolve()))
    print(json.dumps({"passed": m["passed"], "sha256": m["sha256"],
                      "layers": [{k: r[k] for k in ("name", "height_px", "ratio_to_scale_ref", "frac_of_ref", "top_y",
                                                     "ref_top_y_in_image", "passed")} for r in m["layers"]]}, indent=1))
    return 0 if m["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

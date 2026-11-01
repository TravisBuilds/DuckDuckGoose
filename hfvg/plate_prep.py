"""GAP-52 prep (ep05v2 r9, Studio: code-composited start plates). Free, local plate preparation for
hfvg.plate_composite: cut an animal out of an APPROVED still (matte) and paint the animals out of a
still to get a clean plate (clean). Both are deterministic for the same inputs and write a JSON
sidecar with input/output sha256 + provenance so the composite can verify what it is built from.

Heavy optional dependencies (rembg for the matte, simple-lama-inpainting for the clean plate) are
imported lazily: run this module with a python that has them, e.g.
    PLATE_PREP_PYTHON -m hfvg.plate_prep matte  <still> <x0,y0,x1,y1> <out.png> --source "..."
    PLATE_PREP_PYTHON -m hfvg.plate_prep clean  <still> <mask.png> <out.png> --source "..."
The harness venv itself only needs PIL/numpy (the composite step)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter


class PlatePrepError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_box(text: str | list | tuple, size: tuple[int, int]) -> tuple[int, int, int, int]:
    vals = [int(v) for v in (text.split(",") if isinstance(text, str) else text)]
    if len(vals) != 4:
        raise PlatePrepError(f"box must be x0,y0,x1,y1: {text!r}")
    x0, y0, x1, y1 = vals
    w, h = size
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise PlatePrepError(f"box {vals} outside the {w}x{h} image")
    return x0, y0, x1, y1


def keep_largest_component(alpha: np.ndarray, thresh: int = 128) -> np.ndarray:
    """Zero every alpha blob except the largest (a matte of a box can pick up a neighbour's paw)."""
    from collections import deque
    m = alpha >= thresh
    h, w = m.shape
    labels = np.zeros((h, w), np.int32)
    best, best_n, cur = 0, 0, 0
    for y in range(h):
        for x in range(w):
            if m[y, x] and not labels[y, x]:
                cur += 1
                n, q = 0, deque([(y, x)])
                labels[y, x] = cur
                while q:
                    cy, cx = q.popleft()
                    n += 1
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not labels[ny, nx]:
                            labels[ny, nx] = cur
                            q.append((ny, nx))
                if n > best_n:
                    best, best_n = cur, n
    if not best:
        raise PlatePrepError("matte is empty")
    keep = labels == best
    # keep the soft edge around the kept blob (dilate 2 px), drop the others
    keep_img = Image.fromarray((keep * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    return np.where(np.asarray(keep_img) > 0, alpha, 0).astype(np.uint8)


def matte(still: str | Path, box, out_png: str | Path, source: str, remover=None,
          exclude: list[list[list[int]]] | None = None, exclude_cutouts: list[dict[str, Any]] | None = None
          ) -> dict[str, Any]:
    """RGBA cutout of the subject inside ``box`` (trimmed to its alpha bbox) + sidecar json.
    ``remover(PIL RGB) -> PIL L alpha`` defaults to rembg (lazy import). ``exclude`` = polygons
    ([[x, y], ...], still coords) and ``exclude_cutouts`` = other cutouts' sidecars whose alpha is
    removed first (an overlapping neighbour); both are recorded in the sidecar."""
    from PIL import ImageDraw
    img = Image.open(still).convert("RGB")
    x0, y0, x1, y1 = parse_box(box, img.size)
    crop = img.crop((x0, y0, x1, y1))
    if remover is None:
        from rembg import new_session, remove  # optional dependency (prep python only)
        session = new_session("isnet-general-use")

        def remover(im):
            return remove(im, session=session, only_mask=True)
    alpha = np.asarray(remover(crop).convert("L")).copy()
    if exclude or exclude_cutouts:
        ex = Image.new("L", img.size, 0)
        d = ImageDraw.Draw(ex)
        for poly in exclude or []:
            d.polygon([tuple(int(v) for v in pt) for pt in poly], fill=255)
        for m in exclude_cutouts or []:
            a = Image.open(m["path"]).split()[-1].point(lambda v: 255 if v >= 64 else 0)
            ex.paste(255, tuple(m["bbox_in_still"][:2]), a)
        exa = np.asarray(ex.crop((x0, y0, x1, y1)))
        alpha[exa > 0] = 0
    alpha = keep_largest_component(alpha)
    ys, xs = np.nonzero(alpha >= 16)
    if not len(xs):
        raise PlatePrepError("matte is empty")
    bx0, by0, bx1, by1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    rgba = crop.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha))
    rgba = rgba.crop((bx0, by0, bx1, by1))
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(out)
    meta = {"kind": "cutout", "still": str(Path(still).resolve()), "still_sha256": sha256_file(still),
            "box": [x0, y0, x1, y1], "bbox_in_still": [x0 + bx0, y0 + by0, x0 + bx1, y0 + by1],
            "size": list(rgba.size), "path": str(out.resolve()), "sha256": sha256_file(out), "source": source,
            "exclude": exclude or [], "exclude_cutouts": [m.get("sha256") for m in exclude_cutouts or []]}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return meta


def union_mask(size: tuple[int, int], cutout_metas: list[dict[str, Any]], grow_px: int = 12,
               extra: list[list[list[int]]] | None = None, still_sha256: str | None = None) -> Image.Image:
    """Mask (L, 255 = paint out) of the cutouts at their places in the still (+ ``extra`` polygons),
    dilated by grow_px. With ``still_sha256`` every cutout must have been cut from that still."""
    from PIL import ImageDraw
    mask = Image.new("L", size, 0)
    for poly in extra or []:
        ImageDraw.Draw(mask).polygon([tuple(int(v) for v in pt) for pt in poly], fill=255)
    for m in cutout_metas:
        if still_sha256 and m.get("still_sha256") != still_sha256:
            raise PlatePrepError(f"cutout {m.get('path')} was not cut from this still")
        a = Image.open(m["path"]).split()[-1].point(lambda v: 255 if v >= 8 else 0)
        mask.paste(255, tuple(m["bbox_in_still"][:2]), a)
    if grow_px:
        mask = mask.filter(ImageFilter.MaxFilter(2 * (grow_px // 2) + 1))
    return mask


def clean(still: str | Path, mask_png: str | Path, out_png: str | Path, source: str, painter=None) -> dict[str, Any]:
    """Paint the masked region out of the still (LaMa by default, lazy import) -> clean plate + sidecar."""
    img = Image.open(still).convert("RGB")
    mask = Image.open(mask_png).convert("L")
    if mask.size != img.size:
        raise PlatePrepError(f"mask {mask.size} != still {img.size}")
    if painter is None:
        from simple_lama_inpainting import SimpleLama  # optional dependency (prep python only)
        lama = SimpleLama()

        def painter(im, m):
            return lama(im, m)
    out_img = painter(img, mask).convert("RGB").resize(img.size, Image.LANCZOS)
    # keep every unmasked pixel bit-exact from the approved still
    keep = Image.composite(out_img, img, mask)
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    keep.save(out)
    meta = {"kind": "clean_plate", "still": str(Path(still).resolve()), "still_sha256": sha256_file(still),
            "mask": str(Path(mask_png).resolve()), "mask_sha256": sha256_file(mask_png),
            "masked_frac": round(float((np.asarray(mask) > 127).mean()), 4),
            "size": list(keep.size), "path": str(out.resolve()), "sha256": sha256_file(out), "source": source}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hfvg.plate_prep")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("matte")
    a.add_argument("still"); a.add_argument("box"); a.add_argument("out"); a.add_argument("--source", required=True)
    a.add_argument("--exclude", default="[]", help="JSON list of polygons [[x,y],...] in still coords")
    a.add_argument("--exclude-cutout", action="append", default=[], help="sidecar json of a neighbour cutout")
    b = sub.add_parser("mask")
    b.add_argument("still"); b.add_argument("out"); b.add_argument("cutout_json", nargs="+")
    b.add_argument("--grow", type=int, default=12)
    b.add_argument("--extra", default="[]", help="JSON list of extra polygons to paint out")
    c = sub.add_parser("clean")
    c.add_argument("still"); c.add_argument("mask"); c.add_argument("out"); c.add_argument("--source", required=True)
    ns = ap.parse_args(argv)
    if ns.cmd == "matte":
        print(json.dumps(matte(ns.still, ns.box, ns.out, ns.source, exclude=json.loads(ns.exclude),
                               exclude_cutouts=[json.loads(Path(p).read_text()) for p in ns.exclude_cutout])))
    elif ns.cmd == "mask":
        size = Image.open(ns.still).size
        metas = [json.loads(Path(p).read_text()) for p in ns.cutout_json]
        union_mask(size, metas, ns.grow, extra=json.loads(ns.extra), still_sha256=sha256_file(ns.still)).save(ns.out)
        print(json.dumps({"mask": ns.out, "sha256": sha256_file(ns.out)}))
    else:
        print(json.dumps(clean(ns.still, ns.mask, ns.out, ns.source)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

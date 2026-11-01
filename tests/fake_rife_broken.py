#!/usr/bin/env python3
"""Stand-in for a BROKEN rife-ncnn-vulkan (tests only, GAP-81): what the real binary did on 2026-10-31
with no Vulkan ICD -- prints "vkCreateInstance failed -9", exits 0, writes the right number of frames;
output 1 is a copy of input 0 (t=0) and every other output is a horizontally smeared frame."""
import sys
from pathlib import Path

from PIL import Image

a = sys.argv[1:]
opt = {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}
ind, outd, n = Path(opt["-i"]), Path(opt["-o"]), int(opt["-n"])
ins = sorted(ind.glob("*.png"))
outd.mkdir(parents=True, exist_ok=True)
print("vkCreateInstance failed -9", file=sys.stderr)
for i in range(n):
    with Image.open(ins[0]) as im:
        im = im.convert("RGB")
        if i:
            w, h = im.size
            im = im.rotate(90, expand=True).resize((h, 1)).resize((h, w)).rotate(-90, expand=True)
            im = Image.eval(im, lambda v: 255 - v)
        im.save(outd / (opt.get("-f", "%08d.png") % (i + 1)))

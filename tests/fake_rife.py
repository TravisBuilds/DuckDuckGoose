#!/usr/bin/env python3
"""Stand-in for rife-ncnn-vulkan (tests only): same CLI and the same -n time mapping
(output i shows input time i*K/n; frames past the last input hold it), output = nearest input frame
(copied) so tests can check which source frame each output came from. Logs its argv."""
import os
import shutil
import sys
from pathlib import Path

a = sys.argv[1:]
opt = {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}
ind, outd, n = Path(opt["-i"]), Path(opt["-o"]), int(opt["-n"])
assert Path(opt["-m"]).is_dir()
ins = sorted(ind.glob("*.png"))
k = len(ins)
log = os.environ.get("FAKE_RIFE_LOG")
if log:
    with open(log, "a") as f:
        f.write(f"{k} {n}\n")
outd.mkdir(parents=True, exist_ok=True)
for i in range(n):
    t = min(k - 1, round(i * k / n))
    shutil.copy(ins[t], outd / (opt.get("-f", "%08d.png") % (i + 1)))

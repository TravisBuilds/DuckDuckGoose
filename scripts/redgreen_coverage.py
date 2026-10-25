#!/usr/bin/env python3
"""Check that sharded redgreen runs covered every declared probe/mutation exactly once.

Usage: python scripts/redgreen_coverage.py [--script scripts/redgreen.sh]
                                          [--skip-conf scripts/redgreen_skip.conf]
                                          [--markdown OUT.md] RESULTS.tsv [RESULTS.tsv ...]

Each results file is written by scripts/redgreen.sh (one per shard): a header
"# shard<TAB>N<TAB>M<TAB>commit<TAB>declared" and one "<id><TAB><status><TAB><reason>" line per probe/mutation.

Fails (exit 1) when: a declared id is missing from the union, an id was run twice, an id that is
not declared shows up, a status is not PASS (SKIP is allowed only for ids in the skip conf),
shards disagree on the shard count or commit, or a shard 1..M is missing.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

DECL_RE = re.compile(r'^test_(?:mutation|probe)\s+"?([^"\s]+)"?\s', re.M)
SKIP_RE = re.compile(r"^M-(\d+)\s+\S", re.M)


def declared_ids(script_text: str) -> list[str]:
    return DECL_RE.findall(script_text)


def skip_ids(conf_text: str) -> set[str]:
    return set(SKIP_RE.findall(conf_text))


def parse_results(text: str, name: str):
    header = None
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("# shard\t"):
            fields = line.split("\t")
            header = (int(fields[1]), int(fields[2]), fields[3], int(fields[4]) if len(fields) > 4 else -1)
            continue
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise ValueError(f"{name}: malformed line {line!r}")
        rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    if header is None:
        raise ValueError(f"{name}: missing '# shard' header")
    return header, rows


def check(declared: list[str], skips: set[str], results: list[tuple[str, tuple, list]]) -> list[str]:
    problems = []
    dup_decl = [i for i, c in Counter(declared).items() if c > 1]
    if dup_decl:
        problems.append(f"ids declared more than once in redgreen.sh: {sorted(dup_decl)}")
    if not results:
        return problems + ["no results files"]
    totals = {h[1] for _, h, _ in results}
    commits = {h[2] for _, h, _ in results}
    if len(totals) != 1:
        problems.append(f"shards disagree on the shard count: {sorted(totals)}")
    if len(commits) != 1:
        problems.append(f"shards ran different commits: {sorted(commits)}")
    for name, h, _ in results:
        if h[3] != len(declared):
            problems.append(f"{name}: the script declared {h[3]} probes/mutations at run time, "
                            f"redgreen.sh declares {len(declared)} (out of date or conditional declarations)")
    shard_nums = Counter(h[0] for _, h, _ in results)
    total = max(totals)
    missing_shards = sorted(set(range(1, total + 1)) - set(shard_nums))
    if missing_shards:
        problems.append(f"missing shard results: {missing_shards} of {total}")
    dup_shards = sorted(n for n, c in shard_nums.items() if c > 1)
    if dup_shards:
        problems.append(f"shard results given twice: {dup_shards}")

    seen = Counter()
    status = {}
    for name, _, rows in results:
        for mid, st, reason in rows:
            seen[mid] += 1
            status[mid] = (st, reason, name)
    declared_set = set(declared)
    missing = [i for i in declared if seen[i] == 0]
    if missing:
        problems.append(f"{len(missing)} declared id(s) never ran: {missing}")
    twice = sorted(i for i, c in seen.items() if c > 1)
    if twice:
        problems.append(f"id(s) ran more than once: {twice}")
    unknown = sorted(set(seen) - declared_set)
    if unknown:
        problems.append(f"result id(s) not declared in redgreen.sh: {unknown}")
    for mid in declared:
        if mid not in status:
            continue
        st, reason, name = status[mid]
        if st == "PASS":
            continue
        if st == "SKIP" and mid in skips:
            continue
        problems.append(f"{mid}: {st} ({reason}) [{name}]")
    return problems


def markdown(declared, results, problems) -> str:
    counts = Counter(st for _, _, rows in results for _, st, _ in rows)
    commit = next(iter({h[2] for _, h, _ in results}), "?")
    out = ["# Red-green mutation status", "", f"Commit `{commit}`, {len(results)} shard(s), "
           f"{len(declared)} declared probes/mutations.", ""]
    out += [f"- {k}: {v}" for k, v in sorted(counts.items())]
    out += ["", "## Coverage check: " + ("PASS" if not problems else "FAIL"), ""]
    out += [f"- {p}" for p in problems]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(root / "scripts" / "redgreen.sh"))
    ap.add_argument("--skip-conf", default=str(root / "scripts" / "redgreen_skip.conf"))
    ap.add_argument("--markdown")
    ap.add_argument("--expect-commit", help="fail unless every shard ran this commit (CI: github.sha)")
    ap.add_argument("--expect-shards", type=int, help="fail unless the shard count is exactly this")
    ap.add_argument("results", nargs="+")
    args = ap.parse_args(argv)

    declared = declared_ids(Path(args.script).read_text())
    conf = Path(args.skip_conf)
    skips = skip_ids(conf.read_text()) if conf.exists() else set()
    results = []
    problems = []
    for f in args.results:
        try:
            header, rows = parse_results(Path(f).read_text(), f)
        except (OSError, ValueError) as e:
            problems.append(str(e))
            continue
        results.append((f, header, rows))
    problems += check(declared, skips, results)
    if args.expect_commit:
        wrong = sorted({h[2] for _, h, _ in results} - {args.expect_commit})
        if wrong:
            problems.append(f"shards ran commit(s) {wrong}, expected {args.expect_commit}")
    if args.expect_shards is not None:
        counts = sorted({h[1] for _, h, _ in results})
        if counts != [args.expect_shards]:
            problems.append(f"shard count {counts}, expected {args.expect_shards}")
    if args.markdown:
        Path(args.markdown).write_text(markdown(declared, results, problems))
    ran = sum(len(r) for _, _, r in results)
    if problems:
        print(f"redgreen coverage FAILED ({ran} results, {len(declared)} declared):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"redgreen coverage OK: {len(declared)} declared ids, each ran exactly once, all PASS/allowed SKIP")
    return 0


if __name__ == "__main__":
    sys.exit(main())

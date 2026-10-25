"""G-CI-1: the redgreen summary job fails unless the shard union covers every declared id once."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("redgreen_coverage", ROOT / "scripts" / "redgreen_coverage.py")
cov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cov)

SCRIPT = '''
test_probe "PC" "positive control" \\
    "FAIL_ASSERT" x y sed -i s/a/b/
test_mutation 1 "one" \\
    f t sed -i s/a/b/
test_mutation 2 "two" \\
    f t sed -i s/a/b/
test_mutation 3 "three" \\
    f t sed -i s/a/b/
'''


def _shard(tmp_path, n, m, rows, commit="abc", declared=4):
    p = tmp_path / f"s{n}.tsv"
    p.write_text(f"# shard\t{n}\t{m}\t{commit}\t{declared}\n" + "".join(f"{i}\t{s}\tr\n" for i, s in rows))
    return str(p)


def _run(tmp_path, files, skip=""):
    script = tmp_path / "redgreen.sh"
    script.write_text(SCRIPT)
    conf = tmp_path / "skip.conf"
    conf.write_text(skip)
    return cov.main(["--script", str(script), "--skip-conf", str(conf),
                     "--markdown", str(tmp_path / "out.md"), *files])


def test_real_script_declares_unique_ids():
    ids = cov.declared_ids((ROOT / "scripts" / "redgreen.sh").read_text())
    assert len(ids) == len(set(ids)) and len(ids) > 300, len(ids)
    assert ids[:4] == ["PC", "P1", "P2", "P3"]


def test_full_union_passes(tmp_path):
    files = [_shard(tmp_path, 1, 2, [("PC", "PASS"), ("2", "PASS")]),
             _shard(tmp_path, 2, 2, [("1", "PASS"), ("3", "SKIP")])]
    assert _run(tmp_path, files, skip="M-3 obsolete\n") == 0
    assert "Coverage check: PASS" in (tmp_path / "out.md").read_text()


def test_dropped_mutation_fails(tmp_path):
    files = [_shard(tmp_path, 1, 2, [("PC", "PASS"), ("2", "PASS")]),
             _shard(tmp_path, 2, 2, [("1", "PASS")])]
    assert _run(tmp_path, files, skip="M-3 obsolete\n") == 1
    assert "never ran: ['3']" in (tmp_path / "out.md").read_text()


def test_missing_shard_duplicate_unknown_and_bad_status_fail(tmp_path):
    one = _shard(tmp_path, 1, 2, [("PC", "PASS"), ("1", "PASS"), ("2", "PASS"), ("3", "PASS")])
    assert _run(tmp_path, [one]) == 1  # shard 2 of 2 missing
    two = _shard(tmp_path, 2, 2, [("1", "PASS"), ("9", "PASS")])
    assert _run(tmp_path, [one, two]) == 1  # 1 twice + 9 unknown
    for status in ("FAIL", "INVALID", "SKIP"):  # SKIP without a skip-conf entry is a failure
        a = _shard(tmp_path, 1, 2, [("PC", "PASS"), ("1", status)])
        b = _shard(tmp_path, 2, 2, [("2", "PASS"), ("3", "PASS")])
        assert _run(tmp_path, [a, b]) == 1, status


def test_commit_and_declared_count_mismatch_fail(tmp_path):
    a = _shard(tmp_path, 1, 2, [("PC", "PASS"), ("1", "PASS")], commit="abc")
    b = _shard(tmp_path, 2, 2, [("2", "PASS"), ("3", "PASS")], commit="def")
    assert _run(tmp_path, [a, b]) == 1
    a = _shard(tmp_path, 1, 2, [("PC", "PASS"), ("1", "PASS")], declared=5)
    b = _shard(tmp_path, 2, 2, [("2", "PASS"), ("3", "PASS")], declared=5)
    assert _run(tmp_path, [a, b]) == 1


def test_expected_commit_and_shard_count_enforced(tmp_path):
    a = _shard(tmp_path, 1, 2, [("PC", "PASS"), ("1", "PASS")])
    b = _shard(tmp_path, 2, 2, [("2", "PASS"), ("3", "PASS")])
    assert _run(tmp_path, [a, b]) == 0
    base = ["--script", str(tmp_path / "redgreen.sh"), "--skip-conf", str(tmp_path / "skip.conf")]
    assert cov.main(base + ["--expect-commit", "abc", "--expect-shards", "2", a, b]) == 0
    assert cov.main(base + ["--expect-commit", "zzz", a, b]) == 1
    assert cov.main(base + ["--expect-shards", "4", a, b]) == 1


def _summary_exit(invalid, failed):
    """Run redgreen.sh's own print_summary() (extracted verbatim) with the given counters."""
    import re
    import subprocess
    src = (ROOT / "scripts" / "redgreen.sh").read_text()
    fn = re.search(r"^print_summary\(\) \{\n.*?^\}\n", src, re.S | re.M).group(0)
    prelude = ("log_header() { :; }\nGREEN= RED= YELLOW= BLUE= NC=\n"
               "MUTATION_NAMES=(a) MUTATION_RESULTS=(INVALID) MUTATION_REASONS=(r)\n"
               f"PASS_COUNT=0 SKIP_COUNT=0 INVALID_COUNT={invalid} FAIL_COUNT={failed}\n")
    return subprocess.run(["bash", "-c", prelude + fn + "print_summary"], capture_output=True, text=True).returncode


def test_shard_summary_fails_on_invalid_ids():
    """A shard whose ids were all INVALID (e.g. deps missing) proved nothing: it must not exit 0."""
    assert _summary_exit(invalid=0, failed=0) == 0
    assert _summary_exit(invalid=1, failed=0) == 1
    assert _summary_exit(invalid=0, failed=1) == 1

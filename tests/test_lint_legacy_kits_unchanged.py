"""Kits WITH a "lint" object behave exactly as before the built-in default lint profile existed.

A grid of synthetic legacy kits x prompts x shot_meta goes through the same calls the paid-submit
gates make (lint_violations / clip_lint_violations, the file loaders behind apply_continuity_locks
and enforce_prompt_lint, the kit validation errors, the offline lint_report); every output must
equal the one recorded on main before that change (fixtures/lint_legacy_golden.json, problem texts
stored once in its "messages" table). Synthetic text only.
"""

import hashlib
import json
from pathlib import Path

import pytest

from hfvg.prompt_lint import (
    LintKit,
    PromptLintError,
    apply_continuity_locks,
    clip_lint_violations,
    enforce_prompt_lint,
    lint_report,
    lint_violations,
)
from hfvg.shot_data import parse_shots
from tests.lint_helpers import SYNTHETIC_LINT_KIT

GOLDEN = Path(__file__).parent / "fixtures" / "lint_legacy_golden.json"

DUCK_LOCK = "SYNTH DUCK LOCK: plush toy duck, tiny tail nub at most; vest and beanie never change."
DUCK_CLIP = "Duck tail stays a tiny plush nub while it moves."
SCALE_LOCK = "SYNTH SCALE: real size; exactly the stated count."
LOCKS = [
    {"id": "duck_identity", "when": "duck", "kinds": ["still", "clip"], "text": DUCK_LOCK},
    {"id": "duck_clip", "when": "duck", "kinds": ["clip"], "text": DUCK_CLIP},
    {"id": "animal_scale", "when": "animals", "kinds": ["still", "clip"], "text": SCALE_LOCK},
    {"id": "grade", "when": "always", "kinds": ["still"], "text": "SYNTH GRADE: warm dusk palette."},
]

KITS = {
    "synthetic": dict(SYNTHETIC_LINT_KIT),
    "locks": dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS, animal_characters=["Otter1"]),
    "custom": {
        "count_template": "exactly {n} creatures",
        "realtime_pattern": r"in real time",
        "locked_camera_clause": "static tripod shot",
        "antler_clause": "antlers: two",
        "antler_characters": ["E1"],
        "duck_clause": "plush duckling",
        "duck_characters": ["D1"],
        "banned_decor": ["pergola"],
        "forbidden_phrases": ["slow pan"],
        "moose_names": ["elk"],
        "carry_verbs": ["lifts"],
        "bath_clause": "waterline at the shoulders",
        "bath_forbidden": ["on top of the water"],
        "duck_names": ["duckling"],
        "duck_realism_words": ["realistic"],
    },
    "minimal": {
        "count_template": "Count: {n}",
        "realtime_pattern": "real[- ]time",
        "locked_camera_clause": "locked camera",
    },
}

SYN = "Exactly 1 animals in frame. Real-time speed, no slow motion."
PROMPTS = {
    "syn_clean": f"A heron wades by the dock. {SYN}",
    "syn_ext": "Two otters on the pier. Exactly 2 animals in frame. Real-time speed, no slow motion. "
               "locked-off tripod, no camera movement.",
    "custom_clean": "A fox trots across the meadow, exactly 1 creatures, filmed in real time. static tripod shot.",
    "minimal_clean": "Count: 1. A fox in real-time. locked camera.",
    "no_clauses": "A fox trots across a sunlit meadow while leaves drift down.",
    "count_mismatch": "Exactly 3 animals in frame. Exactly 1 animals in frame. Real-time speed, no slow motion.",
    "forbidden": f"{SYN} The pond is calm, nothing else moves. Realistic wild otters. A slow pan.",
    "waves": f"{SYN} Big waves roll in; ONE small wave laps the dock.",
    "carry": f"{SYN} The moose is carrying a crate. The elk lifts a log. The moose holds still.",
    "decor": f"{SYN} A gazebo and lanterns by the lake; a lantern glows under the pergola.",
    "hard_stop": f"{SYN} A nude statue, an explicit poster, nsfw gore.",
    "clauses": f"{SYN} synthetic antler clause: two full antlers visible. synthetic duck clause: one duck on "
               "the water. antlers: two. plush duckling. waterline at the shoulders.",
    "bath_bad": f"{SYN} The otter floats on top of the water.",
    "duck_real": "Exactly 0 animals in frame. Real-time speed, no slow motion. The duck, photoreal feathers, "
                 "waits. A realistic duckling naps.",
    "clip_static": f"{SYN} Freeze-frame: everything else is still, like a still image.",
    "locks": f"{SYN} {DUCK_LOCK} {DUCK_CLIP} {SCALE_LOCK} SYNTH GRADE: warm dusk palette.",
}


def _meta(count=1, **kw):
    meta = {"count": count, "exterior": False, "water": False, "characters": []}
    meta.update(kw)
    return meta


METAS = {
    "int1": _meta(1),
    "ext2": _meta(2, exterior=True),
    "bath": _meta(1, water=True, wet=True),
    "stag": _meta(1, characters=["Stag03", "E1"]),
    "duck": _meta(0, characters=["Duck", "D1", "Otter1"]),
    "flags": _meta(1, characters=["X"], antler=True, duck=True),
    "badcount": _meta("3"),
}

# Invalid legacy lint sections: still refused, with the same message.
BAD_SECTIONS = {
    "no_count": {k: v for k, v in SYNTHETIC_LINT_KIT.items() if k != "count_template"},
    "blank_realtime": dict(SYNTHETIC_LINT_KIT, realtime_pattern="  "),
    "no_camera": {k: v for k, v in SYNTHETIC_LINT_KIT.items() if k != "locked_camera_clause"},
    "count_without_n": dict(SYNTHETIC_LINT_KIT, count_template="Exactly some animals"),
    "bad_regex": dict(SYNTHETIC_LINT_KIT, realtime_pattern="real("),
    "decor_string": dict(SYNTHETIC_LINT_KIT, banned_decor="gazebo"),
    "moose_int": dict(SYNTHETIC_LINT_KIT, moose_names=3),
    "locks_dict": dict(SYNTHETIC_LINT_KIT, continuity_locks={"id": "a"}),
    "lock_no_text": dict(SYNTHETIC_LINT_KIT, continuity_locks=[{"id": "a", "when": "duck", "text": ""}]),
    "lock_dup": dict(SYNTHETIC_LINT_KIT, continuity_locks=[{"id": "a", "when": "duck", "text": "t1"},
                                                           {"id": "a", "when": "duck", "text": "t2"}]),
    "lock_kind": dict(SYNTHETIC_LINT_KIT, continuity_locks=[{"id": "a", "when": "duck", "kinds": ["audio"],
                                                             "text": "t"}]),
}

# How a legacy kit reaches the gate: the per-episode prompt_kit.json "lint" object (top-level
# continuity_locks merged in), or PROMPT_LINT_KIT_PATH naming a bare lint file or a full kit.
LAYOUTS = ("episode_full", "override_bare", "override_full")
GATE_PROMPTS = ("syn_clean", "hard_stop", "clip_static", "locks", "duck_real")
GATE_METAS = ("int1", "duck", "flags")

SHOTS = {"shots": [
    {"id": "A01", "prompt": PROMPTS["syn_clean"], "count": 1, "characters": [], "wet": False,
     "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
    {"id": "A02", "prompt": PROMPTS["syn_clean"], "clip_prompt": PROMPTS["clip_static"], "count": 1,
     "characters": ["Duck"], "wet": False, "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
    {"id": "A03", "prompt": PROMPTS["no_clauses"], "clip_prompt": PROMPTS["syn_ext"], "count": 2,
     "characters": [], "wet": False, "exterior": True, "model": "kling", "duration": 5, "end_frame": False,
     "ref_keys": [], "pre_approved_still": {"path": "refs/a03.png"}},
    {"id": "A04", "prompt": PROMPTS["hard_stop"], "count": 1, "characters": ["Stag03"], "wet": True,
     "water": True, "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
]}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _grid() -> dict:
    out = {}
    for kname, section in KITS.items():
        kit = LintKit.from_dict(section)
        for pname, prompt in PROMPTS.items():
            for mname, meta in METAS.items():
                out[f"{kname}/{pname}/{mname}"] = [lint_violations(prompt, meta, kit),
                                                    clip_lint_violations(prompt, meta, kit)]
    return out


def _bad_sections() -> dict:
    out = {}
    for name, section in BAD_SECTIONS.items():
        try:
            LintKit.from_dict(section)
            out[name] = "loaded"
        except PromptLintError as e:
            out[name] = str(e)
    return out


def _use_layout(root: Path, layout: str, monkeypatch) -> None:
    ep = root / "eps" / "ep90"
    ep.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EPISODES_DIR", str(root / "eps"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    full = {"style": "synthetic", "characters": {}, "lint": dict(SYNTHETIC_LINT_KIT), "continuity_locks": LOCKS}
    if layout == "episode_full":
        (ep / "prompt_kit.json").write_text(json.dumps(full))
        return
    path = root / f"{layout}.json"
    path.write_text(json.dumps(dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS) if layout == "override_bare"
                               else full))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(path))


def _gates(root: Path, monkeypatch) -> dict:
    out = {}
    for layout in LAYOUTS:
        _use_layout(root / layout, layout, monkeypatch)
        for pname in GATE_PROMPTS:
            for mname in GATE_METAS:
                for kind in ("still", "clip"):
                    meta = METAS[mname]
                    prompt, added = apply_continuity_locks("ep90", PROMPTS[pname], meta, True, kind)
                    dry = enforce_prompt_lint("ep90", prompt, meta, False, kind)
                    try:
                        enforce_prompt_lint("ep90", prompt, meta, True, kind)
                        live = "ok"
                    except PromptLintError as e:
                        live = str(e)
                    out[f"{layout}/{pname}/{mname}/{kind}"] = {
                        "prompt_sha256": _sha(prompt), "added": added, "dry": dry, "live": live}
    return out


def _reports(root: Path, monkeypatch) -> dict:
    plans = parse_shots(SHOTS)
    out = {}
    for kname in ("synthetic", "custom", "minimal"):
        ep = root / "reports" / kname / "ep91"
        ep.mkdir(parents=True)
        (ep / "prompt_kit.json").write_text(json.dumps({"style": "synthetic", "lint": KITS[kname]}))
        monkeypatch.setenv("EPISODES_DIR", str(ep.parent))
        monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
        out[kname] = lint_report("ep91", plans)
    return out


def compute(root: Path, monkeypatch) -> dict:
    return {"grid": _grid(), "bad_sections": _bad_sections(), "gates": _gates(root, monkeypatch),
            "reports": _reports(root, monkeypatch)}


def pack(data: dict) -> str:
    """The golden file: problem texts once in "messages", referenced by index; one case per line."""
    messages = sorted({p for v in data["grid"].values() for side in v for p in side}
                      | {p for v in data["gates"].values() for p in v["dry"]}
                      | {p for rep in data["reports"].values() for v in rep.values() for p in v})
    index = {m: i for i, m in enumerate(messages)}
    refused = "Prompt lint failed: {}"
    sections = {
        "grid": {k: [[index[p] for p in side] for side in v] for k, v in data["grid"].items()},
        "bad_sections": data["bad_sections"],
        "gates": {k: {**v, "dry": [index[p] for p in v["dry"]],
                      "live": "refused" if v["live"] == refused.format("; ".join(v["dry"])) else v["live"]}
                  for k, v in data["gates"].items()},
        "reports": {k: {sid: [index[p] for p in v] for sid, v in rep.items()} for k, rep in data["reports"].items()},
    }
    lines = ["{", ' "messages": [']
    lines += [f"  {json.dumps(m, ensure_ascii=False)}," for m in messages]
    lines[-1] = lines[-1].rstrip(",")
    lines.append(" ],")
    for n, (name, cases) in enumerate(sections.items()):
        lines.append(f" {json.dumps(name)}: {{")
        body = [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(', ', ': '))},"
                for k, v in sorted(cases.items())]
        body[-1] = body[-1].rstrip(",")
        lines += body
        lines.append(" }" + ("," if n < len(sections) - 1 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


def unpack(text: str) -> dict:
    raw = json.loads(text)
    msg = raw["messages"]

    def gate(v):
        dry = [msg[i] for i in v["dry"]]
        live = "Prompt lint failed: " + "; ".join(dry) if v["live"] == "refused" else v["live"]
        return {**v, "dry": dry, "live": live}

    return {
        "grid": {k: [[msg[i] for i in side] for side in v] for k, v in raw["grid"].items()},
        "bad_sections": raw["bad_sections"],
        "gates": {k: gate(v) for k, v in raw["gates"].items()},
        "reports": {k: {sid: [msg[i] for i in v] for sid, v in rep.items()} for k, rep in raw["reports"].items()},
    }


@pytest.fixture(scope="module")
def golden() -> dict:
    return unpack(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture
def actual(tmp_path, monkeypatch) -> dict:
    return compute(tmp_path, monkeypatch)


@pytest.mark.parametrize("section", ["grid", "bad_sections", "gates", "reports"])
def test_legacy_kits_reproduce_the_recorded_outputs(golden, actual, section):
    expected, got = golden[section], actual[section]
    assert sorted(got) == sorted(expected), "the case list changed: re-record the golden on main only"
    diffs = [f"{case}:\n  recorded {expected[case]!r}\n  now      {got[case]!r}"
             for case in sorted(expected) if got[case] != expected[case]]
    assert not diffs, f"{len(diffs)} legacy-kit result(s) changed:\n" + "\n".join(diffs[:20])


def test_golden_round_trips_and_covers_clean_and_refused_prompts(golden, actual):
    assert unpack(pack(actual)) == actual
    still = [v[0] for v in golden["grid"].values()]
    assert any(not p for p in still) and any(p for p in still)
    assert any("forbidden phrase: 'nothing else moves'" in p for v in still for p in v)
    assert any(v["live"] == "ok" for v in golden["gates"].values())
    assert any(v["live"] != "ok" for v in golden["gates"].values())
    # legacy kits never had content-policy term checks: the hard-stop words pass them as before
    assert golden["grid"]["synthetic/hard_stop/int1"][0] == []

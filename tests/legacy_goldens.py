"""Goldens of what the first series' episodes get today, recorded before the show profile existed.

Every function below feeds synthetic first-series inputs (prompt kits, shots.json, beatmaps, cast
library characters, reviewer headcount rows) through the public entry points the pipeline uses and
returns JSON-safe outputs: lint and playbook messages, ``shot_meta`` keys in order, headcount
rosters and the answers built from legacy reviewer rows, character sheet prompts, judge prompts and
cache keys. ``tests/fixtures/legacy_golden/<surface>.json`` holds the values recorded on the code
before the show profile (main 76d22d1 + PR 56): an episode without ``show.json`` goes through the
legacy adapter and must get exactly these outputs (tests/test_show_profile_legacy_adapter.py).

Record (only on that code, never to make a failing comparison pass):
``python -m tests.legacy_goldens``. Synthetic text only, no network, no spend.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "legacy_golden"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dedupe(cases: dict[str, Any]) -> dict[str, Any]:
    """{"values": [distinct value, ...], "cases": {case: index}}: one copy of each repeated output."""
    values: list[Any] = []
    index: dict[str, int] = {}
    cases_out: dict[str, int] = {}
    for name in sorted(cases):
        blob = json.dumps(cases[name], sort_keys=True)
        if blob not in index:
            index[blob] = len(values)
            values.append(cases[name])
        cases_out[name] = index[blob]
    return {"values": values, "cases": cases_out}


def _undedupe(packed: dict[str, Any]) -> dict[str, Any]:
    return {name: packed["values"][i] for name, i in packed["cases"].items()}


# ------------------------------------------------------------------------------------------ prompt lint

LOCK_TEXT = {
    "identity": "SYNTH HOST LOCK: plush toy duck, tiny tail nub at most; vest and beanie never change.",
    "clip": "Duck tail stays a tiny plush nub while it moves.",
    "scale": "SYNTH SCALE: real size; exactly the stated count.",
    "grade": "SYNTH GRADE: warm dusk palette.",
}
LOCKS = [
    {"id": "duck_identity", "when": "duck", "kinds": ["still", "clip"], "text": LOCK_TEXT["identity"]},
    {"id": "duck_clip", "when": "duck", "kinds": ["clip"], "text": LOCK_TEXT["clip"]},
    {"id": "animal_scale", "when": "animals", "kinds": ["still", "clip"], "text": LOCK_TEXT["scale"]},
    {"id": "grade", "when": "always", "kinds": ["still"], "text": LOCK_TEXT["grade"]},
]


def _series_kit() -> dict:
    from tests.lint_helpers import SYNTHETIC_LINT_KIT
    return dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS, animal_characters=["Otter1"],
                bath_clause="waterline at the shoulders", bath_forbidden=["On Top Of The Water", "  "],
                carry_verbs=["carry", "carries", "", "lifts"], duck_characters=["Duck", "D1"])


LINT_PROMPTS = {
    "clean": "Exactly 1 animals in frame. Real-time speed, no slow motion. A heron by the dock.",
    "semicolon_carry": "Exactly 1 animals in frame. Real-time speed, no slow motion. The moose waits; it "
                       "carries a log! The MOOSE lifts it? moose2 carries rope.",
    "realism_edges": "Exactly 0 animals in frame. Real-time speed, no slow motion. The duck2 is photoreal. A "
                     "duckling, life-like; the Duck is hyper-real? Duck_bill photo-realistic. ducks realistic.",
    "realism_long": "Exactly 1 animals in frame. Real-time speed, no slow motion. The duck stands by a very long "
                    "row of cedar planks under a pale grey sky with drifting snow and a faint mist, lifelike.",
    "bath": "Exactly 2 animals in frame. Real-time speed, no slow motion. Two otters float ON TOP OF THE "
            "WATER, waterline at the shoulders.",
    "waves": "Waves crash. Exactly 1 animals in frame. Real-time speed, no slow motion. WAVES. Ripples.",
    "locks_partial": f"Exactly 1 animals in frame. Real-time speed, no slow motion. {LOCK_TEXT['identity']}",
    "hard_stop": "Exactly 1 animals in frame. Real-time speed, no slow motion. A nude statue, gore.",
    "plain": "A heron wades by the dock while reeds sway.",
}


def _lmeta(count: Any = 1, **kw) -> dict:
    meta = {"count": count, "exterior": False, "water": False, "characters": []}
    meta.update(kw)
    return meta


LINT_METAS = {
    "plain": _lmeta(1),
    "host_flag": _lmeta(1, duck=True),
    "host_chars": _lmeta(0, characters=["Duck"]),
    "bath_true": _lmeta(0, bath=True),
    "bath_false": _lmeta(2, wet=True, water=True, bath=False),
    "wet_water": _lmeta(2, wet=True, water=True),
    "antler_flag": _lmeta(1, antler=True, characters=["Stag03"]),
    "antler_only_flag": _lmeta(1, antler=True),
    "count_none": _lmeta(None),
    "count_bool": _lmeta(True),
    "otter_char": _lmeta(0, characters=["Otter1"]),
}


def _lint_kits() -> dict:
    from hfvg.prompt_lint import LintKit, default_lint_kit
    from tests.lint_helpers import SYNTHETIC_LINT_KIT
    return {
        "default": default_lint_kit(LOCKS),
        "default_nolocks": default_lint_kit(),
        "synthetic": LintKit.from_dict(dict(SYNTHETIC_LINT_KIT)),
        "series": LintKit.from_dict(_series_kit()),
    }


def lint_extra(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg.prompt_lint import (
        PromptLintError,
        apply_ref_edit_locks,
        clip_lint_violations,
        inject_continuity_locks,
        lint_report,
        lint_violations,
    )
    from hfvg.shot_data import parse_shots

    grid = {}
    for kname, kit in _lint_kits().items():
        for pname, prompt in LINT_PROMPTS.items():
            for mname, meta in LINT_METAS.items():
                still, added_s = inject_continuity_locks(prompt, meta, kit, "still")
                clip, added_c = inject_continuity_locks(prompt, meta, kit, "clip")
                grid[f"{kname}/{pname}/{mname}"] = {
                    "still": lint_violations(prompt, meta, kit), "clip": clip_lint_violations(prompt, meta, kit),
                    "inject_still": [_sha(still), added_s], "inject_clip": [_sha(clip), added_c]}

    ep = root / "lint_eps" / "ep92"
    ep.mkdir(parents=True)
    mp.setenv("EPISODES_DIR", str(ep.parent))
    mp.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    (ep / "prompt_kit.json").write_text(json.dumps({"style": "synthetic", "lint": _series_kit()}))
    ref_edits = {}
    lock_metas = {"none": None, "host": {"duck": True}, "not_host": {"duck": False}, "count": {"count": 2},
                  "chars": {"characters": ["Duck"]}, "host_count0": {"duck": True, "count": 0}}
    edit_prompts = {"plush": "A plush duck sits on the step.", "real": "A realistic duck sits. A realistic pond.",
                    "split": "The duck is photoreal; the pond is realistic."}
    for mname, lock_meta in lock_metas.items():
        for pname, prompt in edit_prompts.items():
            out, added = apply_ref_edit_locks("ep92", prompt, lock_meta, False)
            try:
                apply_ref_edit_locks("ep92", prompt, lock_meta, True)
                live = "ok"
            except PromptLintError as e:
                live = str(e)
            ref_edits[f"{mname}/{pname}"] = {"prompt": out, "added": added, "live": live}

    shots = {"shots": [
        {"id": "A01", "prompt": LINT_PROMPTS["plain"], "count": 1, "characters": ["Duck"], "wet": False,
         "duck_present": True, "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
        {"id": "A02", "prompt": LINT_PROMPTS["realism_edges"], "count": 0, "characters": [], "wet": True,
         "water": True, "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
        {"id": "A03", "prompt": LINT_PROMPTS["hard_stop"], "count": 2, "characters": [], "wet": False,
         "antler_character": True, "model": "kling", "duration": 5, "end_frame": False, "ref_keys": []},
    ]}
    reports = {}
    for name, kit in (("no_lint_key", {"style": "s", "continuity_locks": LOCKS}),
                      ("lint_default", {"style": "s", "lint": "default", "continuity_locks": LOCKS}),
                      ("series", {"style": "s", "lint": _series_kit()})):
        d = root / "reports" / name / "ep93"
        d.mkdir(parents=True)
        (d / "prompt_kit.json").write_text(json.dumps(kit))
        mp.setenv("EPISODES_DIR", str(d.parent))
        reports[name] = lint_report("ep93", parse_shots(shots))
    return {"grid": _dedupe(grid), "ref_edits": ref_edits, "reports": reports}


# ------------------------------------------------------------------------------------------ playbook

PB_PROMPTS = {
    "plain": "Three guests by the pond",
    "negative": "Three guests. No duck in frame.",
    "upright": "A guest standing upright carrying a tray",
    "fours": "A guest on all fours, quadrupedal gait",
    "hold": "Guest monkey holding a bag, hand over to a friend, handing it on",
    "bipedal": "A bipedal robot walking upright",
}


def _pb_metas() -> dict:
    from tests.lint_helpers import lint_meta
    return {
        "guest_only": lint_meta(characters=["G"], duck=False, duck_declared=True),
        "undeclared": lint_meta(characters=["G"], duck=False),
        "host": lint_meta(characters=["G"], duck=True, duck_declared=True),
        "nochars": lint_meta(duck=False, duck_declared=True),
        "zero_flag": lint_meta(characters=["G"], duck=0, duck_declared=True),
        "wet": lint_meta(characters=["G"], wet=True),
        "none": None,
    }


PB_TAGS = {"none": None, "guest_only": ["guest-only"], "pack": ["duck-in-pack"], "guest": ["guest"],
           "animal": ["animal"], "env": ["env"], "env_pre": ["env", "pre-resort"], "mud": ["mud"], "wet": ["wet"]}
PB_REFS = {"none": None, "world": ["/refs/world_a/lobby.png"], "three": ["a.png", "b.png", "c.png"]}
PB_KINDS = {"still": ("still", None), "clip_kling": ("clip", "kling_3.0"), "clip_seedance": ("clip", "seedance_2.5"),
            "clip_none": ("clip", None)}
PB_ENVS = {"none": {}, "hard_host": {"HFVG_PLAYBOOK_HARD_RULES": "duck_role"},
           "world": {"HFVG_PLAYBOOK_WORLD_REFS": "world_a"},
           "hard_all": {"HFVG_PLAYBOOK_HARD_RULES": "duck_role, animal_poses,same_resort,moderation_routing",
                        "HFVG_PLAYBOOK_WORLD_REFS": "world_a,world_b"}}

PLAYBOOK_VIEWS = {
    "room_no_ref": {"shot_id": "R01", "type": "room_plate", "refs": ["img_random"], "tags": []},
    "room_ref": {"shot_id": "R02", "type": "room_plate", "refs": ["img_world_lobby", "img_props"], "tags": []},
    "room_exempt": {"shot_id": "O01", "type": "room_plate", "refs": ["img_wild_spring"], "tags": ["pre-resort"]},
    "env_tag": {"shot_id": "E01", "refs": [], "tags": ["env"]},
    "absent_role": {"shot_id": "A01", "duck_role": "absent", "prompt": "Four penguins standing together",
                    "tags": ["guest-only"]},
    "absent_ok": {"shot_id": "A02", "duck_role": "absent", "prompt": "Four penguins, no duck in frame", "tags": []},
    "pack": {"shot_id": "A03", "prompt": "Group selfie", "tags": ["duck-in-pack"]},
    "upright": {"shot_id": "A04", "prompt": "Guest monkey standing upright holding a bag", "tags": ["guest"]},
    "fours": {"shot_id": "A05", "prompt": "Guest monkey on all fours, bundle tied on back", "tags": ["guest"]},
    "animal_tag": {"shot_id": "A06", "prompt": "A fox trots", "tags": ["animal"]},
    "all": {"shot_id": "A07", "tier": "final", "draft_approved": False, "type": "room_plate",
            "refs": ["img_random"], "tags": ["guest-only"], "duck_role": "absent",
            "prompt": "Guest monkey standing upright"},
    "draft": {"shot_id": "A08", "tier": "draft", "qc_passed": False},
    "wet": {"shot_id": "C01", "tags": ["wet", "pool"]},
    "multi_ref": {"shot_id": "F02", "tags": [], "refs": ["r1", "r2", "r3"]},
}


def _legacy_validator():
    from hfvg.playbook import PlaybookValidator
    return PlaybookValidator()


def moderation_lint(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg import moderation_lint as ml

    def run(env: dict, **kw) -> Any:
        for var in ("HFVG_PLAYBOOK_HARD_RULES", "HFVG_PLAYBOOK_WORLD_REFS"):
            mp.delenv(var, raising=False)
        for var, value in env.items():
            mp.setenv(var, value)
        try:
            return ml.evaluate(**kw)
        except ml.ModerationLintError as e:
            return {"error": str(e), "record": getattr(e, "record", None)}

    metas = _pb_metas()
    records = {}
    for pname, prompt in PB_PROMPTS.items():
        for mname, meta in metas.items():
            for tname, tags in PB_TAGS.items():
                records[f"grid/{pname}/{mname}/{tname}"] = run(
                    {}, shot_id="A01", prompt=prompt, shot_meta=meta, kind="still", tags=tags, live=True)
    for tname, tags in PB_TAGS.items():
        for rname, refs in PB_REFS.items():
            for ename, env in PB_ENVS.items():
                for live in (False, True):
                    records[f"env/{tname}/{rname}/{ename}/{'live' if live else 'dry'}"] = run(
                        env, shot_id="B02", prompt=PB_PROMPTS["plain"], shot_meta=metas["guest_only"], kind="still",
                        tags=tags, refs=refs, live=live)
    for kname, (kind, model) in PB_KINDS.items():
        for tname, tags in PB_TAGS.items():
            for rname, refs in PB_REFS.items():
                records[f"route/{kname}/{tname}/{rname}"] = run(
                    {}, shot_id="C03", prompt=PB_PROMPTS["fours"], shot_meta=metas["undeclared"], kind=kind,
                    model=model, tags=tags, refs=refs, live=True)
    mp.setenv("HFVG_PLAYBOOK_HARD_RULES", "duck_role, not_a_rule")
    try:
        ml.hard_rules()
        unknown = "accepted"
    except ml.ModerationLintError as e:
        unknown = str(e)
    mp.delenv("HFVG_PLAYBOOK_HARD_RULES", raising=False)

    v = _legacy_validator()
    world = ["img_world"]
    validators = {}
    for name, view in PLAYBOOK_VIEWS.items():
        validators[name] = {
            "duck_role": v.validate_duck_role(dict(view)),
            "animal_poses": v.validate_animal_poses(dict(view)),
            "same_resort": v.validate_same_resort(dict(view), world),
            "draft_first": v.validate_draft_first(dict(view)),
            "shot": v.validate_shot(dict(view), world),
            "shot_no_world": v.validate_shot(dict(view)),
            "routing": v.validate_moderation_routing(dict(view)),
        }
    return {"records": _dedupe(records), "unknown_hard_rule": unknown, "validators": validators}


# ------------------------------------------------------------------------------------------ shot plans

SHOTS_SPEC = {"episode_id": "ep94", "shots": [
    {"id": "A01", "prompt": "A heron by the dock.", "count": 1, "characters": [], "wet": False, "model": "kling",
     "duration": 5, "end_frame": False, "ref_keys": []},
    {"id": "A02", "prompt": "Two otters and the host.", "count": 2, "characters": ["Duck", "Otter1"], "wet": True,
     "exterior": True, "water": True, "duck_present": True, "antler_character": True, "model": "kling",
     "duration": 6, "end_frame": True, "end_prompt": "The otters dive.", "ref_keys": ["r_host", "r_otter"]},
    {"id": "A03", "prompt": "Guests only.", "count": 3, "characters": ["G1", "G2", "G3"], "wet": False,
     "duck_present": False, "antler_character": False, "model": "seedance", "seedance_resolution": "720p",
     "duration": 5, "end_frame": False, "clip_prompt": "  The guests wave.  ", "trim_in": 0.5, "trim_out": 4.5,
     "qc_fail_if": ["a guest vanishes"], "ref_keys": []},
    {"id": "A04", "prompt": "Truthy flags.", "count": 0, "characters": [], "wet": False, "duck_present": 1,
     "antler_character": "no", "model": "kling", "duration": 4, "end_frame": False, "ref_keys": [],
     "trim_head_s": 0.25, "trim_tail_s": 0.5, "allow_text": True},
    {"id": "A05", "prompt": "Cast declared.", "count": 2, "characters": [], "wet": False, "duck_present": True,
     "model": "kling", "duration": 5, "end_frame": False, "ref_keys": [], "cast": ["pip", "ana"],
     "pre_approved_still": {"path": "refs/a05.png"}},
]}

EDIT_SPEC = {"episode_id": "ep94", "shots": [
    {"id": "A01", "count": 2, "characters": ["Duck"], "duck_present": True, "duration": 5,
     "reuse": {"clip": "clips/a01.mp4", "clip_sha256": "a" * 64, "source": "ep93"}},
    {"id": "A02", "count": 1, "characters": [], "screen_s": 3.5, "trim_head_s": 0.5, "qc_fail_if": ["x"],
     "reuse": {"clip": "clips/a02.mp4", "sha256": "b" * 64, "status": "locked"}},
    {"id": "A03", "count": 1, "duck_present": False, "duration": 4},
], "dissolves": {"A01->A02": 0.5}}


def shot_meta(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg.edit_plan import edit_plan_from_spec
    from hfvg.shot_data import parse_shots

    plans = parse_shots(json.loads(json.dumps(SHOTS_SPEC)))
    out = {sid: {"plan_keys": list(plan), "shot_meta": [[k, v] for k, v in plan["shot_meta"].items()],
                 "tags": plan["tags"], "params": [[k, v] for k, v in plan["params"].items()]}
           for sid, plan in plans.items()}
    edit = edit_plan_from_spec(json.loads(json.dumps(EDIT_SPEC)))
    reuse = {sid: [[k, v] for k, v in r["shot_meta"].items()] for sid, r in edit["reuse"].items()}
    return {"plans": out, "edit_reuse_meta": reuse, "edit_plan": edit}


BEATMAP = """# Synthetic beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 6 (6) | Lobby | Dusk | D | The host waves at the door | Welcome | Cut | Cut | K |
| 2 | A02 | 5 (5) ⇥ | Pool | Night | D(T) A B | Guests soak in the steaming pool | Rest | Cut | Cut | S |
| 3 | A03 | 4.5 (5) | Spring | Dawn | K(tuque) D(B) | Splash at the surface, wet fur | Play | Cut | Cut | K |
| 4 | A04 | 6 | Deck | Noon | – | Empty deck, mud by the steps | Pause | Cut | Cut | kling |
| 5 | A05 | 3 (3) | Hall | Dusk | AG M1 M2 | Hand-off ★ underwater lamp | Turn | Cut | Cut | seedance |
| 6 | F02 | 7 (7) | Roof | Night | W | A slow look at the stars | End | Cut | Cut | S |
"""


def episode_parser(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg.episode_parser import build_prompt_from_shot, get_model_routing, parse_beatmap_text

    repo = Path(__file__).resolve().parent.parent
    out = {}
    for name, text in (("synthetic", BEATMAP), ("demo", (repo / "fixtures" / "demo" / "BEATMAP.md").read_text(
            encoding="utf-8"))):
        shots = parse_beatmap_text(text)
        out[name] = [{"shot": [[k, v] for k, v in s.items()], "prompt": build_prompt_from_shot(s),
                      "routing": get_model_routing(s)} for s in shots]
    out["prompt_only"] = [build_prompt_from_shot(s) for s in (
        {}, {"shot_id": "Z9"}, {"duck_role": "absent", "characters": ["A"]}, {"duck_role": "", "characters": ["A", "B"]},
        {"characters": ["A"], "room": "Lobby"}, {"duck_role": "host", "room": "Deck", "tod": "Dusk", "action": "Waves"})]
    return out


# ------------------------------------------------------------------------------------------ headcount

def _cast_entry(slug: str, name: str, locked: bool = True, clause: str = "") -> dict:
    return {"character": slug, "name": name, "version": 1, "locked": locked,
            "clause": clause or f"{name}: a synthetic figure called {name}"}


HC_METAS = {
    "count2": {"count": 2, "characters": ["A", "B"]},
    "count2_host": {"count": 2, "characters": [], "duck": True},
    "cast_over_count_host": {"count": 1, "duck": True,
                             "cast": [_cast_entry("host", "Host"), _cast_entry("ana", "Ana")]},
    "cast_under_count_host": {"count": 3, "duck": True, "cast": [_cast_entry("ana", "Ana")]},
    "cast_no_count": {"cast": [_cast_entry("ana", "Ana"), _cast_entry("ben", "Ben", locked=False)]},
    "zero_host": {"count": 0, "duck": True},
    "zero": {"count": 0},
    "bad_count_host": {"count": "2", "duck": True},
    "cast_equal_count": {"count": 2, "cast": [_cast_entry("ana", "Ana"), _cast_entry("ana", "Ana again"),
                                              _cast_entry("ben", "Ben")]},
    "cast_over_count": {"count": 1, "cast": [_cast_entry("ana", "Ana"), _cast_entry("ben", "Ben")]},
}
LEGACY_ROWS = {
    "c2_d1": {"cast": 2, "duck": 1, "other": 0},
    "c1_d0": {"cast": 1, "duck": 0},
    "c3_d2_o1": {"cast": 3, "duck": 2, "other": 1},
    "c0_d1_o2": {"cast": 0, "duck": 1, "other": 2, "by": "rev"},
}
COUNTER_CASES = {
    "as_declared": {},
    "one_more_cast": {"_cast": 3},
    "host_missing": {"_duck_host": 0},
    "unexpected": {"unexpected": 1},
    "background": {"background": 2},
}
LEGACY_COUNTS = {
    "match": {"cast": 2, "duck": 1, "animal": 3},
    "extra": {"cast": 2, "duck": 1, "animal": 4},
    "no_host": {"cast": 2, "duck": 0, "animal": 2},
    "short": {"cast": 2, "duck": 1, "animal": 2},
}


def _png(path: Path, color: tuple[int, int, int] = (90, 140, 200), size: tuple[int, int] = (64, 96)) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def headcount(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg.qc.animal_count import (
        FakeAnimalCounter,
        check_shot_animals,
        expected_cast,
        legacy_row_answer,
    )
    from hfvg.qc.g2_headcount import _row, reviewer_answer
    from hfvg.qc.headcount import FakeCastCounter, check_headcount, shot_roster

    def figures(roster) -> Any:
        if roster is None:
            return None
        return {"as_dict": roster.as_dict(),
                "figures": [[f.id, f.name, f.expected, f.description, f.group, f.locked] for f in roster.figures]}

    still = _png(root / "hc" / "A01" / "still_v1.png")
    clip = root / "hc" / "A01" / "clip.mp4"
    clip.write_bytes(b"not a real video; the fake counters never read it")
    rosters, legacy, reviewer, counted, animals = {}, {}, {}, {}, {}
    for mname, meta in HC_METAS.items():
        roster = shot_roster(meta)
        rosters[mname] = {"roster": figures(roster), "expected_cast": expected_cast(meta)}
        if roster is None:
            continue
        for rname, row in LEGACY_ROWS.items():
            legacy[f"{mname}/{rname}"] = legacy_row_answer(row, roster, meta)
            reviewer[f"{mname}/{rname}"] = reviewer_answer(row, roster, meta)
        for cname, counts in COUNTER_CASES.items():
            counted[f"{mname}/{cname}"] = asyncio.run(check_headcount(FakeCastCounter(counts), still, meta))
        for lname, counts in LEGACY_COUNTS.items():
            for media in (still, clip):
                counter = FakeAnimalCounter(counts, confidence=0.9)
                animals[f"{mname}/{lname}/{media.suffix}/headcount"] = asyncio.run(
                    check_headcount(counter, media, meta))
                animals[f"{mname}/{lname}/{media.suffix}/calls"] = counter.calls
                counter = FakeAnimalCounter(counts, confidence=0.9)
                animals[f"{mname}/{lname}/{media.suffix}/shot_animals"] = asyncio.run(
                    check_shot_animals(counter, media, meta))
    rows = {}
    for name, counts in {"legacy": {"cast": 2, "duck": 1}, "legacy_other": {"cast": 1, "duck": 0, "other": 3},
                         "figures": {"figures": {"b": 1, "a": 2}, "unexpected": 1},
                         "bad_duck": {"cast": 2, "duck": -1}, "bad_figure": {"figures": {"../x": 1}},
                         "missing_cast": {"duck": 1}}.items():
        try:
            rows[name] = _row(counts)
        except ValueError as e:
            rows[name] = f"ValueError: {e}"
    return {"rosters": rosters, "legacy_row_answer": _dedupe(legacy), "reviewer_answer": _dedupe(reviewer),
            "check_headcount": _dedupe(counted), "legacy_counter": _dedupe(animals), "g2_rows": rows}


# ------------------------------------------------------------------------------------------ sheet prompts

SHEET_CHARACTERS = {
    "humanoid": {"name": "Mara", "description": "a tall woman with short grey hair and a green coat"},
    "four_legged": {"name": "Wool", "description": "a round fluffy white sheep with a black face",
                    "body_type": "four_legged"},
    "other": {"name": "Glim", "description": "a floating paper lantern with two round eyes", "body_type": "other"},
    "humanoid_pose": {"name": "Sita", "description": "a girl in a red raincoat",
                      "sheet_pose": "Sitting cross-legged on the floor, hands on knees."},
    "other_pose": {"name": "Hoot", "description": "a small brown owl", "body_type": "other",
                   "sheet_pose": "perched upright, wings folded"},
}


def sheet_prompts(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from PIL import Image

    from hfvg import cast_store
    from hfvg.cast_sheets import placeholder_image, plan_jobs
    from tests.cast_helpers import image_bytes, new_character, upload

    mp.setenv("CAST_STORAGE_DIR", str(root / "private-cast"))
    mp.setenv("DATA_DIR", str(root))
    mp.setenv("DATABASE_PATH", str(root / "sheets.db"))
    mp.setenv("DRY_RUN", "true")
    mp.delenv("MODEL_PATH_STILL", raising=False)
    eps = root / "sheet_eps"
    (eps / "ep95style").mkdir(parents=True)
    (eps / "ep95style" / "prompt_kit.json").write_text(json.dumps({"style": "flat pastel illustration, soft light."}))
    mp.setenv("EPISODES_DIR", str(eps))

    def jobs(character: dict, stage: str, episode: str, **kw) -> list:
        planned = plan_jobs(cast_store.get_character(character["character_id"]), 1, stage, episode_id=episode,
                            live=False, **kw)
        return [[planned["stage"], planned["mode"], j["view"], j["prompt"], j["aspect_ratio"], j["body_type"]]
                for j in planned["jobs"]]

    out = {}
    for episode in ("ep95plain", "ep95style"):
        lib = ("episode", episode)
        for key, fields in SHEET_CHARACTERS.items():
            ch = new_character(lib, fields["name"], **{k: v for k, v in fields.items() if k != "name"})
            out[f"{episode}/{key}/options_text"] = jobs(ch, "options", episode, source="text")
            upload(ch, seed=3)
            out[f"{episode}/{key}/options_upload"] = jobs(ch, "options", episode, source="upload", count=1)
            cast_store.add_generated_image(ch["character_id"], 1, image_bytes(seed=7), role=cast_store.MASTER_ROLE,
                                           job_id=f"m-{ch['character_id']}", placeholder=True, actor="t")
            out[f"{episode}/{key}/turnaround"] = jobs(ch, "turnaround", episode)
    sizes = {}
    for body in ("humanoid", "four_legged", "other", "unknown", None):
        for view in ("master_option", "turnaround_side", "head_sheet"):
            job = {"job_id": "castsheet-ch_0123456789ab-v1-x-a1", "view": view}
            if body is not None:
                job["body_type"] = body
            sizes[f"{body}/{view}"] = list(Image.open(io.BytesIO(placeholder_image(job))).size)
    return {"jobs": out, "placeholders": sizes}


# ------------------------------------------------------------------------------------------ judge prompts

class RecordingService:
    """Stands in for JudgeService: records the request, answers what the validator accepts."""

    model = "gpt-4.1-mini"

    def __init__(self, answer: Callable[[Any], dict]):
        self.answer = answer
        self.calls: list[Any] = []

    async def ask(self, call) -> dict:
        self.calls.append(call)
        return call.validate(self.answer(call))


def _request(call, files: dict[str, Path], model: str = "gpt-4.1-mini") -> dict:
    """The request with each test image's content hash replaced by its name (image bytes depend on
    the Pillow build); the cache key is the one of those parts, so it still pins every other part."""
    from hfvg.qc.judges import cache_key

    blob = json.dumps(call.key_parts, sort_keys=True)
    for name, path in files.items():
        blob = blob.replace(hashlib.sha256(path.read_bytes()).hexdigest(), f"<{name}>")
    parts = json.loads(blob)
    return {"kind": call.kind, "episode_id": call.episode_id, "subject": call.subject, "system_sha": _sha(call.system),
            "text": call.text, "images": len(call.images), "key_parts": parts,
            "cache_key": cache_key(call.kind, model, parts)}


def judge_prompts(root: Path, mp: pytest.MonkeyPatch) -> dict:
    from hfvg.qc import identity, still_review, vision_constraints, vision_counter
    from hfvg.qc.duck_identity import DUCK_HOST_PROFILE, DuckIdentityGate
    from hfvg.qc.headcount import shot_roster
    from hfvg.qc.identity import (
        GENERIC_PROFILE,
        IdentityCriteria,
        VisionIdentityJudge,
        judge_system,
    )
    from hfvg.qc.judges import JUDGE_VERSION

    systems = {"headcount": vision_counter.SYSTEM, "detect": identity.DETECT_SYSTEM, "still": still_review.SYSTEM,
               "fail_if": vision_constraints.SYSTEM}
    labels = {"sheet": [label for label, _ in identity.SHEET_PANEL_ROLES], "refs": ["ref", "ref"], "none": []}
    identity_systems = {}
    for lname, lab in labels.items():
        identity_systems[f"generic/{lname}"] = judge_system(GENERIC_PROFILE, lab)
        for state in (None, "B", "T"):
            identity_systems[f"host/{lname}/{state}"] = judge_system(DUCK_HOST_PROFILE, lab, state)

    still = _png(root / "judge" / "A01" / "still_v1.png", color=(120, 80, 40))

    def count_answer(call) -> dict:
        ids = [f[0] for f in call.key_parts["figures"]]
        return {"figures": {i: {"count": 1, "matches": True} for i in ids}, "unexpected": 0, "background": 0,
                "confidence": 0.9, "notes": "ok"}

    counter_requests = {}
    for mname in ("count2", "count2_host", "cast_over_count_host", "cast_under_count_host", "cast_no_count", "zero"):
        meta = dict(HC_METAS[mname])
        if mname == "count2_host":
            meta["props"] = [{"item": "lamp", "name": "Lamp", "clause": "Lamp: a brass lamp on the desk"}]
        service = RecordingService(count_answer)
        roster = shot_roster(meta)
        asyncio.run(vision_counter.VisionCastCounter(service).count_cast(
            still, {"shot_meta": meta, "roster": roster, "shot_id": "A01", "episode_id": None}))
        counter_requests[mname] = _request(service.calls[0], {"still": still})

    composite = _png(root / "judge" / "composite.png", color=(30, 60, 90), size=(96, 48))
    sheet = (("master", str(_png(root / "judge" / "sheet_master.png", color=(10, 20, 30)))),
             ("head_sheet", str(_png(root / "judge" / "sheet_head.png", color=(40, 50, 60)))))

    def identity_answer(call) -> dict:
        return {"signs": {}, "apparel": {}, "features": {}, "verdict": "PASS", "reason": "ok", "confidence": 0.9}

    generic = IdentityCriteria(slug="pip", name="Pip", version=2, description="a small round robot",
                               lock_hash="h" * 16, sheet=sheet, episode_id="ep96")
    host = DuckIdentityGate(episode_id="ep96").criteria
    identity_requests = {}
    for name, criteria, ctx in (("generic", generic, {"shot_id": "A01"}),
                                ("host_b", host, {"shot_id": "A02", "duck_state": "B", "duck_role": "host"}),
                                ("host_none", host, {"shot_id": "A03"})):
        service = RecordingService(lambda call, c=criteria: {
            **identity_answer(call), "signs": {s: {"score": 0, "confidence": 0.9} for s in c.profile.signs},
            "apparel": {a: {"present": True, "correct": True} for a in c.profile.apparel},
            "features": {f: {"present": True, "correct": True} for f, _ in c.profile.state_features.values()}})
        result = asyncio.run(VisionIdentityJudge(service).judge_identity(composite, ctx, criteria))
        identity_requests[name] = {**_request(service.calls[0], {"composite": composite}),
                                   "result_keys": sorted(result)}
    return {"judge_version": JUDGE_VERSION, "systems": systems, "identity_systems": identity_systems,
            "counter_requests": counter_requests, "identity_requests": identity_requests,
            "host_profile": {"name": DUCK_HOST_PROFILE.name, "label": DUCK_HOST_PROFILE.label,
                             "signs": DUCK_HOST_PROFILE.signs, "apparel": DUCK_HOST_PROFILE.apparel,
                             "hint_signs": list(DUCK_HOST_PROFILE.hint_signs),
                             "state_key": DUCK_HOST_PROFILE.state_key,
                             "state_features": {k: list(v) for k, v in DUCK_HOST_PROFILE.state_features.items()},
                             "min_height_fraction": DUCK_HOST_PROFILE.min_height_fraction,
                             "presence": list(DUCK_HOST_PROFILE.presence or ()),
                             "presence_note": DUCK_HOST_PROFILE.presence_note}}


SURFACES: dict[str, Callable[[Path, pytest.MonkeyPatch], dict]] = {
    "lint_extra": lint_extra,
    "moderation_lint": moderation_lint,
    "shot_meta": shot_meta,
    "episode_parser": episode_parser,
    "headcount": headcount,
    "sheet_prompts": sheet_prompts,
    "judge_prompts": judge_prompts,
}


def golden_path(surface: str) -> Path:
    return GOLDEN_DIR / f"{surface}.json"


def dump(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def load(surface: str) -> dict:
    return json.loads(golden_path(surface).read_text(encoding="utf-8"))


CLEARED_ENV = ("PROMPT_LINT_KIT_PATH", "SHOTS_JSON_PATH", "REFS_JSON_PATH", "HFVG_SHOW_PROFILE_PATH",
               "HFVG_PLAYBOOK_HARD_RULES", "HFVG_PLAYBOOK_WORLD_REFS", "MODEL_PATH_STILL", "HFVG_PROJECT")


def compute(surface: str, root: Path, mp: pytest.MonkeyPatch) -> dict:
    """The surface's outputs, normalised the way the golden file stores them (JSON round trip, the
    temporary folder written as <root>)."""
    for var in CLEARED_ENV:
        mp.delenv(var, raising=False)
    return json.loads(dump(SURFACES[surface](root, mp)).replace(str(root), "<root>"))


def record() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for surface in SURFACES:
        with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
            golden_path(surface).write_text(dump(compute(surface, Path(tmp), mp)), encoding="utf-8")
        print(f"recorded {golden_path(surface)}")


if __name__ == "__main__":
    record()

"""web/lib/presets.ts must mirror hfvg/data/genre_presets.yaml card for card, field for field, and the
create wizard's Skip window and card groups (web/lib/wizard.mjs) must match the API's freeform profile
and the registry's templates."""

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "hfvg" / "data" / "genre_presets.yaml"
TS_PATH = REPO_ROOT / "web" / "lib" / "presets.ts"
WIZARD_PATH = REPO_ROOT / "web" / "lib" / "wizard.mjs"

# presets.ts key -> genre_presets.yaml key
FIELD_MAP = {
    "id": "id",
    "name": "name",
    "description": "description",
    "template": "template",
    "aspect": "aspect",
    "lengthRange": "length_s",
    "defaultLength": "default_length_s",
    "briefFields": "brief_fields",
    "voiceOver": "vo_mode",
    "music": "music",
}


def parse_ts_presets(text: str) -> list[dict]:
    """The `presets` array literal of presets.ts as a list of dicts (keys quoted, then JSON)."""
    match = re.search(r"export const presets: PresetData\[\] = \[\n(.*?)\n\];", text, re.DOTALL)
    assert match, "presets array not found in web/lib/presets.ts"
    body = re.sub(r"^(\s*)([A-Za-z_]\w*):", r'\1"\2":', match.group(1), flags=re.MULTILINE)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(f"[{body}]")


@pytest.fixture(scope="module")
def yaml_presets() -> list[dict]:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))["presets"]


@pytest.fixture(scope="module")
def ts_presets() -> list[dict]:
    return parse_ts_presets(TS_PATH.read_text(encoding="utf-8"))


def test_both_files_list_the_same_24_ids_in_the_same_order(yaml_presets, ts_presets):
    yaml_ids = [p["id"] for p in yaml_presets]
    ts_ids = [p["id"] for p in ts_presets]
    assert len(yaml_ids) == 24
    assert len(set(yaml_ids)) == 24
    assert ts_ids == yaml_ids


@pytest.mark.parametrize("ts_key,yaml_key", sorted(FIELD_MAP.items()))
def test_every_card_field_matches(yaml_presets, ts_presets, ts_key, yaml_key):
    by_id = {p["id"]: p for p in yaml_presets}
    mismatches = [
        f"{ts['id']}: presets.ts {ts_key}={ts.get(ts_key)!r} vs yaml {yaml_key}={by_id[ts['id']].get(yaml_key)!r}"
        for ts in ts_presets
        if ts.get(ts_key) != by_id[ts["id"]].get(yaml_key)
    ]
    assert not mismatches, "\n".join(mismatches)


def test_every_yaml_card_sets_an_explicit_default_length(yaml_presets):
    for p in yaml_presets:
        lo, hi = p["length_s"]
        assert "default_length_s" in p, p["id"]
        assert lo <= p["default_length_s"] <= hi, p["id"]


def test_parser_sees_a_drifted_field():
    """Guards the guard: a one-field edit to the TS literal must change what the parser returns."""
    text = TS_PATH.read_text(encoding="utf-8")
    drifted = text.replace("lengthRange: [10, 15],", "lengthRange: [10, 16],", 1)
    assert drifted != text
    original = parse_ts_presets(text)
    changed = parse_ts_presets(drifted)
    assert [p["lengthRange"] for p in changed] != [p["lengthRange"] for p in original]


def test_wizard_skip_window_matches_the_freeform_profile():
    from hfvg.genre_presets import FREEFORM_PROFILE

    text = WIZARD_PATH.read_text(encoding="utf-8")
    window = re.search(r"export const FREEFORM_LENGTH_RANGE = \[(\d+), (\d+)\];", text)
    default = re.search(r"export const FREEFORM_DEFAULT_LENGTH = (\d+);", text)
    assert window and default, "FREEFORM_LENGTH_RANGE / FREEFORM_DEFAULT_LENGTH not found in web/lib/wizard.mjs"
    assert [int(window.group(1)), int(window.group(2))] == list(FREEFORM_PROFILE["length_s"])
    assert int(default.group(1)) == FREEFORM_PROFILE["default_length_s"]


def test_wizard_groups_cover_every_template(yaml_presets):
    text = WIZARD_PATH.read_text(encoding="utf-8")
    block = re.search(r"export const TEMPLATE_LABELS = \{\n(.*?)\n\};", text, re.DOTALL)
    assert block, "TEMPLATE_LABELS not found in web/lib/wizard.mjs"
    labelled = re.findall(r"^\s*(\w+): '[^']+',$", block.group(1), flags=re.MULTILINE)
    assert sorted(labelled) == sorted({p["template"] for p in yaml_presets})

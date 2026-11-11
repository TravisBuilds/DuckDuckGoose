"""BEATMAP shot tables: the Screen (gen) cell forms the parser accepts, the junk it refuses with an
error that says what the cell must look like, and header tables being authoritative (a bad row is
refused even when other rows of the same table parse)."""

from pathlib import Path

import pytest

from hfvg.episode_parser import BeatmapFormatError, parse_beatmap

HEADER = ("| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
          "|---|----|--------------|------|-----|-------|--------|---------|----|-----|-------|\n")


def _row(n: int, sid: str, screen: str, model: str = "K") -> str:
    return f"| {n} | {sid} | {screen} | Kitchen | Day | A | A kettle hisses | Opening | - | - | {model} |\n"


def _beatmap(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "BEATMAP.md"
    path.write_text("# Episode Beatmap\n\n" + HEADER + "".join(rows), encoding="utf-8")
    return path


@pytest.mark.parametrize("cell,screen,gen,end", [
    ("6 (6)", 6.0, 6.0, False),
    ("6.0 (6.0)", 6.0, 6.0, False),
    ("5.5 (6.0) ⇥", 5.5, 6.0, True),
    ("6 (6)⇥", 6.0, 6.0, True),
    ("6", 6.0, 6.0, False),
    ("6s", 6.0, 6.0, False),
    ("6 s", 6.0, 6.0, False),
    ("6s (6s)", 6.0, 6.0, False),
    ("6s(6s)", 6.0, 6.0, False),
    ("6.0s (6.0s)", 6.0, 6.0, False),
    ("7 ⇥", 7.0, 7.0, True),
])
def test_screen_cell_forms_the_parser_accepts(tmp_path, cell, screen, gen, end):
    shots = parse_beatmap(_beatmap(tmp_path, _row(1, "S01", cell)))
    assert len(shots) == 1
    assert (shots[0]["screen_time"], shots[0]["gen_time"], shots[0]["has_end_frame"]) == (screen, gen, end)
    assert shots[0]["shot_id"] == "S01" and shots[0]["model"] == "K"


@pytest.mark.parametrize("cell", ["six", "6-8", "~6", "(6)", "6 (abc)", "6 sec", "1.2.3", "", "6 (6) (6)", "6x"])
def test_screen_cell_junk_is_refused_with_the_expected_format(tmp_path, cell):
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, _row(1, "S01", "6 (6)"), _row(2, "S02", cell)))
    message = str(err.value)
    assert "S02" in message
    assert repr(cell) in message
    assert "'<seconds> (<seconds>)'" in message and "'6 (6)'" in message and "no units" in message
    assert err.value.shot_id == "S02"
    assert "S02" in err.value.row


@pytest.mark.parametrize("model,code", [("K", "K"), ("S", "S"), ("k", "K"), ("kling", "K"), ("Seedance", "S")])
def test_model_cell_codes(tmp_path, model, code):
    shots = parse_beatmap(_beatmap(tmp_path, _row(1, "S01", "6 (6)", model)))
    assert shots[0]["model"] == code


@pytest.mark.parametrize("model", ["Kling 2.1", "Veo", "", "K/S", "seedance 2.5"])
def test_bad_model_row_is_refused_even_when_other_rows_parse(tmp_path, model):
    """The old regex path matched the good rows and silently dropped this one."""
    rows = [_row(1, "S01", "6 (6)"), _row(2, "S02", "5 (5)", model), _row(3, "S03", "6 (6)")]
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, *rows))
    assert "S02" in str(err.value)
    assert "must be K (Kling) or S (Seedance)" in str(err.value)
    assert repr(model) in str(err.value)


def test_pipe_inside_a_cell_is_refused_with_a_hint(tmp_path):
    row = "| 1 | S01 | 6 (6) | Kitchen | Day | A | Pours tea | Hook | - | - | K | extra |\n"
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, row))
    assert "'|'" in str(err.value) and "S01" in str(err.value)


def test_short_row_is_refused(tmp_path):
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, "| 1 | S01 | 6 (6) | Kitchen | Day |\n"))
    assert "cells" in str(err.value)


def test_shot_number_cell_must_be_a_whole_number(tmp_path):
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, _row(1, "S01", "6 (6)").replace("| 1 |", "| one |", 1)))
    assert "'one'" in str(err.value) and "S01" in str(err.value)


def test_long_cell_is_shortened_in_the_message(tmp_path):
    """A misaligned row can put the action text in the Model cell; the message quotes 40 chars at most."""
    cell = "A kettle hisses while the cat watches from the windowsill"
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(_beatmap(tmp_path, _row(1, "S01", "6 (6)", cell)))
    assert f"Model cell {cell[:37] + '...'!r} must be K (Kling) or S (Seedance)" in str(err.value)
    assert cell not in str(err.value)


def test_row_outside_the_shot_table_is_refused(tmp_path):
    path = tmp_path / "BEATMAP.md"
    path.write_text("# Episode Beatmap\n\n" + HEADER + _row(1, "S01", "6 (6)") + "\nThen:\n\n" + _row(2, "S02", "5.0 (5.0)"),
                    encoding="utf-8")
    with pytest.raises(BeatmapFormatError) as err:
        parse_beatmap(path)
    assert "BEATMAP shot S02: row is outside the shot table" in str(err.value)


def test_parse_beatmap_text_matches_the_file_reader(tmp_path):
    from hfvg.episode_parser import parse_beatmap_text

    path = _beatmap(tmp_path, _row(1, "S01", "6 (6)"), _row(2, "S02", "5s"))
    assert parse_beatmap_text(path.read_text(encoding="utf-8")) == parse_beatmap(path)


def test_beatmap_error_is_a_value_error(tmp_path):
    """Callers that catch ValueError (upload route, draft repair) keep working."""
    with pytest.raises(ValueError):
        parse_beatmap(_beatmap(tmp_path, _row(1, "S01", "junk")))


def test_header_table_parses_every_row_in_order(tmp_path):
    rows = [_row(i, f"S{i:02d}", cell) for i, cell in enumerate(["6", "5s (5s)", "4.0 (4.0) ⇥", "7 (7)"], 1)]
    shots = parse_beatmap(_beatmap(tmp_path, *rows))
    assert [s["shot_id"] for s in shots] == ["S01", "S02", "S03", "S04"]
    assert [s["screen_time"] for s in shots] == [6.0, 5.0, 4.0, 7.0]
    assert [s["shot_num"] for s in shots] == [1, 2, 3, 4]


def test_headerless_legacy_rows_still_parse(tmp_path):
    """Rows without a header line keep the legacy regex reading."""
    path = tmp_path / "BEATMAP.md"
    path.write_text("# Legacy\n\n" + _row(1, "A01", "5.0 (5.0)") + _row(2, "A02", "6.0 (6.0) ⇥", "S"),
                    encoding="utf-8")
    shots = parse_beatmap(path)
    assert [(s["shot_id"], s["screen_time"], s["has_end_frame"], s["model"]) for s in shots] == [
        ("A01", 5.0, False, "K"), ("A02", 6.0, True, "S")]


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("path", [
    FIXTURES / "sample_beatmap.md",
    Path(__file__).parent.parent / "fixtures" / "demo" / "BEATMAP.md",
    Path(__file__).parent.parent / "web" / "tests" / "fixtures" / "e2e" / "BEATMAP.md",
])
def test_committed_beatmaps_parse_the_same_through_the_header(path):
    """Header tables are authoritative now; the committed beatmaps must read exactly as the legacy
    regex read them (same shots, same fields)."""
    from hfvg.episode_parser import _legacy_rows

    content = path.read_text(encoding="utf-8")
    assert parse_beatmap(path) == _legacy_rows(content)

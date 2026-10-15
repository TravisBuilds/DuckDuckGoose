"""Tests for episode BEATMAP parser."""

from pathlib import Path

import pytest

from hfvg.episode_parser import parse_beatmap, get_model_routing, shots_by_scene


@pytest.fixture
def sample_beatmap():
    """Path to synthetic test BEATMAP fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    return fixtures_dir / "sample_beatmap.md"


@pytest.fixture
def episode3_beatmap_synthetic():
    """Synthetic ep03-format fixture matching parser's expected table structure."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
    # Write proper markdown table matching the parser's expected columns
    tmp.write("""# Episode 03 Test

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | O01 | 3.0 (4.0) | Pond | Dawn | – | Duck glides waterline wet | Cold open | – | – | K |
| 2 | O02 | 2.5 (3.5) | Pond | Dawn | – | Underwater POV pool | Immersion | – | – | K |
| 3 | O03 | 2.0 (3.0) | Pond | Dawn | – | Surface break | Transition | – | – | K |
| 4 | A01 | 2.5 (3.5) | Lobby | Morning | D R | Duck enters lobby | Arrival | – | – | S |
| 5 | A02 | 3.0 (4.0) | Lobby | Morning | – | Lobby wide | Establish | – | – | S |
| 6 | A03 | 2.5 (3.5) | Lobby | Morning | D R | Duck at desk | Check-in | – | – | S |
| 7 | A04 | 2.0 (3.0) | Lobby | Morning | D R | Duck receives key ★ | Hand-off | – | – | S |
| 8 | A05 | 3.5 (5.0) ⇥ | Lobby | Morning | D L | Tuque steal ★ | Hero | – | – | S |
| 9 | B01 | 2.0 (3.0) | Hall | Morning | D | Duck walks hallway | Navigate | – | – | S |
| 10 | C01 | 2.5 (3.5) | Tea | Afternoon | D G | Tea service water | Hospitality | – | – | K |
| 11 | C02 | 2.0 (3.0) | Tea | Afternoon | D G | Duck pours tea wet | Pouring | – | – | K |
| 12 | C03 | 2.5 (3.5) | Tea | Afternoon | D G | Guest reacts | Satisfaction | – | – | K |
| 13 | C04 | 3.0 (4.0) | Tea | Afternoon | D G | Tea cup ★ hand-off | Exchange | – | – | K |
| 14 | F01 | 2.0 (3.0) | Entrance | Evening | D | Duck exit tipping hat | Departure | – | – | S |
| 15 | F02 | 3.5 (5.0) | Entrance | Evening | D | Hero exit wide | Finale | – | – | S |
| 16 | X01 | 2.0 (3.0) | Outside | Day | D | Duck walking path | Extra | – | – | S |
| 17 | X02 | 2.0 (3.0) | Outside | Day | D | Duck on bench | Extra | – | – | S |
| 18 | X03 | 2.0 (3.0) | Outside | Day | D | Duck feeding birds | Extra | – | – | S |
| 19 | X04 | 2.0 (3.0) | Garden | Day | D | Duck in garden | Extra | – | – | S |
| 20 | X05 | 2.0 (3.0) | Garden | Day | D | Duck smelling flowers | Extra | – | – | S |
| 21 | Y01 | 2.0 (3.0) | Kitchen | Day | D | Duck cooking | Extra | – | – | S |
| 22 | Y02 | 2.0 (3.0) | Kitchen | Day | D | Duck tasting | Extra | – | – | S |
| 23 | Y03 | 2.0 (3.0) | Kitchen | Day | D | Duck plating | Extra | – | – | S |
| 24 | Z01 | 2.0 (3.0) | Bedroom | Night | D | Duck reading | Extra | – | – | S |
| 25 | Z02 | 2.0 (3.0) | Bedroom | Night | D | Duck sleeping | Extra | – | – | S |
| 26 | Z03 | 2.0 (3.0) | Bedroom | Night | D | Duck dreaming | Extra | – | – | S |
""")
    tmp.close()
    yield Path(tmp.name)
    Path(tmp.name).unlink()


@pytest.fixture
def episode5_beatmap():
    """Path to episode 5 beatmap."""
    beatmap_path = Path("fixtures/ep05/BEATMAP.md")
    if not beatmap_path.exists():
        pytest.skip("fixtures/ep05/BEATMAP.md not available")
    return beatmap_path


def test_parse_beatmap(sample_beatmap):
    """Test parsing synthetic BEATMAP fixture."""
    shots = parse_beatmap(sample_beatmap)
    
    # Should parse all 15 shots from synthetic fixture
    assert len(shots) == 15
    
    # Check first shot (O01)
    first = shots[0]
    assert first["shot_id"] == "O01"
    assert first["scene_id"] == "O"
    assert first["screen_time"] == 3.0
    assert first["gen_time"] == 4.0
    assert first["model"] == "K"
    assert "wet" in first["tags"] or "water" in first["tags"]
    
    # Check a Seedance shot (A02)
    a02 = [s for s in shots if s["shot_id"] == "A02"][0]
    assert a02["model"] == "S"
    assert a02["duck_role"] == "absent"  # No duck in A02
    
    # Check a shot with duck (A03)
    a03 = [s for s in shots if s["shot_id"] == "A03"][0]
    assert a03["duck_role"] == "host"
    assert "D" in a03["characters"]
    
    # Check hand-off shot (A04)
    a04 = [s for s in shots if s["shot_id"] == "A04"][0]
    assert a04["is_handoff"]
    assert a04["model"] == "S"
    
    # Check hero shot (A05)
    a05 = [s for s in shots if s["shot_id"] == "A05"][0]
    assert a05["is_handoff"]  # Tuque steal
    assert a05["has_end_frame"]  # Marked with ⇥


def test_parse_beatmap_episode3(episode3_beatmap_synthetic):
    """Test parsing episode 3 beatmap with original assertions (synthetic fixture)."""
    shots = parse_beatmap(episode3_beatmap_synthetic)
    
    # Should have at least 25 shots
    assert len(shots) >= 25, f"Expected at least 25 shots, got {len(shots)}"
    
    # Check first shot (O01) - original ep03 assertions
    first = shots[0]
    assert first["shot_id"] == "O01"
    assert first["scene_id"] == "O"
    assert first["screen_time"] == 3.0
    assert first["gen_time"] == 4.0
    assert first["model"] == "K"


def test_parse_beatmap_episode5(episode5_beatmap):
    """Test parsing episode 5 beatmap (4 shots)."""
    shots = parse_beatmap(episode5_beatmap)
    
    # Ep05 has 4 shots
    assert len(shots) == 4, f"Expected 4 shots from ep05, got {len(shots)}"
    
    # Check field presence for first shot
    first = shots[0]
    assert "shot_id" in first
    assert "scene_id" in first
    assert "screen_time" in first
    assert "gen_time" in first
    assert "model" in first
    assert first["shot_id"] == "A01"
    
    # Should have duck positions and roles
    with_duck = [s for s in shots if s.get("duck_role") and s["duck_role"] != "absent"]
    assert len(with_duck) > 0, "Episode must have shots with ducks"
    
    # Check that times are numeric
    for shot in shots:
        assert isinstance(shot["screen_time"], (int, float)), f"Bad screen_time: {shot}"
        assert isinstance(shot["gen_time"], (int, float)), f"Bad gen_time: {shot}"


def test_model_routing_kling(sample_beatmap):
    """Test model routing for Kling shots."""
    shots = parse_beatmap(sample_beatmap)
    
    # O01 is Kling (wet/water)
    o01 = shots[0]
    routing = get_model_routing(o01)
    
    assert routing["model"] == "kling_3.0"
    assert routing["resolution"] == "1080p"
    assert routing["draft"] is False  # Kling draft=final
    assert "Kling" in routing["reason"]


def test_model_routing_seedance_draft(sample_beatmap):
    """Test model routing for Seedance non-hero shots."""
    shots = parse_beatmap(sample_beatmap)
    
    # A02 is Seedance, not a hero shot
    a02 = [s for s in shots if s["shot_id"] == "A02"][0]
    routing = get_model_routing(a02)
    
    assert routing["model"] == "seedance_2.5"
    assert routing["resolution"] == "720p"  # Non-hero: 720p
    assert routing["draft"] is True
    assert "Seedance" in routing["reason"]


def test_model_routing_seedance_hero(sample_beatmap):
    """Test model routing for Seedance hero shots (A05, F02)."""
    shots = parse_beatmap(sample_beatmap)
    
    # A05 is hero shot with ⇥ marker
    a05 = [s for s in shots if s["shot_id"] == "A05"][0]
    routing = get_model_routing(a05)
    
    assert routing["model"] == "seedance_2.5"
    assert routing["resolution"] == "1080p"  # Hero: 1080p
    assert routing["draft"] is True
    assert "hero=True" in routing["reason"]
    
    # F02 is also hero shot
    f02 = [s for s in shots if s["shot_id"] == "F02"][0]
    routing = get_model_routing(f02)
    assert routing["resolution"] == "1080p"


def test_shots_by_scene(sample_beatmap):
    """Test grouping shots by scene."""
    shots = parse_beatmap(sample_beatmap)
    scenes = shots_by_scene(shots)
    
    # Synthetic fixture has scenes: O, A, B, C, F
    assert "O" in scenes
    assert "A" in scenes
    assert "B" in scenes
    assert "C" in scenes
    assert "F" in scenes
    
    # Scene O has 3 shots
    assert len(scenes["O"]) == 3
    
    # Scene A has 5 shots
    assert len(scenes["A"]) == 5
    
    # Scene C has 4 shots (tea service)
    assert len(scenes["C"]) == 4


def test_wet_tag_detection(sample_beatmap):
    """Test that wet/underwater shots get proper tags."""
    shots = parse_beatmap(sample_beatmap)
    
    # O02 is underwater
    o02 = [s for s in shots if s["shot_id"] == "O02"][0]
    assert "underwater" in o02["tags"]
    
    # O03 is wet/soak scene
    o03 = [s for s in shots if s["shot_id"] == "O03"][0]
    assert "wet" in o03["tags"] or "soak" in o03["tags"]
    
    # C02 is water scene (tea)
    c02 = [s for s in shots if s["shot_id"] == "C02"][0]
    assert "water" in c02["tags"] or "wet" in c02["tags"]


def test_duck_role_detection(sample_beatmap):
    """Test duck role detection."""
    shots = parse_beatmap(sample_beatmap)
    
    # O01-O03: cold open, no duck (marked with —)
    for shot_id in ["O01", "O02", "O03"]:
        shot = [s for s in shots if s["shot_id"] == shot_id][0]
        assert shot["duck_role"] == "absent"
    
    # A02: lobby wide, no duck
    a02 = [s for s in shots if s["shot_id"] == "A02"][0]
    assert a02["duck_role"] == "absent"
    
    # A03: duck present at desk (host mode)
    a03 = [s for s in shots if s["shot_id"] == "A03"][0]
    assert a03["duck_role"] == "host"
    assert "D" in a03["characters"]


def test_model_counts(sample_beatmap):
    """Test that model distribution matches BEATMAP notes."""
    shots = parse_beatmap(sample_beatmap)
    
    kling_shots = [s for s in shots if s["model"] == "K"]
    seedance_shots = [s for s in shots if s["model"] == "S"]
    
    # Synthetic fixture: 7 Kling shots (O01-O03, C01-C04), 8 Seedance (A01-A05, B01, F01-F02)
    assert len(kling_shots) == 7
    assert len(seedance_shots) == 8

"""hfvg.paths: EPISODES_DIR / DATA_DIR resolution order, read at call time, blank values unset."""

from pathlib import Path

from hfvg import paths


def test_local_dev_defaults_are_the_relative_data_dir(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("EPISODES_DIR", raising=False)
    assert paths.data_dir() == Path("data")
    assert paths.episodes_dir() == Path("data/episodes")
    assert paths.episode_dir("ep01") == Path("data/episodes/ep01")
    assert paths.outputs_dir("ep01") == Path("data/episodes/ep01/outputs")
    assert paths.outputs_dir("ep01", "A01") == Path("data/episodes/ep01/outputs/A01")
    assert paths.dry_run_outputs_dir() == Path("data/dry_run_outputs")


def test_episodes_dir_falls_back_to_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "volume"))
    monkeypatch.delenv("EPISODES_DIR", raising=False)
    assert paths.episodes_dir() == tmp_path / "volume" / "episodes"
    assert paths.episode_dir("ep01") == tmp_path / "volume" / "episodes" / "ep01"


def test_episodes_dir_wins_for_inputs_and_outputs_stay_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "volume"))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "inputs"))
    assert paths.episode_dir("ep01") == tmp_path / "inputs" / "ep01"
    assert paths.outputs_dir("ep01", "A01") == tmp_path / "volume" / "episodes" / "ep01" / "outputs" / "A01"
    assert paths.dry_run_outputs_dir() == tmp_path / "volume" / "dry_run_outputs"


def test_blank_values_mean_unset(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "  ")
    monkeypatch.setenv("EPISODES_DIR", "")
    assert paths.data_dir() == Path("data")
    assert paths.episodes_dir() == Path("data/episodes")


def test_read_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "a" / "episodes"))
    first = (paths.data_dir(), paths.episodes_dir())
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "b" / "episodes"))
    assert first == (tmp_path / "a", tmp_path / "a" / "episodes")
    assert (paths.data_dir(), paths.episodes_dir()) == (tmp_path / "b", tmp_path / "b" / "episodes")

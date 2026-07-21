"""Tests for experiment manager."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runtime.experiment_manager import (
    ExperimentError,
    create_experiment,
    delete_experiment,
    duplicate_experiment,
    export_experiment,
    list_experiments,
    rename_experiment,
    set_active_experiment,
)
from runtime.paths import PROJECT_ROOT, SEED_DIR, set_experiment


@pytest.fixture
def exp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import runtime.experiment_manager as em
    import runtime.paths as paths

    seed = tmp_path / "seed"
    experiments = tmp_path / "experiments"
    # Minimal seed copied from real seed CEO files when available
    (seed / "company" / "agents").mkdir(parents=True)
    (seed / "agents" / "ceo").mkdir(parents=True)
    (seed / "shared" / "knowledge").mkdir(parents=True)
    (seed / "runtime-data" / "conversations").mkdir(parents=True)
    (seed / "company" / "agents" / "ceo.yaml").write_text(
        "agent_id: ceo\ncreated_by: seed\ndisplay_name: CEO\n",
        encoding="utf-8",
    )
    (seed / "agents" / "ceo" / "memory.md").write_text("# mem\n", encoding="utf-8")
    (seed / "shared" / "knowledge" / ".gitkeep").write_text("", encoding="utf-8")

    monkeypatch.setattr(em, "SEED_DIR", seed)
    monkeypatch.setattr(em, "EXPERIMENTS_DIR", experiments)
    monkeypatch.setattr(em, "ACTIVE_POINTER", experiments / ".active")
    monkeypatch.setattr(paths, "SEED_DIR", seed)
    monkeypatch.setattr(paths, "EXPERIMENTS_DIR", experiments)
    monkeypatch.setattr(paths, "ACTIVE_POINTER", experiments / ".active")
    return experiments


def test_create_list_delete(exp_root: Path) -> None:
    result = create_experiment("alpha", notes="first run")
    assert result["ok"] is True
    assert (exp_root / "alpha" / "company" / "agents" / "ceo.yaml").is_file()
    meta = yaml.safe_load((exp_root / "alpha" / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["notes"] == "first run"

    items = list_experiments()
    assert len(items) == 1
    assert items[0]["name"] == "alpha"

    set_active_experiment("alpha")
    with pytest.raises(ExperimentError):
        delete_experiment("alpha")
    set_active_experiment(None)
    delete_experiment("alpha")
    assert not (exp_root / "alpha").exists()


def test_rename_duplicate_export(exp_root: Path, tmp_path: Path) -> None:
    create_experiment("src")
    rename_experiment("src", "renamed")
    assert (exp_root / "renamed").is_dir()
    assert not (exp_root / "src").exists()

    duplicate_experiment("renamed", "fork")
    assert (exp_root / "fork" / "meta.yaml").is_file()
    meta = yaml.safe_load((exp_root / "fork" / "meta.yaml").read_text(encoding="utf-8"))
    assert meta.get("duplicated_from") == "renamed"

    out = tmp_path / "out.zip"
    result = export_experiment("fork", dest_zip=out)
    assert Path(result["path"]).is_file()


def test_seed_dir_exists() -> None:
    assert SEED_DIR.is_dir()
    assert (SEED_DIR / "company" / "agents" / "ceo.yaml").is_file()
    assert PROJECT_ROOT.is_dir()


def test_set_experiment_paths(exp_root: Path) -> None:
    create_experiment("live")
    set_experiment("live", root=exp_root / "live")
    from runtime.paths import company_dir, experiment_root

    assert experiment_root() == (exp_root / "live").resolve()
    assert company_dir().name == "company"
    set_experiment(None)

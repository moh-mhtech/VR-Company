"""Project root and experiment-scoped workspace path helpers."""

from __future__ import annotations

import os
from pathlib import Path

# runtime/ is one level below project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEED_DIR = PROJECT_ROOT / "seed"
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
ACTIVE_POINTER = EXPERIMENTS_DIR / ".active"

# Set when an experiment is active (Autogen worker / supervisor).
_experiment_root: Path | None = None
_experiment_name: str | None = None


def seed_dir() -> Path:
    return SEED_DIR


def experiments_dir() -> Path:
    return EXPERIMENTS_DIR


def get_experiment_name() -> str | None:
    return _experiment_name


def experiment_root() -> Path:
    """Host path for the active experiment directory."""
    if _experiment_root is None:
        raise RuntimeError(
            "No active experiment. Create and start one "
            "(python -m runtime.main experiment create <name> --start)."
        )
    return _experiment_root


def has_active_experiment() -> bool:
    return _experiment_root is not None


def set_experiment(name: str | None, *, root: Path | None = None) -> None:
    """Bind workspace paths to an experiment (or clear when name is None)."""
    global _experiment_root, _experiment_name
    if name is None:
        _experiment_name = None
        _experiment_root = None
        return
    _experiment_name = name
    _experiment_root = (root or (EXPERIMENTS_DIR / name)).resolve()


def activate_from_env() -> str | None:
    """Load experiment from VR_EXPERIMENT or experiments/.active. Returns name or None."""
    name = (os.environ.get("VR_EXPERIMENT") or "").strip()
    if not name and ACTIVE_POINTER.is_file():
        name = ACTIVE_POINTER.read_text(encoding="utf-8").strip()
    if not name:
        set_experiment(None)
        return None
    root = EXPERIMENTS_DIR / name
    if not root.is_dir():
        set_experiment(None)
        raise FileNotFoundError(f"Experiment not found: {name}")
    set_experiment(name, root=root)
    return name


def read_active_pointer() -> str | None:
    if not ACTIVE_POINTER.is_file():
        return None
    name = ACTIVE_POINTER.read_text(encoding="utf-8").strip()
    return name or None


def write_active_pointer(name: str | None) -> None:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if name is None:
        if ACTIVE_POINTER.exists():
            ACTIVE_POINTER.unlink()
        return
    ACTIVE_POINTER.write_text(name.strip() + "\n", encoding="utf-8")


def company_dir() -> Path:
    return experiment_root() / "company"


def agents_dir() -> Path:
    return experiment_root() / "agents"


def shared_dir() -> Path:
    return experiment_root() / "shared"


def runtime_data_dir() -> Path:
    return experiment_root() / "runtime-data"


def workspace_aliases() -> dict[str, Path]:
    root = experiment_root()
    return {
        "company": root / "company",
        "shared": root / "shared",
        "accounting-view": root / "runtime-data" / "accounting" / "view",
    }


def agent_private_dir(agent_id: str) -> Path:
    return agents_dir() / agent_id


def resolve_workspace_path(logical: str, agent_id: str) -> Path:
    """Map /workspace/... logical paths to host paths."""
    path = logical.replace("\\", "/")
    if path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
    elif path.startswith("workspace/"):
        path = path[len("workspace/") :]

    if path == "self" or path.startswith("self/"):
        rest = path[5:] if path.startswith("self/") else ""
        base = agent_private_dir(agent_id)
        return (base / rest).resolve() if rest else base.resolve()

    for alias, base in workspace_aliases().items():
        if path == alias:
            return base.resolve()
        prefix = alias + "/"
        if path.startswith(prefix):
            return (base / path[len(prefix) :]).resolve()

    raise PermissionError(f"Unknown or disallowed workspace path: {logical}")

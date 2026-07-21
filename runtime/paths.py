"""Project root and workspace path helpers."""

from __future__ import annotations

from pathlib import Path

# runtime/ is one level below project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

WORKSPACE_ALIASES = {
    "company": PROJECT_ROOT / "company",
    "shared": PROJECT_ROOT / "shared",
    "accounting-view": PROJECT_ROOT / "runtime-data" / "accounting" / "view",
}


def agent_private_dir(agent_id: str) -> Path:
    return PROJECT_ROOT / "agents" / agent_id


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

    for alias, base in WORKSPACE_ALIASES.items():
        if path == alias:
            return base.resolve()
        prefix = alias + "/"
        if path.startswith(prefix):
            return (base / path[len(prefix) :]).resolve()

    raise PermissionError(f"Unknown or disallowed workspace path: {logical}")

"""Filesystem operations for multi-experiment management.

Each experiment is a directory under ``experiments/<name>/`` created by copying
``seed/``. Seed is git-tracked and immutable at runtime; experiment folders are
mutable and gitignored.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from runtime.paths import (
    ACTIVE_POINTER,
    EXPERIMENTS_DIR,
    PROJECT_ROOT,
    SEED_DIR,
    read_active_pointer,
    write_active_pointer,
)

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_RESERVED = frozenset({".active", "_seed", "seed", "exports"})


class ExperimentError(ValueError):
    """Invalid experiment name or operation."""


def validate_experiment_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ExperimentError("Experiment name is required")
    if cleaned.lower() in _RESERVED or cleaned.startswith("."):
        raise ExperimentError(f"Reserved experiment name: {cleaned}")
    if not _NAME_RE.match(cleaned):
        raise ExperimentError(
            "Experiment name must be 1–63 chars: letters, digits, hyphen, underscore "
            "(must start with a letter or digit)"
        )
    return cleaned


def experiment_path(name: str) -> Path:
    return EXPERIMENTS_DIR / validate_experiment_name(name)


def _make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove_path(path: Path, *, retries: int = 8) -> None:
    if not path.exists():
        return
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            if path.is_dir() and not path.is_symlink():
                for child in list(path.iterdir()):
                    _remove_path(child, retries=retries)
                _make_writable(path)
                path.rmdir()
            else:
                _make_writable(path)
                path.unlink(missing_ok=True)
            return
        except FileNotFoundError:
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.15 * (attempt + 1))
    if last_exc is not None:
        raise PermissionError(f"Could not remove {path}: {last_exc}") from last_exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _copy_seed_into(dest: Path) -> None:
    if not SEED_DIR.is_dir():
        raise ExperimentError(f"Seed directory missing: {SEED_DIR}")
    dest.mkdir(parents=True, exist_ok=False)
    for child in SEED_DIR.iterdir():
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    # Ensure runtime-data scaffolds exist even if seed is incomplete.
    for rel in (
        "runtime-data/conversations",
        "runtime-data/accounting/view",
        "runtime-data/logs",
    ):
        d = dest / rel
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")


def _write_meta(dest: Path, *, name: str, notes: str = "", source: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": name,
        "created_at": _utc_now(),
        "notes": notes or "",
    }
    if source:
        meta["duplicated_from"] = source
    (dest / "meta.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return meta


def _read_meta(path: Path) -> dict[str, Any]:
    meta_path = path / "meta.yaml"
    if not meta_path.is_file():
        return {"name": path.name, "notes": "", "created_at": None}
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("name", path.name)
    data.setdefault("notes", "")
    return data


def list_experiments() -> list[dict[str, Any]]:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    active = read_active_pointer()
    items: list[dict[str, Any]] = []
    for child in sorted(EXPERIMENTS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name == "exports":
            continue
        meta = _read_meta(child)
        mtime = _dir_mtime(child)
        items.append(
            {
                "name": child.name,
                "notes": str(meta.get("notes") or ""),
                "created_at": meta.get("created_at"),
                "modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                if mtime
                else None,
                "active": child.name == active,
                "path": str(child),
            }
        )
    return items


def create_experiment(name: str, *, notes: str = "") -> dict[str, Any]:
    name = validate_experiment_name(name)
    dest = EXPERIMENTS_DIR / name
    if dest.exists():
        raise ExperimentError(f"Experiment already exists: {name}")
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _copy_seed_into(dest)
        meta = _write_meta(dest, name=name, notes=notes)
    except Exception:
        if dest.exists():
            _remove_path(dest)
        raise
    logger.info("Created experiment %s from seed", name)
    return {"ok": True, "name": name, "meta": meta, "path": str(dest)}


def delete_experiment(name: str, *, allow_active: bool = False) -> dict[str, Any]:
    name = validate_experiment_name(name)
    dest = EXPERIMENTS_DIR / name
    if not dest.is_dir():
        raise ExperimentError(f"Experiment not found: {name}")
    active = read_active_pointer()
    if active == name and not allow_active:
        raise ExperimentError(
            f"Cannot delete active experiment '{name}'. Switch to another experiment or stop first."
        )
    _remove_path(dest)
    if active == name:
        write_active_pointer(None)
    logger.info("Deleted experiment %s", name)
    return {"ok": True, "name": name, "deleted": True}


def rename_experiment(old_name: str, new_name: str) -> dict[str, Any]:
    old_name = validate_experiment_name(old_name)
    new_name = validate_experiment_name(new_name)
    src = EXPERIMENTS_DIR / old_name
    dest = EXPERIMENTS_DIR / new_name
    if not src.is_dir():
        raise ExperimentError(f"Experiment not found: {old_name}")
    if dest.exists():
        raise ExperimentError(f"Experiment already exists: {new_name}")
    src.rename(dest)
    meta = _read_meta(dest)
    meta["name"] = new_name
    (dest / "meta.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    if read_active_pointer() == old_name:
        write_active_pointer(new_name)
    logger.info("Renamed experiment %s -> %s", old_name, new_name)
    return {"ok": True, "old_name": old_name, "name": new_name, "path": str(dest)}


def duplicate_experiment(source: str, new_name: str, *, notes: str | None = None) -> dict[str, Any]:
    source = validate_experiment_name(source)
    new_name = validate_experiment_name(new_name)
    src = EXPERIMENTS_DIR / source
    dest = EXPERIMENTS_DIR / new_name
    if not src.is_dir():
        raise ExperimentError(f"Experiment not found: {source}")
    if dest.exists():
        raise ExperimentError(f"Experiment already exists: {new_name}")
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dest)
        src_meta = _read_meta(src)
        note_text = notes if notes is not None else str(src_meta.get("notes") or "")
        meta = _write_meta(dest, name=new_name, notes=note_text, source=source)
    except Exception:
        if dest.exists():
            _remove_path(dest)
        raise
    logger.info("Duplicated experiment %s -> %s", source, new_name)
    return {"ok": True, "source": source, "name": new_name, "meta": meta, "path": str(dest)}


def export_experiment(name: str, *, dest_zip: Path | None = None) -> dict[str, Any]:
    name = validate_experiment_name(name)
    src = EXPERIMENTS_DIR / name
    if not src.is_dir():
        raise ExperimentError(f"Experiment not found: {name}")
    exports = EXPERIMENTS_DIR / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = dest_zip or (exports / f"{name}-{stamp}.zip")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(src.parent)))
    logger.info("Exported experiment %s to %s", name, out)
    return {"ok": True, "name": name, "path": str(out.resolve())}


def set_active_experiment(name: str | None) -> dict[str, Any]:
    """Persist which experiment should be started next (does not start the worker)."""
    if name is None:
        write_active_pointer(None)
        return {"ok": True, "active": None}
    name = validate_experiment_name(name)
    if not (EXPERIMENTS_DIR / name).is_dir():
        raise ExperimentError(f"Experiment not found: {name}")
    write_active_pointer(name)
    return {"ok": True, "active": name}


def get_active_experiment() -> str | None:
    return read_active_pointer()


def ensure_experiments_dir() -> None:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not (EXPERIMENTS_DIR / ".gitkeep").exists():
        (EXPERIMENTS_DIR / ".gitkeep").write_text("", encoding="utf-8")

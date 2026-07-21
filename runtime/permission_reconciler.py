"""Translate company access-control.yaml into path permission checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from runtime.paths import PROJECT_ROOT, agent_private_dir, resolve_workspace_path


class Access(Enum):
    NONE = "none"
    READ = "read"
    READ_WRITE = "read_write"


@dataclass
class PermissionReconciler:
    config: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> PermissionReconciler:
        path = path or (PROJECT_ROOT / "company" / "access-control.yaml")
        with path.open(encoding="utf-8") as fh:
            return cls(config=yaml.safe_load(fh) or {})

    def reload(self) -> None:
        self.config = self.load().config

    def access_for(self, agent_id: str, logical_path: str) -> Access:
        """Return access level for a logical /workspace path."""
        principals = self.config.get("principals") or {}
        principal = principals.get(agent_id) or {}
        rules = list(principal.get("rules") or [])

        # Default employee rules when no principal entry exists
        if not rules:
            defaults = self.config.get("defaults") or {}
            mapped = {
                "/workspace/company/**": defaults.get("company", "read"),
                "/workspace/shared/**": defaults.get("shared", "read_write"),
                "/workspace/self/**": defaults.get("private_self", "read_write"),
            }
            rules = [{"path": p, "access": a} for p, a in mapped.items()]

        best = Access.NONE
        norm = logical_path.replace("\\", "/")
        if not norm.startswith("/"):
            norm = "/workspace/" + norm.lstrip("/")

        for rule in rules:
            pattern = str(rule.get("path", "")).replace("\\", "/")
            if self._matches(pattern, norm):
                best = self._parse_access(rule.get("access"))
        return best

    def assert_access(self, agent_id: str, logical_path: str, need_write: bool = False) -> Path:
        access = self.access_for(agent_id, logical_path)
        if access is Access.NONE:
            raise PermissionError(f"Access denied for {agent_id} to {logical_path}")
        if need_write and access is not Access.READ_WRITE:
            raise PermissionError(f"Write denied for {agent_id} to {logical_path}")

        host = resolve_workspace_path(logical_path, agent_id)
        self._assert_not_protected(host, agent_id)
        return host

    def _assert_not_protected(self, host: Path, agent_id: str) -> None:
        host = host.resolve()
        root = PROJECT_ROOT.resolve()
        if not str(host).startswith(str(root)):
            raise PermissionError("Host filesystem escape blocked")

        runtime_dir = (root / "runtime").resolve()
        if host == runtime_dir or runtime_dir in host.parents or host.is_relative_to(runtime_dir):
            # Allow only immutable prompt read? Plan says no access to runtime/**
            raise PermissionError("Runtime directory is protected")

        raw_accounting = (root / "runtime-data" / "accounting" / "raw-usage.jsonl").resolve()
        if host == raw_accounting:
            raise PermissionError("Raw accounting storage is protected")

        creds = root / ".env"
        if host == creds.resolve():
            raise PermissionError("Credentials are protected")

        # Block other agents' private directories
        agents_root = (root / "agents").resolve()
        if agents_root in host.parents or host == agents_root:
            own = agent_private_dir(agent_id).resolve()
            if not (host == own or own in host.parents or host.is_relative_to(own)):
                # listing agents/ itself is denied for cross-access
                if host != own and not str(host).startswith(str(own)):
                    raise PermissionError("Private agent cross-access denied")

    @staticmethod
    def _parse_access(value: Any) -> Access:
        text = str(value or "none").lower()
        if text in {"read_write", "rw", "write"}:
            return Access.READ_WRITE
        if text == "read":
            return Access.READ
        return Access.NONE

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            return path == prefix or path.startswith(prefix + "/")
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if not path.startswith(prefix + "/"):
                return False
            rest = path[len(prefix) + 1 :]
            return "/" not in rest
        return path == pattern

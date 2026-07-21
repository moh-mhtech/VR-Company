"""Filesystem operations within agent permission boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from runtime.paths import PROJECT_ROOT
from runtime.permission_reconciler import PermissionReconciler


class WorkspaceManager:
    def __init__(self, permissions: PermissionReconciler, limits: dict[str, Any]) -> None:
        self.permissions = permissions
        self.limits = limits

    def read_file(self, agent_id: str, logical_path: str) -> str:
        host = self.permissions.assert_access(agent_id, logical_path, need_write=False)
        if not host.is_file():
            raise FileNotFoundError(logical_path)
        text = host.read_text(encoding="utf-8")
        max_chars = int(self.limits.get("max_file_read_chars", 100_000))
        if len(text) > max_chars:
            return text[:max_chars] + f"\n\n[truncated after {max_chars} chars]"
        return text

    def write_file(self, agent_id: str, logical_path: str, content: str) -> str:
        host = self.permissions.assert_access(agent_id, logical_path, need_write=True)
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(content, encoding="utf-8")
        return f"Wrote {logical_path} ({len(content)} chars)"

    def list_dir(self, agent_id: str, logical_path: str) -> str:
        host = self.permissions.assert_access(agent_id, logical_path, need_write=False)
        if not host.exists():
            raise FileNotFoundError(logical_path)
        if host.is_file():
            return logical_path
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in host.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    def run_code(self, agent_id: str, code: str, language: str = "python") -> str:
        """Run code in the agent's private workspace directory."""
        workdir = self.permissions.assert_access(agent_id, "/workspace/self", need_write=True)
        workdir.mkdir(parents=True, exist_ok=True)
        max_out = int(self.limits.get("max_code_output_chars", 20_000))

        if language.lower() not in {"python", "py"}:
            return f"Unsupported language: {language}"

        script = workdir / "_runtime_exec.py"
        script.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["python", str(script)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            if script.exists():
                script.unlink()

        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        out = out.strip() or f"(exit {proc.returncode}, no output)"
        if len(out) > max_out:
            out = out[:max_out] + f"\n\n[truncated after {max_out} chars]"
        return out

    def update_private_memory(self, agent_id: str, content: str) -> str:
        return self.write_file(agent_id, "/workspace/self/memory.md", content)

    def ensure_accounting_view(self) -> Path:
        view_dir = PROJECT_ROOT / "runtime-data" / "accounting" / "view"
        view_dir.mkdir(parents=True, exist_ok=True)
        return view_dir

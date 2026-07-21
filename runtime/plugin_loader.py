"""Load the mutable company accounting plugin."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from runtime.paths import PROJECT_ROOT


class PluginLoader:
    def __init__(self, plugin_path: Path | None = None) -> None:
        self.plugin_path = plugin_path or (
            PROJECT_ROOT / "company" / "accounting" / "accounting_plugin.py"
        )
        self._module: ModuleType | None = None
        self.version = "1"

    def load(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            "company_accounting_plugin", self.plugin_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load accounting plugin from {self.plugin_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        cfg = PROJECT_ROOT / "company" / "accounting" / "configuration.yaml"
        if cfg.exists():
            import yaml

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            self.version = str(data.get("version", "1"))
        return module

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            return self.load()
        return self._module

    def prepare_call(self, context: Any) -> Any:
        fn = getattr(self.module, "prepare_call", None)
        return fn(context) if callable(fn) else context

    def process_usage(self, context: Any, provider_usage: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self.module, "process_usage", None)
        if callable(fn):
            return fn(context, provider_usage)
        return dict(provider_usage)

"""Model gateway: invoke LLM, record immutable raw usage, call company plugin."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.paths import runtime_data_dir
from runtime.plugin_loader import PluginLoader


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CallContext:
    agent_id: str
    conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelGateway:
    def __init__(self, plugin_loader: PluginLoader | None = None) -> None:
        self.plugin_loader = plugin_loader or PluginLoader()
        data = runtime_data_dir()
        self.raw_log = data / "accounting" / "raw-usage.jsonl"
        self.view_dir = data / "accounting" / "view"
        self.raw_log.parent.mkdir(parents=True, exist_ok=True)
        self.view_dir.mkdir(parents=True, exist_ok=True)
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4-nano")
        self._client = None

    @staticmethod
    def _model_info_for(model: str) -> dict[str, Any] | None:
        """AutoGen only knows a fixed model list; newer names need explicit capabilities."""
        from autogen_ext.models.openai import _model_info

        try:
            _model_info.get_info(model)
            return None
        except ValueError:
            family = "gpt-5" if model.startswith("gpt-5") else "unknown"
            return {
                "vision": True,
                "function_calling": True,
                "json_output": True,
                "family": family,
                "structured_output": True,
                "multiple_system_messages": True,
            }

    def _get_client(self):
        if self._client is None:
            from autogen_ext.models.openai import OpenAIChatCompletionClient

            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
                )
            kwargs: dict[str, Any] = {"model": self.model, "api_key": api_key}
            model_info = self._model_info_for(self.model)
            if model_info is not None:
                kwargs["model_info"] = model_info
            self._client = OpenAIChatCompletionClient(**kwargs)
        return self._client

    @property
    def client(self):
        return self._get_client()

    def record_usage(self, context: CallContext, provider_usage: dict[str, Any]) -> dict[str, Any]:
        context = self.plugin_loader.prepare_call(context)
        classified = self.plugin_loader.process_usage(context, provider_usage)

        event = {
            "event_id": f"usage_{uuid.uuid4().hex[:10]}",
            "timestamp": _utcnow(),
            "agent_id": context.agent_id,
            "model": self.model,
            "prompt_tokens": provider_usage.get("prompt_tokens"),
            "completion_tokens": provider_usage.get("completion_tokens"),
            "total_tokens": provider_usage.get("total_tokens"),
            "conversation_id": context.conversation_id,
            "metadata": context.metadata,
            "accounting_plugin_version": self.plugin_loader.version,
            "classified": classified,
        }
        with self.raw_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

        # Mutable view for agents with read access
        view_file = self.view_dir / "usage-summary.jsonl"
        summary = {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "agent_id": event["agent_id"],
            "model": event["model"],
            "total_tokens": event["total_tokens"],
            "conversation_id": event["conversation_id"],
            "classified": classified,
        }
        with view_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary) + "\n")
        return event

    def extract_usage_from_result(self, result: Any) -> dict[str, Any]:
        """Best-effort token usage extraction from AutoGen / OpenAI result objects."""
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

        candidates: list[Any] = [result]
        for attr in ("usage", "model_usage", "token_usage"):
            if hasattr(result, attr):
                candidates.append(getattr(result, attr))
        if isinstance(result, dict):
            candidates.extend(result.get(k) for k in ("usage", "model_usage") if k in result)

        for obj in candidates:
            if obj is None:
                continue
            if isinstance(obj, dict):
                pt = obj.get("prompt_tokens") or obj.get("prompt_tokens")
                ct = obj.get("completion_tokens")
                tt = obj.get("total_tokens")
                if pt is not None or ct is not None or tt is not None:
                    usage = {
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "total_tokens": tt if tt is not None else ((pt or 0) + (ct or 0)),
                    }
                    break
            else:
                pt = getattr(obj, "prompt_tokens", None)
                ct = getattr(obj, "completion_tokens", None)
                tt = getattr(obj, "total_tokens", None)
                if pt is not None or ct is not None or tt is not None:
                    usage = {
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "total_tokens": tt if tt is not None else ((pt or 0) + (ct or 0)),
                    }
                    break
        return usage

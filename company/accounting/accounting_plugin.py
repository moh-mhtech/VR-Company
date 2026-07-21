"""Company-controlled accounting plugin (mutable).

Agents with sufficient access may revise this module. Raw token measurements
recorded by the runtime remain immutable regardless of plugin behavior.
"""

from __future__ import annotations

from typing import Any


def prepare_call(context: Any) -> Any:
    """Optionally enrich call metadata before the model is invoked."""
    return context


def process_usage(context: Any, provider_usage: dict[str, Any]) -> dict[str, Any]:
    """Classify usage for company reporting. Must not alter raw measurements."""
    metadata = getattr(context, "metadata", None) or {}
    if isinstance(context, dict):
        metadata = context.get("metadata") or {}
        agent_id = context.get("agent_id")
    else:
        agent_id = getattr(context, "agent_id", None)

    return {
        "agent_id": agent_id,
        "project_id": metadata.get("project_id"),
        "activity": metadata.get("activity", "unallocated"),
        "prompt_tokens": provider_usage.get("prompt_tokens"),
        "completion_tokens": provider_usage.get("completion_tokens"),
        "total_tokens": provider_usage.get("total_tokens"),
    }

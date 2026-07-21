"""Optional AgentOps observability for live session visualization."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_enabled = False


def init_agentops() -> bool:
    """Initialize AgentOps if AGENTOPS_API_KEY is set. Safe no-op otherwise."""
    global _enabled
    api_key = (os.getenv("AGENTOPS_API_KEY") or "").strip()
    if not api_key or api_key.startswith("your-"):
        logger.info(
            "AgentOps disabled (set AGENTOPS_API_KEY in .env to enable live session traces at "
            "https://app.agentops.ai )"
        )
        _enabled = False
        return False

    try:
        import agentops
    except ImportError:
        logger.warning("AGENTOPS_API_KEY is set but agentops is not installed. Run: pip install agentops")
        _enabled = False
        return False

    agentops.init(
        api_key=api_key,
        default_tags=["vr-company", "autogen-agentchat"],
        trace_name="vr-company-runtime",
        # OpenAI 2.x + AgentOps instrumentor can break on missing openai.resources.beta.chat.
        # Keep session tracing; skip fragile LLM monkeypatching.
        instrument_llm_calls=False,
        auto_start_session=True,
        log_session_replay_url=True,
        fail_safe=True,
    )
    try:
        from runtime.console_encoding import harden_console

        harden_console()
    except Exception:  # noqa: BLE001
        pass
    _enabled = True
    logger.info("AgentOps enabled — view sessions at https://app.agentops.ai/drilldown")
    return True


def end_agentops(end_state: str = "Success") -> None:
    """Flush/close the AgentOps session on runtime shutdown."""
    global _enabled
    if not _enabled:
        return
    try:
        import agentops

        end_session = getattr(agentops, "end_session", None)
        if callable(end_session):
            end_session(end_state)
        else:
            end_trace = getattr(agentops, "end_trace", None)
            if callable(end_trace):
                end_trace()
        logger.info("AgentOps session ended (%s)", end_state)
    except Exception:  # noqa: BLE001 — never block shutdown
        logger.exception("Failed to end AgentOps session")
    finally:
        _enabled = False


def record_agent_message(
    *,
    agent_id: str,
    acting_as: str,
    recipient: str,
    conversation_id: str | None,
    content_preview: str,
) -> None:
    """Best-effort custom event for board/client turns (optional enrichment)."""
    if not _enabled:
        return
    try:
        import agentops

        record = getattr(agentops, "record", None)
        if not callable(record):
            return
        # Keep payloads small; full transcripts live in runtime-data/conversations
        preview = content_preview if len(content_preview) <= 500 else content_preview[:500] + "…"
        ActionEvent = getattr(agentops, "ActionEvent", None)
        if ActionEvent is not None:
            record(
                ActionEvent(
                    action_type="human_message",
                    params={
                        "acting_as": acting_as,
                        "agent_id": agent_id,
                        "recipient": recipient,
                        "conversation_id": conversation_id,
                        "content_preview": preview,
                    },
                )
            )
    except Exception:  # noqa: BLE001
        logger.debug("AgentOps custom event skipped", exc_info=True)


def is_enabled() -> bool:
    return _enabled

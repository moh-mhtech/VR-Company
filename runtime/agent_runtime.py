"""Generic AutoGen agent factory and company runtime orchestration."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from runtime.event_bus import EventBus, preview
from runtime.message_router import MessageRouter
from runtime.model_gateway import CallContext, ModelGateway
from runtime.paths import (
    PROJECT_ROOT,
    agent_private_dir,
    company_dir,
    experiment_root,
    runtime_data_dir,
)
from runtime.permission_reconciler import PermissionReconciler
from runtime.plugin_loader import PluginLoader
from runtime.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

# Prevent runaway agent-to-agent ping-pong within one client turn.
_delivery_depth: ContextVar[int] = ContextVar("delivery_depth", default=0)
_MAX_DELIVERY_DEPTH = 6


def _load_limits() -> dict[str, Any]:
    path = PROJECT_ROOT / "runtime" / "limits.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@dataclass
class AgentSpec:
    agent_id: str
    display_name: str
    created_by: str
    manager: str | None
    system_prompt_files: list[str]
    initial_context_files: list[str]
    private_directory: str
    capabilities: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> AgentSpec:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            agent_id=data["agent_id"],
            display_name=data.get("display_name") or data["agent_id"],
            created_by=data.get("created_by") or "unknown",
            manager=data.get("manager"),
            system_prompt_files=list(data.get("system_prompt_files") or []),
            initial_context_files=list(data.get("initial_context_files") or []),
            private_directory=data.get("private_directory") or f"agents/{data['agent_id']}",
            capabilities=list(data.get("capabilities") or []),
            raw=data,
        )


@dataclass
class ActiveAgent:
    spec: AgentSpec
    assistant: Any | None = None


class CompanyRuntime:
    """Central runtime: agents, messaging, tools, model gateway."""

    def __init__(self) -> None:
        self.limits = _load_limits()
        buffer_size = int(self.limits.get("event_buffer_size") or 500)
        self.events = EventBus(buffer_size=buffer_size)
        self.permissions = PermissionReconciler.load()
        self.workspace = WorkspaceManager(self.permissions, self.limits)
        self.router = MessageRouter(event_bus=self.events)
        self.plugins = PluginLoader()
        self.gateway = ModelGateway(self.plugins)
        self.active: dict[str, ActiveAgent] = {}
        self.state_path = runtime_data_dir() / "active-agents.json"
        self.workspace.ensure_accounting_view()
        self._admin_lock = threading.Lock()

    def start(self) -> None:
        self.plugins.load()
        self._ensure_seed_ceo()
        self._start_all_specs()
        self._persist_active()
        logger.info("Runtime started with agents: %s", ", ".join(self.active) or "(none)")
        self.events.emit(
            "runtime.ready",
            agents=[
                {"agent_id": aid, "display_name": self.active[aid].spec.display_name}
                for aid in sorted(self.active)
            ],
        )

    def restart_session(self) -> dict[str, Any]:
        """Drop in-memory AutoGen assistants and start a fresh observability session.

        Keeps filesystem state (conversations, company docs, agent memory).
        """
        with self._admin_lock:
            for active in self.active.values():
                active.assistant = None
            self.events.clear()
            try:
                from runtime.observability import end_agentops, init_agentops

                end_agentops("Indeterminate")
                init_agentops()
            except Exception:  # noqa: BLE001
                logger.exception("Observability session restart failed")
            self.events.emit(
                "session.restarted",
                agents=[{"agent_id": aid} for aid in sorted(self.active)],
            )
            logger.info("AutoGen session restarted (%d agents)", len(self.active))
            return {"ok": True, "agents": list(sorted(self.active))}

    def _ensure_seed_ceo(self) -> None:
        ceo_yaml = company_dir() / "agents" / "ceo.yaml"
        if ceo_yaml.is_file() and "ceo" not in self.active:
            self.create_agent_from_spec(ceo_yaml, start=True)

    def _start_all_specs(self) -> None:
        """Start every company/agents/*.yaml so handoffs are not blocked by stopped workers."""
        agents_yaml = company_dir() / "agents"
        for path in sorted(agents_yaml.glob("*.yaml")):
            try:
                spec = AgentSpec.from_yaml(path)
            except Exception:  # noqa: BLE001
                logger.exception("Skipping invalid agent spec %s", path)
                continue
            if spec.agent_id not in self.active:
                self.create_agent_from_spec(path, start=True)

    def list_agents(self) -> list[dict[str, Any]]:
        result = []
        for agent_id, active in self.active.items():
            result.append(
                {
                    "agent_id": agent_id,
                    "display_name": active.spec.display_name,
                    "manager": active.spec.manager,
                    "status": "active",
                }
            )
        # Also list specs on disk that are not running
        agents_yaml = company_dir() / "agents"
        for path in sorted(agents_yaml.glob("*.yaml")):
            spec = AgentSpec.from_yaml(path)
            if spec.agent_id not in self.active:
                result.append(
                    {
                        "agent_id": spec.agent_id,
                        "display_name": spec.display_name,
                        "manager": spec.manager,
                        "status": "stopped",
                    }
                )
        return result

    def create_agent_from_spec(self, spec_path: Path | str, start: bool = True) -> str:
        path = Path(spec_path)
        if not path.is_absolute():
            path = experiment_root() / path
        spec = AgentSpec.from_yaml(path)
        private = agent_private_dir(spec.agent_id)
        private.mkdir(parents=True, exist_ok=True)
        (private / "journal").mkdir(exist_ok=True)
        (private / "artifacts").mkdir(exist_ok=True)
        mem = private / "memory.md"
        if not mem.exists():
            mem.write_text("# Agent private memory\n\n_No durable notes yet._\n", encoding="utf-8")
        work = private / "current-work.md"
        if not work.exists():
            work.write_text("# Current work\n\n_No active work recorded._\n", encoding="utf-8")

        if start:
            self._start_agent(spec)
        self._persist_active()
        return spec.agent_id

    def stop_agent(self, agent_id: str) -> str:
        if agent_id not in self.active:
            return f"Agent {agent_id} is not active"
        display_name = self.active[agent_id].spec.display_name
        del self.active[agent_id]
        self._persist_active()
        self.events.emit("agent.stopped", agent_id=agent_id, display_name=display_name)
        return f"Stopped agent {agent_id}"

    def _start_agent(self, spec: AgentSpec) -> ActiveAgent:
        # Lazily build the AutoGen assistant on first message so the TCP
        # server can start before OPENAI_API_KEY is configured.
        active = ActiveAgent(spec=spec, assistant=None)
        self.active[spec.agent_id] = active
        self.events.emit(
            "agent.started",
            agent_id=spec.agent_id,
            display_name=spec.display_name,
            manager=spec.manager,
        )
        return active

    def _ensure_assistant(self, agent_id: str) -> Any:
        active = self.active[agent_id]
        # Always rebuild so mutable company prompts/docs apply on the next turn.
        spec_path = company_dir() / "agents" / f"{agent_id}.yaml"
        if spec_path.is_file():
            active.spec = AgentSpec.from_yaml(spec_path)
        active.assistant = self._build_assistant(active.spec)
        return active.assistant

    def _build_system_message(self, spec: AgentSpec) -> str:
        immutable = _read_text(PROJECT_ROOT / "runtime" / "immutable_runtime_prompt.txt")
        parts = [immutable.strip(), "", "---", "", f"You are agent `{spec.agent_id}` ({spec.display_name}).", ""]
        root = experiment_root()
        for rel in spec.system_prompt_files:
            path = root / rel
            if path.is_file():
                parts.append(_read_text(path).strip())
                parts.append("")
        parts.append("## Initial company context")
        for rel in spec.initial_context_files:
            path = root / rel
            if path.is_file():
                parts.append(f"### {rel}")
                parts.append(_read_text(path).strip())
                parts.append("")
        # Private memory snapshot
        mem = agent_private_dir(spec.agent_id) / "memory.md"
        if mem.is_file():
            parts.append("## Your private memory")
            parts.append(_read_text(mem).strip())
        manifest = company_dir() / "manifest.yaml"
        if manifest.is_file():
            parts.append("")
            parts.append("## Company manifest")
            parts.append(_read_text(manifest).strip())
        parts.append("")
        parts.append(
            "Use tools to read/write files, message others, manage agents, and record memory. "
            "Logical paths use /workspace/company, /workspace/shared, /workspace/self, "
            "/workspace/accounting-view."
        )
        return "\n".join(parts)

    def _instrument_tool(self, agent_id: str, tool_name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap an async tool so each call emits tool.called / tool.result events."""
        import functools
        import inspect

        runtime = self
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            bound = sig.bind_partial(*args, **kwargs)
            arg_preview = {k: preview(v) for k, v in bound.arguments.items()}
            runtime.events.emit(
                "tool.called",
                agent_id=agent_id,
                tool=tool_name,
                call_id=call_id,
                args=arg_preview,
            )
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                runtime.events.emit(
                    "tool.result",
                    agent_id=agent_id,
                    tool=tool_name,
                    call_id=call_id,
                    ok=False,
                    error=str(exc),
                    result_preview="",
                )
                raise
            runtime.events.emit(
                "tool.result",
                agent_id=agent_id,
                tool=tool_name,
                call_id=call_id,
                ok=True,
                result_preview=preview(result, limit=600),
            )
            return result

        wrapped.__signature__ = sig  # type: ignore[attr-defined]
        return wrapped

    def _build_tools(self, spec: AgentSpec) -> list[Any]:
        from autogen_core.tools import FunctionTool

        agent_id = spec.agent_id
        caps = set(spec.capabilities)
        tools: list[Any] = []
        runtime = self

        def add_tool(fn: Callable[..., Any], description: str, name: str | None = None) -> None:
            tool_name = name or getattr(fn, "__name__", "tool")
            instrumented = runtime._instrument_tool(agent_id, tool_name, fn)
            tools.append(FunctionTool(instrumented, description=description, name=tool_name))

        if "filesystem" in caps or "modify_company" in caps:

            async def read_file(path: str) -> str:
                """Read a permitted workspace file. Use /workspace/... paths."""
                return runtime.workspace.read_file(agent_id, path)

            async def write_file(path: str, content: str) -> str:
                """Write a permitted workspace file. Use /workspace/... paths."""
                runtime.permissions.reload()
                return runtime.workspace.write_file(agent_id, path, content)

            async def list_files(path: str) -> str:
                """List files in a permitted workspace directory."""
                return runtime.workspace.list_dir(agent_id, path)

            add_tool(read_file, "Read a permitted workspace file.")
            add_tool(write_file, "Write a permitted workspace file.")
            add_tool(list_files, "List a permitted workspace directory.")

        if "code_execution" in caps:

            async def run_python(code: str) -> str:
                """Run Python code in your private workspace directory."""
                return runtime.workspace.run_code(agent_id, code, language="python")

            add_tool(run_python, "Run Python in your private workspace.")

        if "messaging" in caps:

            async def send_message(recipient: str, content: str, conversation_id: str = "") -> str:
                """Send a private message to another agent or human (e.g. human:board, human:client).

                Messages to other agents are delivered immediately (the recipient runs and may reply).
                Do not reuse a client conversation id when contacting internal agents — a new
                private thread is created unless the recipient is already a participant.
                """
                if recipient == agent_id:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "Cannot message yourself. Implement the work with tools "
                            "or message a different agent id.",
                        }
                    )

                cid: str | None = conversation_id or None
                if cid and not recipient.startswith("human:"):
                    try:
                        parts = runtime.router.participants_of(cid)
                        if recipient not in parts:
                            cid = None
                    except FileNotFoundError:
                        cid = None

                record = runtime.router.send(
                    sender=agent_id,
                    recipient=recipient,
                    content=content,
                    conversation_id=cid,
                )
                if not recipient.startswith("human:") and recipient != agent_id:
                    try:
                        reply = await runtime.deliver_to_agent(
                            recipient=recipient,
                            content=content,
                            conversation_id=record["conversation_id"],
                            from_id=agent_id,
                        )
                        record["delivery"] = {
                            "status": "delivered",
                            "reply_preview": (reply or "")[:800],
                        }
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Agent delivery to %s failed", recipient)
                        record["delivery"] = {"status": "failed", "error": str(exc)}
                return json.dumps(record)

            async def read_inbox(limit: int = 10) -> str:
                """Read recent private messages where you are a participant."""
                return json.dumps(runtime.router.inbox(agent_id, limit=limit), indent=2)

            add_tool(
                send_message,
                (
                    "Send a private message. Humans: human:board / human:client. "
                    "Other agents are woken immediately and may reply in this call."
                ),
            )
            add_tool(read_inbox, "Read your private message inbox.")

        if "list_agents" in caps:

            async def list_agents() -> str:
                """List known and active agents."""
                return json.dumps(runtime.list_agents(), indent=2)

            add_tool(list_agents, "List company agents.")

        if "create_agent" in caps:

            async def create_agent(spec_relative_path: str) -> str:
                """Create/start an agent from a YAML spec under company/agents/."""
                return runtime.create_agent_from_spec(spec_relative_path, start=True)

            add_tool(
                create_agent,
                "Create and start an agent from a company/agents/*.yaml spec path.",
            )

        if "stop_agent" in caps:

            async def stop_agent(target_agent_id: str) -> str:
                """Stop a running agent by id."""
                return runtime.stop_agent(target_agent_id)

            add_tool(stop_agent, "Stop a running agent.")

        if "read_usage" in caps:

            async def read_usage(limit: int = 20) -> str:
                """Read the company token usage view (not raw immutable storage)."""
                view = runtime_data_dir() / "accounting" / "view" / "usage-summary.jsonl"
                if not view.is_file():
                    return "[]"
                lines = [ln for ln in view.read_text(encoding="utf-8").splitlines() if ln.strip()]
                return "\n".join(lines[-limit:])

            add_tool(read_usage, "Read token usage summary view.")

        async def update_memory(content: str) -> str:
            """Replace your private memory.md with durable notes."""
            return runtime.workspace.update_private_memory(agent_id, content)

        add_tool(update_memory, "Update your private memory.md.")
        return tools

    def _build_assistant(self, spec: AgentSpec) -> Any:
        import inspect

        from autogen_agentchat.agents import AssistantAgent

        kwargs: dict[str, Any] = {
            "name": spec.agent_id.replace("-", "_"),
            "model_client": self.gateway.client,
            "tools": self._build_tools(spec),
            "system_message": self._build_system_message(spec),
            "reflect_on_tool_use": True,
        }
        # Compatible with both AutoGen 0.5.x and 0.7.x AssistantAgent signatures.
        params = inspect.signature(AssistantAgent.__init__).parameters
        if "max_tool_iterations" in params:
            kwargs["max_tool_iterations"] = int(self.limits.get("max_agent_turns", 20))
        return AssistantAgent(**kwargs)

    def _persist_active(self) -> None:
        payload = {
            "agents": [
                {"agent_id": a, "display_name": self.active[a].spec.display_name}
                for a in sorted(self.active)
            ]
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def handle_human_message(
        self,
        *,
        acting_as: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Route a human message to an agent and return the agent reply."""
        human_id = f"human:{acting_as}" if not acting_as.startswith("human:") else acting_as
        record = self.router.send(
            sender=human_id,
            recipient=recipient,
            content=content,
            conversation_id=conversation_id,
            acting_as=human_id,
        )
        cid = record["conversation_id"]

        if recipient not in self.active:
            # Try to start from spec
            spec_path = company_dir() / "agents" / f"{recipient}.yaml"
            if spec_path.is_file():
                self.create_agent_from_spec(spec_path, start=True)
            else:
                return {
                    "ok": False,
                    "error": f"Recipient agent '{recipient}' is not active and has no spec",
                    "conversation_id": cid,
                    "message": record,
                }

        reply_text = await self._run_agent(recipient, content, conversation_id=cid, from_id=human_id)

        # If the agent already messaged this human via send_message during the turn,
        # reuse that content and do not write a duplicate outbound message.
        recent = self.router.history_for(cid, human_id, limit=10)
        already = [
            m
            for m in recent
            if m.get("sender") == recipient and m.get("recipient") == human_id
        ]
        if already and (already[-1].get("content") or "").strip():
            reply_record = already[-1]
            reply_text = reply_record["content"]
        else:
            reply_record = self.router.send(
                sender=recipient,
                recipient=human_id,
                content=reply_text,
                conversation_id=cid,
            )
        return {
            "ok": True,
            "conversation_id": cid,
            "inbound": record,
            "reply": reply_record,
            "content": reply_text,
        }

    def ensure_agent_running(self, agent_id: str) -> None:
        if agent_id in self.active:
            return
        spec_path = company_dir() / "agents" / f"{agent_id}.yaml"
        if not spec_path.is_file():
            raise FileNotFoundError(f"No agent spec for '{agent_id}' at {spec_path}")
        self.create_agent_from_spec(spec_path, start=True)

    async def deliver_to_agent(
        self,
        *,
        recipient: str,
        content: str,
        conversation_id: str,
        from_id: str,
    ) -> str:
        """Wake an agent to process an inbound private message and record their reply."""
        depth = _delivery_depth.get()
        if depth >= _MAX_DELIVERY_DEPTH:
            raise RuntimeError(
                f"Agent delivery depth limit ({_MAX_DELIVERY_DEPTH}) reached; "
                f"message to {recipient} was stored but not processed"
            )
        token = _delivery_depth.set(depth + 1)
        try:
            self.ensure_agent_running(recipient)
            reply_text = await self._run_agent(
                recipient,
                content,
                conversation_id=conversation_id,
                from_id=from_id,
            )
            self.router.send(
                sender=recipient,
                recipient=from_id,
                content=reply_text,
                conversation_id=conversation_id,
            )
            return reply_text
        finally:
            _delivery_depth.reset(token)

    async def _run_agent(
        self,
        agent_id: str,
        user_content: str,
        *,
        conversation_id: str,
        from_id: str,
    ) -> str:
        from autogen_agentchat.messages import TextMessage
        from autogen_core import CancellationToken

        started = time.perf_counter()
        self.events.emit(
            "turn.started",
            agent_id=agent_id,
            conversation_id=conversation_id,
            from_id=from_id,
            content_preview=preview(user_content, limit=300),
        )
        try:
            assistant = self._ensure_assistant(agent_id)
            # Reload permissions so access-control changes apply between turns.
            self.permissions.reload()

            history = self.router.history_for(conversation_id, agent_id, limit=30)
            history_text = "\n".join(
                f"[{m.get('timestamp')}] {m.get('sender')} -> {m.get('recipient')}: {m.get('content')}"
                for m in history[:-1]  # exclude the just-appended inbound if duplicated
            )
            prompt = (
                f"You have a new private message in conversation `{conversation_id}` "
                f"from `{from_id}`.\n\n"
                f"Recent conversation:\n{history_text or '(none)'}\n\n"
                f"Latest message:\n{user_content}\n\n"
                "Respond as your role. Keep the final reply concise.\n\n"
                "MANDATORY TOOL RULES:\n"
                "- If work requires another employee (CEO, software-dev, sales, etc.), you MUST "
                "call send_message to their agent_id in THIS turn before telling a human that "
                "work is underway, assigned, or 'being checked'.\n"
                "- Reading the inbox is not enough. If you lack status, message the responsible agent.\n"
                "- For client implementation requests: message `software-dev` with the full scope and "
                "an explicit instruction to create a NEW project under /workspace/shared/projects/. "
                "Wait for delivery.reply_preview.\n"
                "- If a coworker's reply is a blocker you can resolve (e.g. they ask for an existing "
                "codebase on greenfield work), send_message them AGAIN in this same turn with the "
                "correction and ask them to implement now. Do not end the turn after one blocked reply.\n"
                "- Never send_message to yourself.\n"
                "- Software builders: empty shared/projects means create files, not stop.\n"
                "- Do not claim you contacted someone unless send_message returned a delivery result.\n"
                "- Do NOT call send_message to the human you are already answering "
                f"({from_id}); your final assistant text is delivered to them automatically.\n"
                "- Persist important status with update_memory before you finish."
            )

            result = await assistant.run(
                task=prompt,
                cancellation_token=CancellationToken(),
            )

            usage = self.gateway.extract_usage_from_result(result)
            # Also try messages for usage
            if hasattr(result, "messages"):
                for msg in reversed(list(result.messages)):
                    u = self.gateway.extract_usage_from_result(msg)
                    if any(v is not None for v in u.values()):
                        usage = u
                        break

            self.gateway.record_usage(
                CallContext(agent_id=agent_id, conversation_id=conversation_id, metadata={}),
                usage,
            )

            text = self._result_to_text(result)
            self.events.emit(
                "turn.ended",
                agent_id=agent_id,
                conversation_id=conversation_id,
                from_id=from_id,
                ok=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
                reply_preview=preview(text, limit=400),
            )
            return text
        except Exception as exc:  # noqa: BLE001
            self.events.emit(
                "turn.ended",
                agent_id=agent_id,
                conversation_id=conversation_id,
                from_id=from_id,
                ok=False,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
            )
            raise

    @staticmethod
    def _result_to_text(result: Any) -> str:
        if hasattr(result, "messages") and result.messages:
            for msg in reversed(list(result.messages)):
                content = getattr(msg, "content", None)
                source = getattr(msg, "source", None)
                if isinstance(content, str) and content.strip():
                    # Prefer final assistant text
                    if source and source != "user":
                        return content.strip()
            last = result.messages[-1]
            content = getattr(last, "content", None)
            if isinstance(content, str):
                return content.strip()
        return str(result)

"""Generic AutoGen agent factory and company runtime orchestration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from runtime.message_router import MessageRouter
from runtime.model_gateway import CallContext, ModelGateway
from runtime.paths import PROJECT_ROOT, agent_private_dir
from runtime.permission_reconciler import PermissionReconciler
from runtime.plugin_loader import PluginLoader
from runtime.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


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
        self.permissions = PermissionReconciler.load()
        self.workspace = WorkspaceManager(self.permissions, self.limits)
        self.router = MessageRouter()
        self.plugins = PluginLoader()
        self.gateway = ModelGateway(self.plugins)
        self.active: dict[str, ActiveAgent] = {}
        self.state_path = PROJECT_ROOT / "runtime-data" / "active-agents.json"
        self.workspace.ensure_accounting_view()

    def start(self) -> None:
        self.plugins.load()
        self._ensure_seed_ceo()
        self._persist_active()
        logger.info("Runtime started with agents: %s", ", ".join(self.active) or "(none)")

    def _ensure_seed_ceo(self) -> None:
        ceo_yaml = PROJECT_ROOT / "company" / "agents" / "ceo.yaml"
        if ceo_yaml.is_file() and "ceo" not in self.active:
            self.create_agent_from_spec(ceo_yaml, start=True)

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
        agents_dir = PROJECT_ROOT / "company" / "agents"
        for path in sorted(agents_dir.glob("*.yaml")):
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
            path = PROJECT_ROOT / path
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
        del self.active[agent_id]
        self._persist_active()
        return f"Stopped agent {agent_id}"

    def _start_agent(self, spec: AgentSpec) -> ActiveAgent:
        # Lazily build the AutoGen assistant on first message so the TCP
        # server can start before OPENAI_API_KEY is configured.
        active = ActiveAgent(spec=spec, assistant=None)
        self.active[spec.agent_id] = active
        return active

    def _ensure_assistant(self, agent_id: str) -> Any:
        active = self.active[agent_id]
        if active.assistant is None:
            active.assistant = self._build_assistant(active.spec)
        return active.assistant

    def _build_system_message(self, spec: AgentSpec) -> str:
        immutable = _read_text(PROJECT_ROOT / "runtime" / "immutable_runtime_prompt.txt")
        parts = [immutable.strip(), "", "---", "", f"You are agent `{spec.agent_id}` ({spec.display_name}).", ""]
        for rel in spec.system_prompt_files:
            path = PROJECT_ROOT / rel
            if path.is_file():
                parts.append(_read_text(path).strip())
                parts.append("")
        parts.append("## Initial company context")
        for rel in spec.initial_context_files:
            path = PROJECT_ROOT / rel
            if path.is_file():
                parts.append(f"### {rel}")
                parts.append(_read_text(path).strip())
                parts.append("")
        # Private memory snapshot
        mem = agent_private_dir(spec.agent_id) / "memory.md"
        if mem.is_file():
            parts.append("## Your private memory")
            parts.append(_read_text(mem).strip())
        manifest = PROJECT_ROOT / "company" / "manifest.yaml"
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

    def _build_tools(self, spec: AgentSpec) -> list[Any]:
        from autogen_core.tools import FunctionTool

        agent_id = spec.agent_id
        caps = set(spec.capabilities)
        tools: list[Any] = []
        runtime = self

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

            tools.extend(
                [
                    FunctionTool(read_file, description="Read a permitted workspace file."),
                    FunctionTool(write_file, description="Write a permitted workspace file."),
                    FunctionTool(list_files, description="List a permitted workspace directory."),
                ]
            )

        if "code_execution" in caps:

            async def run_python(code: str) -> str:
                """Run Python code in your private workspace directory."""
                return runtime.workspace.run_code(agent_id, code, language="python")

            tools.append(FunctionTool(run_python, description="Run Python in your private workspace."))

        if "messaging" in caps:

            async def send_message(recipient: str, content: str, conversation_id: str = "") -> str:
                """Send a private message to another agent or human (e.g. human:board, human:client)."""
                cid = conversation_id or None
                record = runtime.router.send(
                    sender=agent_id,
                    recipient=recipient,
                    content=content,
                    conversation_id=cid,
                )
                # Deliver to recipient agent if active
                if recipient in runtime.active and recipient != agent_id:
                    # Queued only; recipient processes when addressed. Store for inbox.
                    pass
                return json.dumps(record)

            async def read_inbox(limit: int = 10) -> str:
                """Read recent private messages where you are a participant."""
                return json.dumps(runtime.router.inbox(agent_id, limit=limit), indent=2)

            tools.extend(
                [
                    FunctionTool(send_message, description="Send a private message to a recipient."),
                    FunctionTool(read_inbox, description="Read your private message inbox."),
                ]
            )

        if "list_agents" in caps:

            async def list_agents() -> str:
                """List known and active agents."""
                return json.dumps(runtime.list_agents(), indent=2)

            tools.append(FunctionTool(list_agents, description="List company agents."))

        if "create_agent" in caps:

            async def create_agent(spec_relative_path: str) -> str:
                """Create/start an agent from a YAML spec under company/agents/."""
                return runtime.create_agent_from_spec(spec_relative_path, start=True)

            tools.append(
                FunctionTool(
                    create_agent,
                    description="Create and start an agent from a company/agents/*.yaml spec path.",
                )
            )

        if "stop_agent" in caps:

            async def stop_agent(target_agent_id: str) -> str:
                """Stop a running agent by id."""
                return runtime.stop_agent(target_agent_id)

            tools.append(FunctionTool(stop_agent, description="Stop a running agent."))

        if "read_usage" in caps:

            async def read_usage(limit: int = 20) -> str:
                """Read the company token usage view (not raw immutable storage)."""
                view = PROJECT_ROOT / "runtime-data" / "accounting" / "view" / "usage-summary.jsonl"
                if not view.is_file():
                    return "[]"
                lines = [ln for ln in view.read_text(encoding="utf-8").splitlines() if ln.strip()]
                return "\n".join(lines[-limit:])

            tools.append(FunctionTool(read_usage, description="Read token usage summary view."))

        async def update_memory(content: str) -> str:
            """Replace your private memory.md with durable notes."""
            return runtime.workspace.update_private_memory(agent_id, content)

        tools.append(FunctionTool(update_memory, description="Update your private memory.md."))
        return tools

    def _build_assistant(self, spec: AgentSpec) -> Any:
        from autogen_agentchat.agents import AssistantAgent

        return AssistantAgent(
            name=spec.agent_id.replace("-", "_"),
            model_client=self.gateway.client,
            tools=self._build_tools(spec),
            system_message=self._build_system_message(spec),
            reflect_on_tool_use=True,
            max_tool_iterations=int(self.limits.get("max_agent_turns", 20)),
        )

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
            spec_path = PROJECT_ROOT / "company" / "agents" / f"{recipient}.yaml"
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
            "Respond as your role. Use tools when needed. Keep the reply concise."
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
        return text

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

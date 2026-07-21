"""Private message delivery — participants only, no broadcast."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from runtime.paths import runtime_data_dir

if TYPE_CHECKING:
    from runtime.event_bus import EventBus


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MessageRouter:
    root: Path | None = None
    event_bus: EventBus | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = runtime_data_dir() / "conversations"
        self.root.mkdir(parents=True, exist_ok=True)

    def _conv_dir(self, conversation_id: str) -> Path:
        return self.root / conversation_id

    def find_or_create(self, participants: list[str], conversation_id: str | None = None) -> str:
        participants = sorted(set(participants))
        if conversation_id:
            path = self._conv_dir(conversation_id)
            path.mkdir(parents=True, exist_ok=True)
            meta = path / "participants.json"
            existing: list[str] = []
            if meta.exists():
                data = json.loads(meta.read_text(encoding="utf-8"))
                existing = list(data.get("participants") or [])
            merged = sorted(set(existing) | set(participants))
            meta.write_text(
                json.dumps({"conversation_id": conversation_id, "participants": merged}, indent=2),
                encoding="utf-8",
            )
            messages = path / "messages.jsonl"
            if not messages.exists():
                messages.write_text("", encoding="utf-8")
            return conversation_id

        # Reuse existing conversation with exact participant set when possible
        for child in self.root.iterdir():
            meta = child / "participants.json"
            if not meta.is_file():
                continue
            data = json.loads(meta.read_text(encoding="utf-8"))
            if sorted(data.get("participants") or []) == participants:
                return child.name

        conversation_id = f"conv_{uuid.uuid4().hex[:10]}"
        path = self._conv_dir(conversation_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "participants.json").write_text(
            json.dumps({"conversation_id": conversation_id, "participants": participants}, indent=2),
            encoding="utf-8",
        )
        (path / "messages.jsonl").write_text("", encoding="utf-8")
        return conversation_id

    def participants_of(self, conversation_id: str) -> list[str]:
        meta = self._conv_dir(conversation_id) / "participants.json"
        if not meta.is_file():
            raise FileNotFoundError(conversation_id)
        data = json.loads(meta.read_text(encoding="utf-8"))
        return list(data.get("participants") or [])

    def assert_participant(self, conversation_id: str, actor: str) -> None:
        if actor not in self.participants_of(conversation_id):
            raise PermissionError(f"{actor} is not a participant of {conversation_id}")

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
        acting_as: str | None = None,
    ) -> dict[str, Any]:
        participants = [sender, recipient]
        if acting_as and acting_as not in participants:
            participants.append(acting_as)
        cid = self.find_or_create(participants, conversation_id)
        self.assert_participant(cid, sender)

        record = {
            "message_id": f"msg_{uuid.uuid4().hex[:10]}",
            "timestamp": _utcnow(),
            "sender": sender,
            "recipient": recipient,
            "acting_as": acting_as or sender,
            "content": content,
        }
        path = self._conv_dir(cid) / "messages.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        out = {"conversation_id": cid, **record}
        if self.event_bus is not None:
            self.event_bus.emit("message.sent", **out)
        return out

    def history_for(self, conversation_id: str, actor: str, limit: int = 50) -> list[dict[str, Any]]:
        self.assert_participant(conversation_id, actor)
        path = self._conv_dir(conversation_id) / "messages.jsonl"
        if not path.is_file():
            return []
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        records = [json.loads(ln) for ln in lines[-limit:]]
        return records

    def inbox(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent messages across conversations where agent is a participant."""
        items: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir()):
            meta = child / "participants.json"
            if not meta.is_file():
                continue
            data = json.loads(meta.read_text(encoding="utf-8"))
            if agent_id not in (data.get("participants") or []):
                continue
            for msg in self.history_for(child.name, agent_id, limit=limit):
                items.append({"conversation_id": child.name, **msg})
        items.sort(key=lambda m: m.get("timestamp") or "")
        return items[-limit:]

    def list_conversations(self) -> list[dict[str, Any]]:
        """Observer listing of all conversations (no participant filter)."""
        results: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return results
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            meta = child / "participants.json"
            if not meta.is_file():
                continue
            data = json.loads(meta.read_text(encoding="utf-8"))
            messages_path = child / "messages.jsonl"
            last_timestamp = ""
            message_count = 0
            if messages_path.is_file():
                lines = [ln for ln in messages_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                message_count = len(lines)
                if lines:
                    try:
                        last_timestamp = json.loads(lines[-1]).get("timestamp") or ""
                    except json.JSONDecodeError:
                        last_timestamp = ""
            results.append(
                {
                    "conversation_id": child.name,
                    "participants": list(data.get("participants") or []),
                    "message_count": message_count,
                    "last_timestamp": last_timestamp,
                }
            )
        results.sort(key=lambda c: c.get("last_timestamp") or "", reverse=True)
        return results

    def history_observer(self, conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Full conversation history for operator UI (skips participant checks)."""
        path = self._conv_dir(conversation_id) / "messages.jsonl"
        if not path.is_file():
            raise FileNotFoundError(conversation_id)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        records = [json.loads(ln) for ln in lines[-limit:]]
        return records

"""In-process event bus for optional live observers (web UI, etc.).

Emit is a cheap no-op when nobody is subscribed. No external broker.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def preview(value: Any, limit: int = 400) -> str:
    """Truncate large tool args/results for event payloads."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            import json

            text = json.dumps(value, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class EventBus:
    """Fan-out structured events to asyncio subscribers + ring buffer."""

    def __init__(self, buffer_size: int = 500) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max(1, int(buffer_size)))
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_id": f"evt_{uuid.uuid4().hex[:10]}",
            "timestamp": _utcnow(),
            "type": event_type,
            **payload,
        }
        self._buffer.append(event)
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest from this slow subscriber, then retry once.
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)
        return event

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self._buffer)
        if limit <= 0:
            return items
        return items[-limit:]

    def clear(self) -> None:
        """Drop buffered events (subscribers keep their queues)."""
        self._buffer.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

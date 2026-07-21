"""Bridge between the FastAPI web process and the NDJSON TCP runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from interfaces.runtime_client import RuntimeClient, RuntimeEventStream, load_endpoint

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RuntimeBridge:
    """Control-plane client + background event subscription with reconnect."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        default_host, default_port = load_endpoint()
        self.host = host or default_host
        self.port = port or default_port
        self.on_event = on_event
        self.connected = False
        self._control = RuntimeClient(self.host, self.port)
        self._stream: RuntimeEventStream | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._subscribe_loop(), name="runtime-event-subscribe")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._stream is not None:
            await self._stream.close()
            self._stream = None
        await self._control.close()
        self.connected = False

    async def _subscribe_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                stream = RuntimeEventStream(self.host, self.port)
                await stream.connect()
                self._stream = stream
                self.connected = True
                backoff = 1.0
                logger.info("Subscribed to runtime events at %s:%s", self.host, self.port)
                async for event in stream.events():
                    if self._stop.is_set():
                        break
                    if self.on_event is not None:
                        await self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.connected = False
                logger.debug("Runtime subscribe disconnected; retrying", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    backoff = min(backoff * 1.5, 10.0)
            finally:
                if self._stream is not None:
                    await self._stream.close()
                    self._stream = None
                self.connected = False

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._control.request(payload)
        except Exception as exc:  # noqa: BLE001
            # Stale socket after Autogen restart — reconnect once.
            self.connected = False
            try:
                await self._control.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._control.connect()
                return await self._control.request(payload)
            except Exception as retry_exc:  # noqa: BLE001
                return {"ok": False, "error": str(retry_exc) or str(exc)}

    async def ping(self) -> dict[str, Any]:
        return await self.request({"op": "ping"})

    async def list_agents(self) -> dict[str, Any]:
        return await self.request({"op": "list_agents"})

    async def list_conversations(self) -> dict[str, Any]:
        return await self.request({"op": "list_conversations"})

    async def get_history(self, conversation_id: str, limit: int = 200) -> dict[str, Any]:
        return await self.request(
            {"op": "get_history", "conversation_id": conversation_id, "limit": limit}
        )

    async def get_events(self, limit: int = 100) -> dict[str, Any]:
        return await self.request({"op": "get_events", "limit": limit})

    async def send_message(
        self,
        *,
        acting_as: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": "message",
            "acting_as": acting_as,
            "recipient": recipient,
            "content": content,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return await self.request(payload)

    async def restart_session(self) -> dict[str, Any]:
        return await self.request({"op": "restart_session"})

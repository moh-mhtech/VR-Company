"""FastAPI application for the VR-Company web console.

Talks to the AutoGen worker over TCP. When started by ``runtime.main``,
experiment admin ops go through the supervisor so Autogen is stopped before
filesystem changes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from web.runtime_bridge import RuntimeBridge

STATIC_DIR = Path(__file__).resolve().parent / "static"


class MessageBody(BaseModel):
    acting_as: str = Field(description="board or client")
    recipient: str
    content: str
    conversation_id: str | None = None


class ExperimentCreateBody(BaseModel):
    name: str
    notes: str = ""
    start: bool = False


class ExperimentRenameBody(BaseModel):
    new_name: str


class ExperimentDuplicateBody(BaseModel):
    new_name: str
    notes: str | None = None


class RuntimeAPI(Protocol):
    connected: bool

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ping(self) -> dict[str, Any]: ...
    async def list_agents(self) -> dict[str, Any]: ...
    async def list_conversations(self) -> dict[str, Any]: ...
    async def get_history(self, conversation_id: str, limit: int = 200) -> dict[str, Any]: ...
    async def get_events(self, limit: int = 100) -> dict[str, Any]: ...
    async def send_message(
        self,
        *,
        acting_as: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]: ...
    async def restart_session(self) -> dict[str, Any]: ...
    async def reset_connections(self) -> None: ...


class ConnectionHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


class SupervisedBridge:
    """TCP bridge to Autogen + supervisor hooks for experiment admin ops."""

    def __init__(self, bridge: RuntimeBridge, supervisor: Any | None = None) -> None:
        self.bridge = bridge
        self.supervisor = supervisor

    @property
    def connected(self) -> bool:
        return self.bridge.connected

    async def start(self) -> None:
        await self.bridge.start()

    async def stop(self) -> None:
        await self.bridge.stop()

    async def reset_connections(self) -> None:
        await self.bridge.stop()
        await self.bridge.start()

    async def ping(self) -> dict[str, Any]:
        return await self.bridge.ping()

    async def list_agents(self) -> dict[str, Any]:
        return await self.bridge.list_agents()

    async def list_conversations(self) -> dict[str, Any]:
        return await self.bridge.list_conversations()

    async def get_history(self, conversation_id: str, limit: int = 200) -> dict[str, Any]:
        return await self.bridge.get_history(conversation_id, limit=limit)

    async def get_events(self, limit: int = 100) -> dict[str, Any]:
        return await self.bridge.get_events(limit=limit)

    async def send_message(
        self,
        *,
        acting_as: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.bridge.send_message(
            acting_as=acting_as,
            recipient=recipient,
            content=content,
            conversation_id=conversation_id,
        )

    async def restart_session(self) -> dict[str, Any]:
        if self.supervisor is not None:
            return await self.supervisor.restart_session()
        return await self.bridge.restart_session()


def create_app(
    *,
    runtime_host: str | None = None,
    runtime_port: int | None = None,
    supervisor: Any | None = None,
    company: Any | None = None,  # deprecated; ignored (Autogen runs out-of-process)
) -> FastAPI:
    del company  # kept for call-site compatibility
    hub = ConnectionHub()

    async def on_event(event: dict[str, Any]) -> None:
        await hub.broadcast({"type": "runtime_event", "event": event})

    bridge = RuntimeBridge(host=runtime_host, port=runtime_port, on_event=on_event)
    api: RuntimeAPI = SupervisedBridge(bridge, supervisor=supervisor)

    def _require_supervisor() -> Any:
        if supervisor is None:
            raise HTTPException(
                status_code=503,
                detail="Experiment management requires supervisor (python -m runtime.main)",
            )
        return supervisor

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await api.start()
        yield
        await api.stop()

    app = FastAPI(title="VR-Company Console", lifespan=lifespan)
    app.state.api = api
    app.state.hub = hub
    app.state.supervisor = supervisor

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        ping = await api.ping()
        active = None
        running = bool(ping.get("ok"))
        if supervisor is not None:
            active = supervisor.active_experiment
            running = bool(supervisor.autogen.running)
        return {
            "ok": True,
            "runtime_connected": bool(ping.get("ok")),
            "subscribe_connected": api.connected,
            "supervised": supervisor is not None,
            "experiment": ping.get("experiment") or active,
            "running": running,
            "runtime": ping,
        }

    @app.get("/api/experiments")
    async def experiments() -> dict[str, Any]:
        if supervisor is not None:
            return supervisor.list_experiments()
        from runtime.experiment_manager import get_active_experiment, list_experiments

        return {
            "ok": True,
            "experiments": list_experiments(),
            "active": get_active_experiment(),
            "running": False,
        }

    @app.post("/api/experiments")
    async def create_experiment_api(body: ExperimentCreateBody) -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.create_experiment(body.name, notes=body.notes, start=body.start)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "create failed")
        if body.start:
            await api.reset_connections()
        await hub.broadcast({"type": "admin", "action": "experiment_create", "result": resp})
        return resp

    @app.post("/api/experiments/{name}/start")
    async def start_experiment_api(name: str) -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.start_experiment(name)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "start failed")
        await api.reset_connections()
        await hub.broadcast({"type": "admin", "action": "experiment_start", "result": resp})
        return resp

    @app.post("/api/experiments/stop")
    async def stop_experiment_api() -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.stop_experiment()
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "stop failed")
        await hub.broadcast({"type": "admin", "action": "experiment_stop", "result": resp})
        return resp

    @app.delete("/api/experiments/{name}")
    async def delete_experiment_api(name: str) -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.delete_experiment(name)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "delete failed")
        await api.reset_connections()
        await hub.broadcast({"type": "admin", "action": "experiment_delete", "result": resp})
        return resp

    @app.post("/api/experiments/{name}/rename")
    async def rename_experiment_api(name: str, body: ExperimentRenameBody) -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.rename_experiment(name, body.new_name)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "rename failed")
        await api.reset_connections()
        await hub.broadcast({"type": "admin", "action": "experiment_rename", "result": resp})
        return resp

    @app.post("/api/experiments/{name}/duplicate")
    async def duplicate_experiment_api(name: str, body: ExperimentDuplicateBody) -> dict[str, Any]:
        sup = _require_supervisor()
        resp = await sup.duplicate_experiment(name, body.new_name, notes=body.notes)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "duplicate failed")
        await hub.broadcast({"type": "admin", "action": "experiment_duplicate", "result": resp})
        return resp

    @app.post("/api/experiments/{name}/export")
    async def export_experiment_api(name: str) -> FileResponse:
        sup = _require_supervisor()
        resp = await sup.export_experiment(name)
        if not resp.get("ok"):
            raise HTTPException(status_code=400, detail=resp.get("error") or "export failed")
        path = Path(resp["path"])
        return FileResponse(
            path,
            media_type="application/zip",
            filename=path.name,
        )

    @app.get("/api/agents")
    async def agents() -> dict[str, Any]:
        resp = await api.list_agents()
        if not resp.get("ok"):
            raise HTTPException(status_code=503, detail=resp.get("error") or "runtime unavailable")
        return resp

    @app.get("/api/conversations")
    async def conversations() -> dict[str, Any]:
        resp = await api.list_conversations()
        if not resp.get("ok"):
            raise HTTPException(status_code=503, detail=resp.get("error") or "runtime unavailable")
        return resp

    @app.get("/api/conversations/{conversation_id}")
    async def conversation_history(conversation_id: str, limit: int = 200) -> dict[str, Any]:
        resp = await api.get_history(conversation_id, limit=limit)
        if not resp.get("ok"):
            status = 404 if "not found" in str(resp.get("error") or "").lower() else 503
            raise HTTPException(status_code=status, detail=resp.get("error") or "failed")
        return resp

    @app.get("/api/events")
    async def events(limit: int = 100) -> dict[str, Any]:
        resp = await api.get_events(limit=limit)
        if not resp.get("ok"):
            raise HTTPException(status_code=503, detail=resp.get("error") or "runtime unavailable")
        return resp

    @app.post("/api/message")
    async def message(body: MessageBody) -> dict[str, Any]:
        acting = body.acting_as.strip().lower()
        if acting not in {"board", "client"}:
            raise HTTPException(status_code=400, detail="acting_as must be board or client")
        if not body.content.strip():
            raise HTTPException(status_code=400, detail="content is required")
        resp = await api.send_message(
            acting_as=acting,
            recipient=body.recipient.strip(),
            content=body.content,
            conversation_id=body.conversation_id,
        )
        if not resp.get("ok"):
            raise HTTPException(status_code=502, detail=resp.get("error") or "message failed")
        return resp

    @app.post("/api/admin/restart-session")
    async def restart_session() -> dict[str, Any]:
        resp = await api.restart_session()
        if not resp.get("ok"):
            raise HTTPException(status_code=500, detail=resp.get("error") or "restart failed")
        await hub.broadcast({"type": "admin", "action": "restart_session", "result": resp})
        return resp

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            catchup = await api.get_events(limit=100)
            experiment = None
            if supervisor is not None:
                experiment = supervisor.active_experiment
            await ws.send_json(
                {
                    "type": "hello",
                    "runtime_subscribe": api.connected,
                    "supervised": supervisor is not None,
                    "experiment": experiment,
                    "events": catchup.get("events") if catchup.get("ok") else [],
                }
            )
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await hub.disconnect(ws)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app

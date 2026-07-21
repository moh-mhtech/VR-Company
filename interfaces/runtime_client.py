"""Shared NDJSON TCP client for board/client CLIs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_endpoint() -> tuple[str, int]:
    limits = yaml.safe_load((PROJECT_ROOT / "runtime" / "limits.yaml").read_text(encoding="utf-8")) or {}
    return str(limits.get("tcp_host", "127.0.0.1")), int(limits.get("tcp_port", 8765))


class RuntimeClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        except ConnectionRefusedError as exc:
            raise SystemExit(
                f"Cannot connect to runtime at {self.host}:{self.port}. "
                "Start it with: python -m runtime.main"
            ) from exc

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._reader is None or self._writer is None:
            await self.connect()
        assert self._reader is not None and self._writer is not None
        self._writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            return {"ok": False, "error": "runtime closed the connection"}
        return json.loads(line.decode("utf-8"))

"""Central runtime process — NDJSON TCP server for board/client CLIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.console_encoding import harden_console

harden_console()

from runtime.agent_runtime import CompanyRuntime
from runtime.paths import PROJECT_ROOT as ROOT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("runtime.main")


def _load_limits() -> dict:
    return yaml.safe_load((ROOT / "runtime" / "limits.yaml").read_text(encoding="utf-8")) or {}


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    company: CompanyRuntime,
) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("CLI connected: %s", peer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                request = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                writer.write((json.dumps({"ok": False, "error": f"invalid json: {exc}"}) + "\n").encode())
                await writer.drain()
                continue

            op = request.get("op") or "message"
            try:
                if op == "ping":
                    response = {"ok": True, "pong": True}
                elif op == "list_agents":
                    response = {"ok": True, "agents": company.list_agents()}
                elif op == "message":
                    response = await company.handle_human_message(
                        acting_as=request.get("acting_as") or "board",
                        recipient=request.get("recipient") or "ceo",
                        content=request.get("content") or "",
                        conversation_id=request.get("conversation_id"),
                    )
                else:
                    response = {"ok": False, "error": f"unknown op: {op}"}
            except Exception as exc:  # noqa: BLE001 — surface to CLI
                logger.exception("Request failed")
                response = {"ok": False, "error": str(exc)}

            writer.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
            await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        logger.info("CLI disconnected: %s", peer)


async def run_server(host: str, port: int) -> None:
    load_dotenv(ROOT / ".env")
    from runtime.observability import end_agentops, init_agentops

    init_agentops()
    company = CompanyRuntime()
    company.start()

    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, company),
        host,
        port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    logger.info("VR-Company runtime listening on %s", addrs)
    try:
        async with server:
            await server.serve_forever()
    finally:
        end_agentops("Success")


def main() -> None:
    limits = _load_limits()
    parser = argparse.ArgumentParser(description="VR-Company AutoGen runtime")
    parser.add_argument("--host", default=limits.get("tcp_host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(limits.get("tcp_port", 8765)))
    args = parser.parse_args()
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Runtime stopped")
        from runtime.observability import end_agentops

        end_agentops("Indeterminate")


if __name__ == "__main__":
    main()

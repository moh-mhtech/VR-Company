"""AutoGen worker process — NDJSON TCP server for the company simulation.

Started by ``runtime.main`` (supervisor) or directly:
``python -m runtime.autogen_server``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.agent_runtime import CompanyRuntime
from runtime.paths import PROJECT_ROOT as ROOT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("runtime.autogen_server")


def _load_limits() -> dict:
    return yaml.safe_load((ROOT / "runtime" / "limits.yaml").read_text(encoding="utf-8")) or {}


async def _stream_events(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    company: CompanyRuntime,
) -> None:
    queue = company.events.subscribe()
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            read_task = asyncio.create_task(reader.read(1))
            done, pending = await asyncio.wait(
                {get_task, read_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if read_task in done:
                break
            event = get_task.result()
            writer.write((json.dumps({"event": event}, default=str) + "\n").encode("utf-8"))
            await writer.drain()
    finally:
        company.events.unsubscribe(queue)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    company: CompanyRuntime,
) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("Client connected: %s", peer)
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
                    from runtime.paths import get_experiment_name

                    response = {
                        "ok": True,
                        "pong": True,
                        "experiment": get_experiment_name(),
                    }
                elif op == "list_agents":
                    response = {"ok": True, "agents": company.list_agents()}
                elif op == "message":
                    response = await company.handle_human_message(
                        acting_as=request.get("acting_as") or "board",
                        recipient=request.get("recipient") or "ceo",
                        content=request.get("content") or "",
                        conversation_id=request.get("conversation_id"),
                    )
                elif op == "list_conversations":
                    response = {"ok": True, "conversations": company.router.list_conversations()}
                elif op == "get_history":
                    cid = request.get("conversation_id") or ""
                    limit = int(request.get("limit") or 200)
                    try:
                        messages = company.router.history_observer(cid, limit=limit)
                        response = {"ok": True, "conversation_id": cid, "messages": messages}
                    except FileNotFoundError:
                        response = {"ok": False, "error": f"conversation not found: {cid}"}
                elif op == "get_events":
                    limit = int(request.get("limit") or 100)
                    response = {"ok": True, "events": company.events.recent(limit=limit)}
                elif op == "restart_session":
                    response = company.restart_session()
                elif op == "subscribe":
                    writer.write((json.dumps({"ok": True, "subscribed": True}) + "\n").encode("utf-8"))
                    await writer.drain()
                    await _stream_events(reader, writer, company)
                    break
                else:
                    response = {"ok": False, "error": f"unknown op: {op}"}
            except Exception as exc:  # noqa: BLE001
                logger.exception("Request failed")
                response = {"ok": False, "error": str(exc)}

            if op != "subscribe":
                writer.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
                await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        logger.info("Client disconnected: %s", peer)


async def run_autogen_server(host: str, port: int, *, experiment: str) -> None:
    load_dotenv(ROOT / ".env")
    from runtime.observability import end_agentops, init_agentops
    from runtime.paths import EXPERIMENTS_DIR, set_experiment

    set_experiment(experiment, root=EXPERIMENTS_DIR / experiment)
    init_agentops()
    company = CompanyRuntime()
    company.start()

    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, company),
        host,
        port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    logger.info("AutoGen worker listening on %s (experiment=%s)", addrs, experiment)
    try:
        async with server:
            await server.serve_forever()
    finally:
        end_agentops("Success")


def main() -> None:
    from runtime.console_encoding import harden_console

    harden_console()
    limits = _load_limits()
    parser = argparse.ArgumentParser(description="VR-Company AutoGen worker")
    parser.add_argument("--host", default=limits.get("tcp_host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(limits.get("tcp_port", 8765)))
    parser.add_argument(
        "--experiment",
        default=None,
        help="Experiment name under experiments/ (or set VR_EXPERIMENT)",
    )
    args = parser.parse_args()
    experiment = args.experiment
    if not experiment:
        from runtime.paths import activate_from_env

        try:
            experiment = activate_from_env()
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
    if not experiment:
        raise SystemExit(
            "No experiment specified. Pass --experiment <name> or set VR_EXPERIMENT / experiments/.active"
        )
    try:
        asyncio.run(run_autogen_server(args.host, args.port, experiment=experiment))
    except KeyboardInterrupt:
        logger.info("AutoGen worker stopped")
        from runtime.observability import end_agentops

        end_agentops("Indeterminate")


if __name__ == "__main__":
    main()

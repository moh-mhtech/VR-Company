"""Supervisor entrypoint — launches AutoGen worker + optional web console.

Architecture:
  python -m runtime.main
    ├── subprocess: runtime.autogen_server  (simulation / TCP)
    └── optional: web FastAPI process-in-loop (talks to Autogen over TCP)

Experiment lifecycle (create / start / switch / delete / …) is owned by the
supervisor so Autogen is stopped before filesystem changes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.experiment_manager import (
    ExperimentError,
    create_experiment,
    delete_experiment,
    duplicate_experiment,
    ensure_experiments_dir,
    export_experiment,
    get_active_experiment,
    list_experiments,
    rename_experiment,
    set_active_experiment,
)
from runtime.paths import PROJECT_ROOT as ROOT, set_experiment
from runtime.process_manager import AutogenProcessManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("runtime.main")


def _load_limits() -> dict:
    return yaml.safe_load((ROOT / "runtime" / "limits.yaml").read_text(encoding="utf-8")) or {}


class Supervisor:
    """Owns Autogen lifecycle and experiment admin operations used by the web console."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.autogen = AutogenProcessManager(host, port)
        self._admin_lock = asyncio.Lock()
        # True while Autogen is intentionally stopped (switch / stop / delete).
        self.maintenance = False
        self.active_experiment: str | None = get_active_experiment()

    async def start(self, experiment: str | None = None) -> None:
        """Start Autogen for ``experiment`` (or the active pointer). No-op if none."""
        name = experiment or self.active_experiment or get_active_experiment()
        if not name:
            logger.info("No active experiment; supervisor idle until one is started")
            self.active_experiment = None
            set_experiment(None)
            return
        await self.start_experiment(name)

    async def stop(self) -> None:
        await self.autogen.stop()

    async def restart_session(self) -> dict[str, Any]:
        """Ask the live Autogen worker to drop assistants / refresh observability."""
        async with self._admin_lock:
            if not self.autogen.running:
                return {"ok": False, "error": "No experiment is running"}
            from interfaces.runtime_client import RuntimeClient

            client = RuntimeClient(self.host, self.port)
            try:
                await client.connect()
                return await client.request({"op": "restart_session"})
            finally:
                await client.close()

    async def start_experiment(self, name: str) -> dict[str, Any]:
        """Stop any running worker, activate ``name``, start Autogen."""
        async with self._admin_lock:
            return await self._start_experiment_unlocked(name)

    async def _start_experiment_unlocked(self, name: str) -> dict[str, Any]:
        from runtime.experiment_manager import validate_experiment_name
        from runtime.paths import EXPERIMENTS_DIR

        name = validate_experiment_name(name)
        root = EXPERIMENTS_DIR / name
        if not root.is_dir():
            return {"ok": False, "error": f"Experiment not found: {name}"}

        self.maintenance = True
        try:
            if self.autogen.running:
                logger.info("Switching experiment: stopping AutoGen worker")
                await self.autogen.stop()
            set_active_experiment(name)
            set_experiment(name, root=root)
            self.active_experiment = name
            await self.autogen.start(experiment=name)
            logger.info("Experiment started: %s", name)
            return {"ok": True, "active": name, "agents": await self._list_agents_safe()}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to start experiment %s", name)
            self.active_experiment = get_active_experiment()
            return {"ok": False, "error": str(exc)}
        finally:
            self.maintenance = False

    async def stop_experiment(self) -> dict[str, Any]:
        """Stop Autogen and clear the active pointer (no experiment running)."""
        async with self._admin_lock:
            self.maintenance = True
            try:
                await self.autogen.stop()
                set_active_experiment(None)
                set_experiment(None)
                self.active_experiment = None
                return {"ok": True, "active": None}
            finally:
                self.maintenance = False

    async def switch_experiment(self, name: str) -> dict[str, Any]:
        return await self.start_experiment(name)

    async def create_experiment(
        self,
        name: str,
        *,
        notes: str = "",
        start: bool = False,
    ) -> dict[str, Any]:
        async with self._admin_lock:
            try:
                result = await asyncio.to_thread(create_experiment, name, notes=notes)
            except ExperimentError as exc:
                return {"ok": False, "error": str(exc)}
            if start:
                started = await self._start_experiment_unlocked(name)
                result["started"] = started
                if not started.get("ok"):
                    result["ok"] = False
                    result["error"] = started.get("error")
            return result

    async def delete_experiment(self, name: str) -> dict[str, Any]:
        async with self._admin_lock:
            active = self.active_experiment or get_active_experiment()
            if active == name:
                self.maintenance = True
                try:
                    await self.autogen.stop()
                    set_active_experiment(None)
                    set_experiment(None)
                    self.active_experiment = None
                finally:
                    self.maintenance = False
            try:
                return await asyncio.to_thread(delete_experiment, name, allow_active=True)
            except ExperimentError as exc:
                return {"ok": False, "error": str(exc)}

    async def rename_experiment(self, old_name: str, new_name: str) -> dict[str, Any]:
        async with self._admin_lock:
            was_active = (self.active_experiment or get_active_experiment()) == old_name
            running = was_active and self.autogen.running
            self.maintenance = True
            try:
                if running:
                    await self.autogen.stop()
                try:
                    result = await asyncio.to_thread(rename_experiment, old_name, new_name)
                except ExperimentError as exc:
                    if running:
                        await self._start_experiment_unlocked(old_name)
                    return {"ok": False, "error": str(exc)}
                if was_active:
                    self.active_experiment = new_name
                    if running:
                        started = await self._start_experiment_unlocked(new_name)
                        result["restarted"] = started
                return result
            finally:
                self.maintenance = False

    async def duplicate_experiment(
        self,
        source: str,
        new_name: str,
        *,
        notes: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                duplicate_experiment, source, new_name, notes=notes
            )
        except ExperimentError as exc:
            return {"ok": False, "error": str(exc)}

    async def export_experiment(self, name: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(export_experiment, name)
        except ExperimentError as exc:
            return {"ok": False, "error": str(exc)}

    def list_experiments(self) -> dict[str, Any]:
        items = list_experiments()
        return {
            "ok": True,
            "experiments": items,
            "active": self.active_experiment or get_active_experiment(),
            "running": self.autogen.running,
        }

    async def _list_agents_safe(self) -> list[dict[str, Any]]:
        if not self.autogen.running:
            return []
        from interfaces.runtime_client import RuntimeClient

        client = RuntimeClient(self.host, self.port)
        try:
            await client.connect()
            resp = await client.request({"op": "list_agents"})
            return list(resp.get("agents") or [])
        except Exception:  # noqa: BLE001
            return []
        finally:
            await client.close()


async def _run_web(app: object, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()


async def run_supervisor(
    host: str,
    port: int,
    *,
    enable_web: bool = True,
    web_host: str = "127.0.0.1",
    web_port: int = 8787,
    experiment: str | None = None,
) -> None:
    load_dotenv(ROOT / ".env")
    ensure_experiments_dir()
    supervisor = Supervisor(host, port)
    await supervisor.start(experiment)

    web_task: asyncio.Task[None] | None = None
    if enable_web:
        try:
            from web.app import create_app
        except ImportError as exc:
            await supervisor.stop()
            raise SystemExit(
                "Web UI enabled but dependencies are missing. "
                'Install with: pip install -e ".[web]"   '
                "Or run without the UI: python -m runtime.main --no-web"
            ) from exc
        app = create_app(
            runtime_host=host,
            runtime_port=port,
            supervisor=supervisor,
        )
        web_task = asyncio.create_task(_run_web(app, web_host, web_port), name="web-console")
        logger.info("Web console at http://%s:%s  (disable with --no-web)", web_host, web_port)

    try:
        if web_task is not None:
            while True:
                await asyncio.sleep(1.0)
                if web_task.done():
                    exc = web_task.exception()
                    if exc:
                        raise exc
                    break
                if not supervisor.autogen.running and not supervisor.maintenance:
                    if supervisor.active_experiment:
                        logger.error("AutoGen worker exited unexpectedly")
                        break
        else:
            logger.info("Supervisor running without web; Ctrl+C to stop")
            while True:
                await asyncio.sleep(1.0)
                if not supervisor.autogen.running and not supervisor.maintenance:
                    if supervisor.active_experiment:
                        logger.error("AutoGen worker exited unexpectedly")
                        break
                    # Idle with no experiment — keep process alive for CLI-only is N/A;
                    # without web, exit when nothing is running.
                    if not supervisor.active_experiment:
                        logger.info("No experiment running; exiting")
                        break
    finally:
        if web_task is not None and not web_task.done():
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        await supervisor.stop()


def _cmd_experiment(args: argparse.Namespace) -> int:
    """Synchronous CLI helpers that mutate the filesystem (no live supervisor)."""
    ensure_experiments_dir()
    action = args.experiment_command
    try:
        if action == "list":
            for item in list_experiments():
                marker = "*" if item["active"] else " "
                notes = f" — {item['notes']}" if item.get("notes") else ""
                print(f"{marker} {item['name']}{notes}")
            active = get_active_experiment()
            print(f"Active: {active or '(none)'}")
            return 0
        if action == "create":
            result = create_experiment(args.name, notes=args.notes or "")
            print(f"Created experiment: {result['name']}")
            if args.start:
                set_active_experiment(args.name)
                print(f"Marked active: {args.name} (start with: python -m runtime.main)")
            return 0
        if action == "delete":
            result = delete_experiment(args.name, allow_active=bool(args.force))
            print(f"Deleted experiment: {result['name']}")
            return 0
        if action == "start":
            set_active_experiment(args.name)
            print(f"Active experiment set to: {args.name}")
            print("Starting supervisor…")
            limits = _load_limits()
            asyncio.run(
                run_supervisor(
                    args.host or limits.get("tcp_host", "127.0.0.1"),
                    int(args.port or limits.get("tcp_port", 8765)),
                    enable_web=not args.no_web,
                    web_host=str(args.web_host or limits.get("web_host", "127.0.0.1")),
                    web_port=int(args.web_port or limits.get("web_port", 8787)),
                    experiment=args.name,
                )
            )
            return 0
        if action == "stop":
            set_active_experiment(None)
            print("Cleared active experiment pointer. Stop any running supervisor with Ctrl+C.")
            return 0
        if action == "rename":
            result = rename_experiment(args.name, args.new_name)
            print(f"Renamed: {result['old_name']} -> {result['name']}")
            return 0
        if action == "duplicate":
            result = duplicate_experiment(args.name, args.new_name, notes=args.notes)
            print(f"Duplicated: {result['source']} -> {result['name']}")
            return 0
        if action == "export":
            dest = Path(args.output) if args.output else None
            result = export_experiment(args.name, dest_zip=dest)
            print(f"Exported to: {result['path']}")
            return 0
    except ExperimentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown experiment command: {action}", file=sys.stderr)
    return 1


def main() -> None:
    from runtime.console_encoding import harden_console

    harden_console()
    limits = _load_limits()
    parser = argparse.ArgumentParser(
        description="VR-Company supervisor (AutoGen worker + optional web console)"
    )
    parser.add_argument("--host", default=limits.get("tcp_host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(limits.get("tcp_port", 8765)))
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Do not start the web console (AutoGen worker only)",
    )
    parser.add_argument("--web-host", default=str(limits.get("web_host", "127.0.0.1")))
    parser.add_argument("--web-port", type=int, default=int(limits.get("web_port", 8787)))
    parser.add_argument(
        "--experiment",
        default=None,
        help="Experiment to start (default: experiments/.active if set)",
    )

    sub = parser.add_subparsers(dest="command")
    exp = sub.add_parser("experiment", help="Manage experiments")
    exp_sub = exp.add_subparsers(dest="experiment_command", required=True)

    p_list = exp_sub.add_parser("list", help="List experiments")
    p_list.set_defaults(func=_cmd_experiment)

    p_create = exp_sub.add_parser("create", help="Create experiment from seed")
    p_create.add_argument("name")
    p_create.add_argument("--notes", default="")
    p_create.add_argument(
        "--start",
        action="store_true",
        help="Mark as active (use with runtime.main to run, or experiment start)",
    )
    p_create.set_defaults(func=_cmd_experiment)

    p_delete = exp_sub.add_parser("delete", help="Delete an experiment folder")
    p_delete.add_argument("name")
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="Allow deleting the active experiment pointer target",
    )
    p_delete.set_defaults(func=_cmd_experiment)

    p_start = exp_sub.add_parser("start", help="Set active and run supervisor")
    p_start.add_argument("name")
    p_start.add_argument("--host", default=limits.get("tcp_host", "127.0.0.1"))
    p_start.add_argument("--port", type=int, default=int(limits.get("tcp_port", 8765)))
    p_start.add_argument("--no-web", action="store_true")
    p_start.add_argument("--web-host", default=str(limits.get("web_host", "127.0.0.1")))
    p_start.add_argument("--web-port", type=int, default=int(limits.get("web_port", 8787)))
    p_start.set_defaults(func=_cmd_experiment)

    p_stop = exp_sub.add_parser("stop", help="Clear active experiment pointer")
    p_stop.set_defaults(func=_cmd_experiment)

    p_rename = exp_sub.add_parser("rename", help="Rename an experiment")
    p_rename.add_argument("name")
    p_rename.add_argument("new_name")
    p_rename.set_defaults(func=_cmd_experiment)

    p_dup = exp_sub.add_parser("duplicate", help="Copy an experiment folder")
    p_dup.add_argument("name")
    p_dup.add_argument("new_name")
    p_dup.add_argument("--notes", default=None)
    p_dup.set_defaults(func=_cmd_experiment)

    p_export = exp_sub.add_parser("export", help="Zip an experiment for archive")
    p_export.add_argument("name")
    p_export.add_argument("-o", "--output", default=None, help="Output zip path")
    p_export.set_defaults(func=_cmd_experiment)

    args = parser.parse_args()
    if getattr(args, "command", None) == "experiment":
        raise SystemExit(args.func(args))

    try:
        asyncio.run(
            run_supervisor(
                args.host,
                args.port,
                enable_web=not args.no_web,
                web_host=args.web_host,
                web_port=args.web_port,
                experiment=args.experiment,
            )
        )
    except KeyboardInterrupt:
        logger.info("Supervisor stopped")


if __name__ == "__main__":
    main()

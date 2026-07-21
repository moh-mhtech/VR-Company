"""Manage the AutoGen worker as a subprocess of the supervisor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time

from runtime.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)


class AutogenProcessManager:
    """Start/stop ``python -m runtime.autogen_server`` and wait until TCP is ready."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._proc: subprocess.Popen[bytes] | None = None
        self.experiment: str | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self, experiment: str | None = None) -> None:
        if experiment is not None:
            self.experiment = experiment
        if not self.experiment:
            raise RuntimeError("Cannot start AutoGen worker without an active experiment")
        if self.running:
            return
        cmd = [
            sys.executable,
            "-m",
            "runtime.autogen_server",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--experiment",
            self.experiment,
        ]
        env = os.environ.copy()
        env["VR_EXPERIMENT"] = self.experiment
        logger.info("Starting AutoGen worker: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            # Inherit stdout/stderr so worker logs appear in the supervisor terminal.
        )
        await self.wait_ready(timeout=45.0)
        logger.info(
            "AutoGen worker ready on %s:%s experiment=%s (pid=%s)",
            self.host,
            self.port,
            self.experiment,
            self._proc.pid if self._proc else "?",
        )

    async def stop(self, *, grace_seconds: float = 8.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        logger.info("Stopping AutoGen worker (pid=%s)", proc.pid)
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        else:
            logger.warning("AutoGen worker did not exit; killing")
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error("AutoGen worker kill timed out")
        # Brief pause so Windows releases directory handles.
        await asyncio.sleep(0.35)

    async def restart(self, experiment: str | None = None) -> None:
        await self.stop()
        await self.start(experiment=experiment)

    async def wait_ready(self, timeout: float = 45.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        last_err = "not started"
        while asyncio.get_running_loop().time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"AutoGen worker exited early with code {self._proc.returncode}")
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                writer.write((json.dumps({"op": "ping"}) + "\n").encode("utf-8"))
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                if line:
                    payload = json.loads(line.decode("utf-8"))
                    if payload.get("ok"):
                        return
                    last_err = str(payload.get("error") or payload)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            await asyncio.sleep(0.2)
        raise TimeoutError(f"AutoGen worker not ready on {self.host}:{self.port}: {last_err}")

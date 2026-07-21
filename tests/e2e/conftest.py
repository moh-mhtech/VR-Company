"""Shared fixtures for HTTP + TCP end-to-end tests."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Must run before test modules evaluate OPENAI_API_KEY skip markers.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def free_port() -> int:
    return _free_port()


@pytest.fixture(scope="module")
def e2e_experiment(request: pytest.FixtureRequest) -> Iterator[str]:
    """Ensure a disposable experiment exists for the Autogen worker."""
    from runtime.experiment_manager import (
        create_experiment,
        delete_experiment,
        set_active_experiment,
    )
    from runtime.paths import EXPERIMENTS_DIR

    # Unique per test module so TCP + web suites do not clobber each other.
    suffix = request.module.__name__.rsplit(".", 1)[-1].replace("_", "-")
    name = f"e2e-{suffix}"[:63]
    root = EXPERIMENTS_DIR / name
    if root.exists():
        delete_experiment(name, allow_active=True)
    create_experiment(name, notes="e2e fixture")
    set_active_experiment(name)
    try:
        yield name
    finally:
        set_active_experiment(None)
        try:
            delete_experiment(name, allow_active=True)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="module")
def autogen_worker(free_port: int, e2e_experiment: str) -> Iterator[dict[str, Any]]:
    """Real AutoGen TCP worker subprocess on an ephemeral port (one per module)."""
    from runtime.process_manager import AutogenProcessManager

    mgr = AutogenProcessManager("127.0.0.1", free_port)
    _run(mgr.start(experiment=e2e_experiment))
    try:
        yield {"host": "127.0.0.1", "port": free_port, "manager": mgr, "experiment": e2e_experiment}
    finally:
        _run(mgr.stop())


@pytest.fixture
def supervisor(autogen_worker: dict[str, Any]) -> Any:
    """Supervisor bound to the live Autogen worker."""
    from runtime.main import Supervisor

    sup = Supervisor(autogen_worker["host"], autogen_worker["port"])
    # Worker already started by autogen_worker fixture; reuse its manager.
    sup.autogen = autogen_worker["manager"]
    sup.active_experiment = autogen_worker["experiment"]
    return sup


@pytest.fixture
def web_client(supervisor: Any, autogen_worker: dict[str, Any]) -> Iterator[Any]:
    """HTTP TestClient against the FastAPI app (lifespan runs bridge subscribe)."""
    from fastapi.testclient import TestClient

    from web.app import create_app

    app = create_app(
        runtime_host=autogen_worker["host"],
        runtime_port=autogen_worker["port"],
        supervisor=supervisor,
    )
    with TestClient(app) as client:
        yield client

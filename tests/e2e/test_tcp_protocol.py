"""E2E tests for the AutoGen NDJSON TCP protocol."""

from __future__ import annotations

import asyncio

from interfaces.runtime_client import RuntimeClient, RuntimeEventStream


def _run(coro):  # noqa: ANN001
    return asyncio.run(coro)


def test_tcp_ping(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "ping"})
            assert resp["ok"] is True
            assert resp.get("pong") is True
        finally:
            await client.close()

    _run(_test())


def test_tcp_list_agents(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "list_agents"})
            assert resp["ok"] is True
            ids = {a["agent_id"] for a in resp["agents"]}
            assert "ceo" in ids
        finally:
            await client.close()

    _run(_test())


def test_tcp_list_conversations(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "list_conversations"})
            assert resp["ok"] is True
            assert isinstance(resp["conversations"], list)
        finally:
            await client.close()

    _run(_test())


def test_tcp_get_history_missing(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request(
                {"op": "get_history", "conversation_id": "conv_missing_e2e", "limit": 10}
            )
            assert resp["ok"] is False
            assert "not found" in (resp.get("error") or "").lower()
        finally:
            await client.close()

    _run(_test())


def test_tcp_get_events(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "get_events", "limit": 50})
            assert resp["ok"] is True
            assert isinstance(resp["events"], list)
        finally:
            await client.close()

    _run(_test())


def test_tcp_restart_session(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "restart_session"})
            assert resp["ok"] is True
            assert "agents" in resp
        finally:
            await client.close()

    _run(_test())


def test_tcp_ping_includes_experiment(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "ping"})
            assert resp["ok"] is True
            assert resp.get("experiment") == autogen_worker["experiment"]
        finally:
            await client.close()

    _run(_test())


def test_tcp_unknown_op(autogen_worker: dict) -> None:
    async def _test() -> None:
        client = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
        await client.connect()
        try:
            resp = await client.request({"op": "not_a_real_op"})
            assert resp["ok"] is False
        finally:
            await client.close()

    _run(_test())


def test_tcp_subscribe_receives_events(autogen_worker: dict) -> None:
    async def _scenario() -> dict:
        stream = RuntimeEventStream(autogen_worker["host"], autogen_worker["port"])
        await stream.connect()
        try:
            control = RuntimeClient(autogen_worker["host"], autogen_worker["port"])
            await control.connect()
            try:
                await control.request({"op": "restart_session"})
            finally:
                await control.close()

            async def _read_one() -> dict:
                async for event in stream.events():
                    return event
                raise AssertionError("subscribe closed without events")

            return await asyncio.wait_for(_read_one(), timeout=10.0)
        finally:
            await stream.close()

    got = _run(_scenario())
    assert isinstance(got, dict)
    assert got.get("type")

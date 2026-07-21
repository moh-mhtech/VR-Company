"""Tests for the in-process event bus and observer conversation APIs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from runtime.event_bus import EventBus, preview
from runtime.message_router import MessageRouter


def test_preview_truncates() -> None:
    assert preview("abc") == "abc"
    long = "x" * 500
    out = preview(long, limit=50)
    assert out.endswith("…")
    assert len(out) == 51


def test_event_bus_buffer_and_subscribe() -> None:
    async def _run() -> None:
        bus = EventBus(buffer_size=3)
        q = bus.subscribe()
        bus.emit("a", n=1)
        bus.emit("b", n=2)
        bus.emit("c", n=3)
        bus.emit("d", n=4)
        recent = bus.recent(limit=10)
        assert [e["type"] for e in recent] == ["b", "c", "d"]
        got = []
        got.append(await asyncio.wait_for(q.get(), timeout=1))
        got.append(await asyncio.wait_for(q.get(), timeout=1))
        got.append(await asyncio.wait_for(q.get(), timeout=1))
        got.append(await asyncio.wait_for(q.get(), timeout=1))
        assert [e["type"] for e in got] == ["a", "b", "c", "d"]
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0

    asyncio.run(_run())


def test_event_bus_noop_without_subscribers() -> None:
    bus = EventBus(buffer_size=10)
    event = bus.emit("runtime.ready", agents=[])
    assert event["type"] == "runtime.ready"
    assert bus.recent(1)[0]["event_id"] == event["event_id"]


def test_message_router_emits_and_observer_apis(tmp_path: Path) -> None:
    bus = EventBus(buffer_size=20)
    router = MessageRouter(root=tmp_path / "conversations", event_bus=bus)
    first = router.send(sender="human:client", recipient="sales", content="hello")
    cid = first["conversation_id"]
    router.send(sender="sales", recipient="human:client", content="hi back", conversation_id=cid)

    conversations = router.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["conversation_id"] == cid
    assert conversations[0]["message_count"] == 2

    history = router.history_observer(cid)
    assert len(history) == 2
    assert history[0]["content"] == "hello"

    types = [e["type"] for e in bus.recent()]
    assert types.count("message.sent") == 2


def test_company_runtime_starts_with_idle_bus(tmp_path: Path) -> None:
    import shutil

    from runtime.agent_runtime import CompanyRuntime
    from runtime.paths import SEED_DIR, set_experiment

    dest = tmp_path / "exp"
    shutil.copytree(SEED_DIR, dest)
    set_experiment("exp", root=dest)
    try:
        company = CompanyRuntime()
        company.start()
        assert company.events.subscriber_count == 0
        recent = company.events.recent(limit=50)
        assert any(e["type"] == "runtime.ready" for e in recent)
        assert any(e["type"] == "agent.started" for e in recent)
    finally:
        set_experiment(None)

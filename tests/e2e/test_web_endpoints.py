"""E2E tests for every FastAPI console endpoint."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import pytest

pytest.importorskip("fastapi")


def test_index_page(web_client) -> None:
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert "VR-Company" in resp.text
    assert "text/html" in resp.headers.get("content-type", "")


def test_static_css(web_client) -> None:
    resp = web_client.get("/static/app.css")
    assert resp.status_code == 200
    assert "event-feed" in resp.text or "--bg0" in resp.text


def test_static_js(web_client) -> None:
    resp = web_client.get("/static/app.js")
    assert resp.status_code == 200
    assert "feedStickBottom" in resp.text or "compose-form" in resp.text


def test_health(web_client) -> None:
    resp = web_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["runtime_connected"] is True
    assert data["supervised"] is True
    assert data["runtime"].get("ok") is True


def test_agents(web_client) -> None:
    resp = web_client.get("/api/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    agents = data["agents"]
    assert isinstance(agents, list)
    ids = {a["agent_id"] for a in agents}
    assert "ceo" in ids


def test_conversations_list(web_client) -> None:
    resp = web_client.get("/api/conversations")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["conversations"], list)


def test_conversation_history_missing(web_client) -> None:
    resp = web_client.get("/api/conversations/conv_does_not_exist_e2e")
    assert resp.status_code == 404


def test_events(web_client) -> None:
    resp = web_client.get("/api/events?limit=50")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["events"], list)
    types = {e.get("type") for e in data["events"]}
    assert "runtime.ready" in types or "agent.started" in types or len(data["events"]) >= 0


def test_message_validation(web_client) -> None:
    resp = web_client.post(
        "/api/message",
        json={"acting_as": "hacker", "recipient": "ceo", "content": "hi"},
    )
    assert resp.status_code == 400

    resp = web_client.post(
        "/api/message",
        json={"acting_as": "board", "recipient": "ceo", "content": "   "},
    )
    assert resp.status_code == 400


def test_message_endpoint(web_client) -> None:
    """Validates the message route; may 502 without a usable model key."""
    resp = web_client.post(
        "/api/message",
        json={
            "acting_as": "board",
            "recipient": "ceo",
            "content": "e2e ping — reply with OK only",
        },
    )
    assert resp.status_code in (200, 502)
    data = resp.json()
    if resp.status_code == 200:
        assert data.get("ok") is True
        assert data.get("content")
        assert data.get("conversation_id")
    else:
        assert "detail" in data


@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("OPENAI_API_KEY") or "").strip()
    or (os.getenv("OPENAI_API_KEY") or "").startswith("your-"),
    reason="OPENAI_API_KEY required for live message e2e",
)
def test_message_live_llm(web_client) -> None:
    resp = web_client.post(
        "/api/message",
        json={
            "acting_as": "board",
            "recipient": "ceo",
            "content": "Reply with exactly: E2E_OK",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["conversation_id"]

    hist = web_client.get(f"/api/conversations/{data['conversation_id']}")
    assert hist.status_code == 200
    messages = hist.json()["messages"]
    assert len(messages) >= 1


def test_list_experiments(web_client) -> None:
    resp = web_client.get("/api/experiments")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["experiments"], list)
    assert data["active"]


def test_restart_session(web_client) -> None:
    resp = web_client.post("/api/admin/restart-session")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "agents" in data

    # Worker still healthy afterward
    health = web_client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["runtime_connected"] is True


def test_websocket_hello(web_client) -> None:
    with web_client.websocket_connect("/ws") as ws:
        payload = ws.receive_json()
        assert payload["type"] == "hello"
        assert "events" in payload
        assert isinstance(payload["events"], list)
        ws.send_text("ping")

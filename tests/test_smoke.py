"""Smoke tests that do not require a live model API key."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.message_router import MessageRouter
from runtime.paths import SEED_DIR, set_experiment
from runtime.permission_reconciler import Access, PermissionReconciler
from runtime.plugin_loader import PluginLoader
from runtime.workspace_manager import WorkspaceManager


@pytest.fixture
def active_seed_experiment(tmp_path: Path):
    """Copy seed into a temp experiment and activate it for path-dependent tests."""
    dest = tmp_path / "exp"
    shutil.copytree(SEED_DIR, dest)
    set_experiment("exp", root=dest)
    yield dest
    set_experiment(None)


def test_message_router_merges_participants(tmp_path: Path) -> None:
    router = MessageRouter(root=tmp_path / "conversations")
    first = router.send(sender="human:client", recipient="sales", content="hi")
    cid = first["conversation_id"]
    assert router.participants_of(cid) == ["human:client", "sales"]
    router.send(sender="sales", recipient="ceo", content="please assign", conversation_id=cid)
    parts = router.participants_of(cid)
    assert "ceo" in parts
    assert "sales" in parts
    assert "human:client" in parts


def test_message_router_private_delivery(tmp_path: Path) -> None:
    router = MessageRouter(root=tmp_path / "conversations")
    record = router.send(sender="human:board", recipient="ceo", content="Hello CEO")
    cid = record["conversation_id"]
    assert "ceo" in router.participants_of(cid)
    history = router.history_for(cid, "ceo")
    assert len(history) == 1
    assert history[0]["content"] == "Hello CEO"
    with pytest.raises(PermissionError):
        router.history_for(cid, "sales_001")


def test_permissions_ceo_can_write_company(active_seed_experiment: Path) -> None:
    perms = PermissionReconciler.load()
    assert perms.access_for("ceo", "/workspace/company/organization.md") is Access.READ_WRITE
    host = perms.assert_access("ceo", "/workspace/company/organization.md", need_write=True)
    assert host.name == "organization.md"


def test_permissions_block_escape(active_seed_experiment: Path) -> None:
    perms = PermissionReconciler.load()
    # Path traversal out of experiment workspace must be denied.
    with pytest.raises(PermissionError):
        perms.assert_access("ceo", "/workspace/company/../../../.env", need_write=False)


def test_workspace_read_write_roundtrip(active_seed_experiment: Path) -> None:
    perms = PermissionReconciler.load()
    ws = WorkspaceManager(perms, {"max_file_read_chars": 1000})
    path = "/workspace/shared/knowledge/test-note.md"
    msg = ws.write_file("ceo", path, "# note\n")
    assert "Wrote" in msg
    assert ws.read_file("ceo", path).startswith("# note")
    listed = ws.list_dir("ceo", "/workspace/shared/knowledge")
    assert "test-note.md" in listed


def test_accounting_plugin_loads(active_seed_experiment: Path) -> None:
    loader = PluginLoader()
    mod = loader.load()
    assert hasattr(mod, "prepare_call")
    assert hasattr(mod, "process_usage")
    out = loader.process_usage(
        {"agent_id": "ceo", "metadata": {"project_id": "p1"}},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    assert out["project_id"] == "p1"
    assert out["total_tokens"] == 3


def test_raw_usage_append(tmp_path: Path, active_seed_experiment: Path) -> None:
    from runtime.model_gateway import CallContext, ModelGateway

    gw = ModelGateway()
    gw.raw_log = tmp_path / "raw-usage.jsonl"
    gw.view_dir = tmp_path / "view"
    gw.view_dir.mkdir()
    gw.raw_log.parent.mkdir(parents=True, exist_ok=True)
    event = gw.record_usage(
        CallContext(agent_id="ceo", conversation_id="conv_x"),
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert event["total_tokens"] == 15
    lines = gw.raw_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["agent_id"] == "ceo"

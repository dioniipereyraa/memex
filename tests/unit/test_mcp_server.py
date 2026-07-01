"""Smoke tests for the shared MCP server.

Verify that `build_server()` registers the 4 tools with their descriptions,
and that `call_tool` runs the whole chain (wrapper -> pure tool -> JSON
serialization). We do not test the JSON-RPC protocol; FastMCP covers that
internally. The stdio entrypoint is covered by checking that its
module-level `server` exposes the same tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Conversation, Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.transports import mcp_server, stdio
from memex.transports.mcp_server import build_server

TOOL_NAMES = ("search_chats", "get_chat", "list_recent_chats", "find_related")


@pytest.fixture
def server():
    return build_server()


@pytest.fixture
def mcp_server_with_temp_db(tmp_path):
    """Set up the server with a temporary DB and a FakeEmbedder.

    Uses the module-level singletons `_conn` and `_embedder` in
    `mcp_server`. We reset them on teardown to avoid polluting other tests.
    """
    db_path = tmp_path / "mcp_test.db"
    test_conn = connect_and_init(db_path)
    test_embedder = FakeEmbedder(dim=768)

    # Insert something so queries return results.
    conv = Conversation(
        uuid="smoke-conv",
        title="Smoke test",
        source=Source.CONVERSATIONS,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    repo.insert_conversation(test_conn, conv)

    # Replace the module's singletons.
    original_conn = mcp_server._conn
    original_embedder = mcp_server._embedder
    mcp_server._conn = test_conn
    mcp_server._embedder = test_embedder
    yield
    mcp_server._conn = original_conn
    mcp_server._embedder = original_embedder
    test_conn.close()


class TestServerStructure:
    @pytest.mark.asyncio
    async def test_four_tools_registered(self, server) -> None:
        for name in TOOL_NAMES:
            tool = await server.get_tool(name)
            assert tool is not None, f"tool {name} is not registered"
            assert tool.description, f"tool {name} has no description"

    def test_server_name(self, server) -> None:
        assert server.name == "memex"

    @pytest.mark.asyncio
    async def test_stdio_entrypoint_serves_the_same_tools(self) -> None:
        """The `memex-mcp` entrypoint (stdio.server) must expose the 4 tools."""
        for name in TOOL_NAMES:
            tool = await stdio.server.get_tool(name)
            assert tool is not None, f"tool {name} is not registered in stdio"


class TestCallToolFlow:
    @pytest.mark.asyncio
    async def test_list_recent_chats_returns_json(self, server, mcp_server_with_temp_db) -> None:
        result = await server.call_tool("list_recent_chats", {"limit": 5})
        # FastMCP returns ToolResult with content[0].text
        assert result.content
        payload = json.loads(result.content[0].text)
        assert payload["count"] == 1
        assert payload["chats"][0]["uuid"] == "smoke-conv"

    @pytest.mark.asyncio
    async def test_get_chat_unknown_uuid_returns_error_in_json(
        self, server, mcp_server_with_temp_db
    ) -> None:
        result = await server.call_tool("get_chat", {"uuid": "no-existe"})
        payload = json.loads(result.content[0].text)
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_search_chats_empty_query_returns_error(
        self, server, mcp_server_with_temp_db
    ) -> None:
        # Does not touch the embedder, no Ollama needed.
        result = await server.call_tool("search_chats", {"query": "  "})
        payload = json.loads(result.content[0].text)
        assert "error" in payload


class TestSerializeSanitizes:
    def test_strips_bidi_and_zero_width(self) -> None:
        # Disguise chars in stored chat text must not reach the agent.
        result = {"title": "hello‮evil‬", "snippet": "ok​﻿"}
        out = mcp_server._serialize(result)
        for ch in ("‮", "‬", "​", "﻿"):
            assert ch not in out

    def test_keeps_normal_text(self) -> None:
        out = mcp_server._serialize({"title": "arreglá el login", "n": 5})
        assert "arreglá el login" in out


class TestExceptionHandling:
    @pytest.mark.asyncio
    async def test_unexpected_exception_wrapped_in_error_dict(
        self, server, mcp_server_with_temp_db
    ) -> None:
        """If the pure tool raises something unexpected, the wrapper returns a
        generic JSON error (without leaking the raw exception message to the
        client)."""
        with patch.object(mcp_server.tools, "list_recent_chats") as mock:
            mock.side_effect = RuntimeError("unexpected boom with secret: /home/user/.env")
            result = await server.call_tool("list_recent_chats", {"limit": 5})
            payload = json.loads(result.content[0].text)
            assert "error" in payload
            # Message to the client mentions exception type but not its content.
            assert "RuntimeError" in payload["error"]
            # Crucially, it does NOT leak the raw message (which could carry paths/secrets).
            assert "secret" not in payload["error"]
            assert ".env" not in payload["error"]


# --------------------------------------------------------------------------
# Write / maintenance tools (local stdio only): index_terminal_sessions, sync_now
# --------------------------------------------------------------------------

LOCAL_ONLY_TOOLS = ("index_terminal_sessions", "sync_now")


async def _has_tool(srv, name: str) -> bool:
    """True if `name` is registered on `srv` (get_tool may return None or raise)."""
    try:
        return await srv.get_tool(name) is not None
    except Exception:
        return False


class TestWriteTools:
    @pytest.mark.asyncio
    async def test_local_server_registers_write_tools(self, server) -> None:
        for name in LOCAL_ONLY_TOOLS:
            tool = await server.get_tool(name)
            assert tool is not None and tool.description

    @pytest.mark.asyncio
    async def test_stdio_entrypoint_has_write_tools(self) -> None:
        for name in LOCAL_ONLY_TOOLS:
            assert await _has_tool(stdio.server, name)

    @pytest.mark.asyncio
    async def test_remote_server_excludes_write_tools(self) -> None:
        # SECURITY: the write/sync tools must never be exposed on the remote
        # (authed) claude.ai connector, only the local stdio server.
        from unittest.mock import MagicMock

        from fastmcp.server.auth import AuthProvider

        remote = build_server(auth=MagicMock(spec=AuthProvider))
        for name in TOOL_NAMES:  # read tools still present
            assert await _has_tool(remote, name)
        for name in LOCAL_ONLY_TOOLS:  # write tools absent
            assert not await _has_tool(remote, name)

    def test_sync_now_refuses_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr("memex.sync.state.is_enabled", lambda *a, **k: False)
        payload = json.loads(mcp_server.sync_now())
        assert payload["status"] == "disabled"

    def test_sync_now_no_targets(self, monkeypatch, mcp_server_with_temp_db) -> None:
        monkeypatch.setattr("memex.sync.state.is_enabled", lambda *a, **k: True)
        monkeypatch.setattr("memex.sync.peers.load_peers", lambda *a, **k: [])
        monkeypatch.setattr("memex.sync.file_sync.resolve_sync_dir", lambda: None)
        payload = json.loads(mcp_server.sync_now())
        assert payload["status"] == "no_targets"

    def test_sync_now_reconciles_peer(self, monkeypatch, mcp_server_with_temp_db) -> None:
        from memex.sync import client as sync_client
        from memex.sync.peers import Peer

        monkeypatch.setattr("memex.sync.state.is_enabled", lambda *a, **k: True)
        monkeypatch.setattr(
            "memex.sync.peers.load_peers",
            lambda *a, **k: [Peer(name="mac", url="http://127.0.0.1:5777", token="t")],
        )
        monkeypatch.setattr("memex.sync.file_sync.resolve_sync_dir", lambda: None)
        monkeypatch.setattr(
            "memex.sync.client.reconcile",
            lambda conn, peer, *, local_model, local_dim: sync_client.ReconcileSummary(
                peer=peer.name, pulled=2, pushed=1, failed=0
            ),
        )
        payload = json.loads(mcp_server.sync_now())
        assert payload["status"] == "ok"
        assert payload["peers"][0]["peer"] == "mac"
        assert payload["peers"][0]["pulled"] == 2
        assert payload["peers"][0]["pushed"] == 1

    def test_sync_now_reports_file_sync(self, monkeypatch, mcp_server_with_temp_db) -> None:
        import memex.sync.file_sync as fsmod

        monkeypatch.setattr("memex.sync.state.is_enabled", lambda *a, **k: True)
        monkeypatch.setattr("memex.sync.peers.load_peers", lambda *a, **k: [])
        monkeypatch.setattr("memex.sync.file_sync.resolve_sync_dir", lambda: "/shared")
        monkeypatch.setattr("memex.sync.file_sync.resolve_device_name", lambda: "mac")
        monkeypatch.setattr(
            "memex.sync.file_sync.sync_once",
            lambda *a, **k: fsmod.FileSyncSummary(
                device="mac",
                peers_seen=1,
                pulled=3,
                failed=0,
                would_push=0,
                forks=0,
                incompatible=0,
                exported=True,
            ),
        )
        payload = json.loads(mcp_server.sync_now())
        assert payload["status"] == "ok"
        assert payload["file_sync"]["pulled"] == 3
        assert payload["file_sync"]["folder"] == "/shared"

    def test_index_terminal_sessions_busy_lock(self, monkeypatch) -> None:
        monkeypatch.setattr("memex.ingest_lock.acquire_nonblocking", lambda *a, **k: None)
        payload = json.loads(mcp_server.index_terminal_sessions())
        assert payload["status"] == "busy"

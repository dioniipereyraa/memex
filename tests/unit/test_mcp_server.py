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

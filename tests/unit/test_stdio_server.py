"""Smoke tests for the MCP server.

Verify that the server has the 4 tools registered with their descriptions,
and that `call_tool` runs the whole chain (decorator -> wrapper -> pure
tool -> JSON serialization). We do not test the JSON-RPC protocol; FastMCP
covers that internally.
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
from memex.transports import stdio


@pytest.fixture
def mcp_server_with_temp_db(tmp_path):
    """Set up the server with a temporary DB and a FakeEmbedder.

    Uses the module-level singletons `_conn` and `_embedder`. We reset them
    on teardown to avoid polluting other tests.
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
    original_conn = stdio._conn
    original_embedder = stdio._embedder
    stdio._conn = test_conn
    stdio._embedder = test_embedder
    yield
    stdio._conn = original_conn
    stdio._embedder = original_embedder
    test_conn.close()


class TestServerStructure:
    @pytest.mark.asyncio
    async def test_three_tools_registered(self) -> None:
        names = ("search_chats", "get_chat", "list_recent_chats", "find_related")
        for name in names:
            tool = await stdio.server.get_tool(name)
            assert tool is not None, f"tool {name} is not registered"
            assert tool.description, f"tool {name} has no description"

    def test_server_name(self) -> None:
        assert stdio.server.name == "memex"


class TestCallToolFlow:
    @pytest.mark.asyncio
    async def test_list_recent_chats_returns_json(self, mcp_server_with_temp_db) -> None:
        result = await stdio.server.call_tool("list_recent_chats", {"limit": 5})
        # FastMCP returns ToolResult with content[0].text
        assert result.content
        payload = json.loads(result.content[0].text)
        assert payload["count"] == 1
        assert payload["chats"][0]["uuid"] == "smoke-conv"

    @pytest.mark.asyncio
    async def test_get_chat_unknown_uuid_returns_error_in_json(
        self, mcp_server_with_temp_db
    ) -> None:
        result = await stdio.server.call_tool("get_chat", {"uuid": "no-existe"})
        payload = json.loads(result.content[0].text)
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_search_chats_empty_query_returns_error(self, mcp_server_with_temp_db) -> None:
        # Does not touch the embedder, no Ollama needed.
        result = await stdio.server.call_tool("search_chats", {"query": "  "})
        payload = json.loads(result.content[0].text)
        assert "error" in payload


class TestExceptionHandling:
    @pytest.mark.asyncio
    async def test_unexpected_exception_wrapped_in_error_dict(
        self, mcp_server_with_temp_db
    ) -> None:
        """If the pure tool raises something unexpected, the wrapper returns a
        generic JSON error (without leaking the raw exception message to the
        client)."""
        with patch.object(stdio.tools, "list_recent_chats") as mock:
            mock.side_effect = RuntimeError("unexpected boom with secret: /home/user/.env")
            result = await stdio.server.call_tool("list_recent_chats", {"limit": 5})
            payload = json.loads(result.content[0].text)
            assert "error" in payload
            # Message to the client mentions exception type but not its content.
            assert "RuntimeError" in payload["error"]
            # Crucially, it does NOT leak the raw message (which could carry paths/secrets).
            assert "secret" not in payload["error"]
            assert ".env" not in payload["error"]

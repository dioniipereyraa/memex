"""Smoke tests del MCP server.

Verifican que el server tiene las 3 tools registradas con sus descripciones,
y que `call_tool` ejecuta la cadena entera (decorador → wrapper → tool puro →
serialización JSON). No probamos protocolo JSON-RPC; eso lo testea FastMCP
internamente.
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
    """Setea el server con una DB temporal y un FakeEmbedder.

    Usa los singletons globales `_conn` y `_embedder` del módulo. Reseteamos al
    salir para no contaminar otros tests.
    """
    db_path = tmp_path / "mcp_test.db"
    test_conn = connect_and_init(db_path)
    test_embedder = FakeEmbedder(dim=768)

    # Insertar algo para que las queries devuelvan resultados.
    conv = Conversation(
        uuid="smoke-conv",
        title="Smoke test",
        source=Source.CONVERSATIONS,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    repo.insert_conversation(test_conn, conv)

    # Reemplazar los singletons del módulo.
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
        names = ("search_chats", "get_chat", "list_recent_chats")
        for name in names:
            tool = await stdio.server.get_tool(name)
            assert tool is not None, f"tool {name} no está registrada"
            assert tool.description, f"tool {name} no tiene descripción"

    def test_server_name(self) -> None:
        assert stdio.server.name == "memex"


class TestCallToolFlow:
    @pytest.mark.asyncio
    async def test_list_recent_chats_returns_json(
        self, mcp_server_with_temp_db
    ) -> None:
        result = await stdio.server.call_tool("list_recent_chats", {"limit": 5})
        # FastMCP devuelve ToolResult con content[0].text
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
    async def test_search_chats_empty_query_returns_error(
        self, mcp_server_with_temp_db
    ) -> None:
        # No llega a tocar el embedder, no necesita Ollama.
        result = await stdio.server.call_tool("search_chats", {"query": "  "})
        payload = json.loads(result.content[0].text)
        assert "error" in payload


class TestExceptionHandling:
    @pytest.mark.asyncio
    async def test_unexpected_exception_wrapped_in_error_dict(
        self, mcp_server_with_temp_db
    ) -> None:
        """Si la tool pura lanza algo no esperado, el wrapper devuelve error JSON."""
        with patch.object(stdio.tools, "list_recent_chats") as mock:
            mock.side_effect = RuntimeError("boom inesperado")
            result = await stdio.server.call_tool("list_recent_chats", {"limit": 5})
            payload = json.loads(result.content[0].text)
            assert "error" in payload
            assert "boom inesperado" in payload["error"]

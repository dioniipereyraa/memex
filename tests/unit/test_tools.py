"""Tests de las tools puras de `memex.transports.tools`.

Tests directos sobre las funciones que devuelven dicts. La capa MCP de
`stdio.py` solo serializa estos dicts a JSON, así que testear acá cubre
toda la lógica de retrieval que va a expose el servidor.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage import repo
from memex.transports import tools


@pytest.fixture
def populated_db(
    db: sqlite3.Connection,
    project: Project,
    conversation: Conversation,
    human_message: Message,
    assistant_message: Message,
) -> sqlite3.Connection:
    """DB con un project, una conversación, dos mensajes y un chunk con embedding."""
    repo.insert_project(db, project)
    repo.insert_conversation(db, conversation)
    repo.insert_message(db, human_message)
    repo.insert_message(db, assistant_message)
    chunk = Chunk(
        conversation_uuid=conversation.uuid,
        message_uuid=assistant_message.uuid,
        sender=assistant_message.sender.value,
        text=assistant_message.text,
        char_start=0,
        char_end=len(assistant_message.text),
        created_at=assistant_message.created_at,
    )
    embedder = FakeEmbedder(dim=768)
    repo.add_chunk(db, chunk, embedder.embed_one(assistant_message.text))
    return db


class TestSearchChats:
    def test_basic_search(self, populated_db: sqlite3.Connection) -> None:
        embedder = FakeEmbedder(dim=768)
        result = tools.search_chats(
            populated_db,
            embedder,
            query="sqlite-vec",
            limit=5,
        )
        assert "results" in result
        assert result["query"] == "sqlite-vec"
        assert result["count"] >= 0
        assert isinstance(result["results"], list)

    def test_empty_query_returns_error(
        self, populated_db: sqlite3.Connection
    ) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="   ", limit=5)
        assert "error" in result
        assert "vac" in result["error"].lower()

    def test_invalid_source_returns_error(
        self, populated_db: sqlite3.Connection
    ) -> None:
        result = tools.search_chats(
            populated_db, FakeEmbedder(), query="x", source="inventado"
        )
        assert "error" in result
        assert "inventado" in result["error"]

    def test_limit_clamped_to_max(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(
            populated_db, FakeEmbedder(), query="x", limit=999
        )
        assert "results" in result
        assert len(result["results"]) <= 50

    def test_limit_clamped_to_min(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(
            populated_db, FakeEmbedder(), query="x", limit=0
        )
        assert "results" in result

    def test_source_filter_applied(
        self,
        db: sqlite3.Connection,
        project: Project,
    ) -> None:
        """Si pido source=memory pero solo hay conversations, devuelve 0 resultados."""
        repo.insert_project(db, project)
        conv = Conversation(
            uuid="conv-x",
            title="Suelta",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        embedder = FakeEmbedder(dim=768)
        chunk = Chunk(
            conversation_uuid=conv.uuid,
            text="hola",
            char_start=0,
            char_end=4,
            created_at=conv.updated_at,
        )
        repo.add_chunk(db, chunk, embedder.embed_one("hola"))

        result = tools.search_chats(db, embedder, query="hola", source="memory")
        assert result["count"] == 0

    def test_result_shape_includes_required_fields(
        self, populated_db: sqlite3.Connection
    ) -> None:
        embedder = FakeEmbedder(dim=768)
        result = tools.search_chats(populated_db, embedder, query="anything")
        if result["results"]:
            r0 = result["results"][0]
            for key in (
                "rank",
                "conversation_uuid",
                "title",
                "source",
                "distance",
                "snippet",
                "created_at",
                "updated_at",
            ):
                assert key in r0


class TestGetChat:
    def test_basic_get(
        self, populated_db: sqlite3.Connection, conversation: Conversation
    ) -> None:
        result = tools.get_chat(populated_db, conversation.uuid)
        assert result["uuid"] == conversation.uuid
        assert result["title"] == conversation.title
        assert result["message_count"] == 2
        assert len(result["messages"]) == 2
        assert result["messages"][0]["sender"] == Sender.HUMAN.value
        assert result["messages"][1]["sender"] == Sender.ASSISTANT.value

    def test_get_includes_project_when_linked(
        self,
        populated_db: sqlite3.Connection,
        conversation: Conversation,
        project: Project,
    ) -> None:
        result = tools.get_chat(populated_db, conversation.uuid)
        assert result["project"] is not None
        assert result["project"]["uuid"] == project.uuid
        assert result["project"]["name"] == project.name
        assert result["project"]["prompt_template"] == project.prompt_template

    def test_get_unknown_uuid_returns_error(
        self, populated_db: sqlite3.Connection
    ) -> None:
        result = tools.get_chat(populated_db, "no-existe-este-uuid")
        assert "error" in result
        assert "no-existe-este-uuid" in result["error"]

    def test_get_empty_uuid_returns_error(
        self, populated_db: sqlite3.Connection
    ) -> None:
        result = tools.get_chat(populated_db, "  ")
        assert "error" in result

    def test_messages_are_in_chronological_order(
        self, populated_db: sqlite3.Connection, conversation: Conversation
    ) -> None:
        result = tools.get_chat(populated_db, conversation.uuid)
        timestamps = [m["created_at"] for m in result["messages"]]
        assert timestamps == sorted(timestamps)


class TestListRecentChats:
    def test_empty_db_returns_empty_list(self, db: sqlite3.Connection) -> None:
        result = tools.list_recent_chats(db, limit=10)
        assert result["count"] == 0
        assert result["chats"] == []

    def test_ordering_by_updated_at_desc(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        repo.insert_project(db, project)
        old = Conversation(
            uuid="old",
            title="Vieja",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        new = Conversation(
            uuid="new",
            title="Nueva",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, old)
        repo.insert_conversation(db, new)
        result = tools.list_recent_chats(db, limit=10)
        assert [c["uuid"] for c in result["chats"]] == ["new", "old"]

    def test_limit_clamped(self, db: sqlite3.Connection) -> None:
        result = tools.list_recent_chats(db, limit=9999)
        assert result["count"] <= 100

    def test_invalid_source_returns_error(self, db: sqlite3.Connection) -> None:
        result = tools.list_recent_chats(db, source="basura")
        assert "error" in result

    def test_source_filter(self, db: sqlite3.Connection) -> None:
        memory_conv = Conversation(
            uuid="mem",
            title="Memoria",
            source=Source.MEMORY,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        normal_conv = Conversation(
            uuid="norm",
            title="Normal",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, memory_conv)
        repo.insert_conversation(db, normal_conv)

        only_memory = tools.list_recent_chats(db, source="memory")
        assert only_memory["count"] == 1
        assert only_memory["chats"][0]["uuid"] == "mem"

        only_conv = tools.list_recent_chats(db, source="conversations")
        assert only_conv["count"] == 1
        assert only_conv["chats"][0]["uuid"] == "norm"

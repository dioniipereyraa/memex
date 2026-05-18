"""Tests de los modelos de dominio."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memex.core.models import (
    Chunk,
    Conversation,
    Message,
    Project,
    SearchHit,
    Sender,
    Source,
)


class TestEnums:
    def test_source_values(self) -> None:
        assert Source.CONVERSATIONS.value == "conversations"
        assert Source.DESIGN_CHAT.value == "design_chat"
        assert Source.MEMORY.value == "memory"

    def test_sender_values(self) -> None:
        assert Sender.HUMAN.value == "human"
        assert Sender.ASSISTANT.value == "assistant"
        assert Sender.SYSTEM.value == "system"


class TestProject:
    def test_minimal(self) -> None:
        p = Project(
            uuid="p1",
            name="Test",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert p.uuid == "p1"
        assert p.is_private is True
        assert p.is_starter_project is False
        assert p.description is None

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Project(  # type: ignore[call-arg]
                uuid="p1",
                name="Test",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                hacked_field="oops",
            )


class TestConversation:
    def test_with_project(self) -> None:
        c = Conversation(
            uuid="c1",
            title="chat",
            source=Source.DESIGN_CHAT,
            project_uuid="p1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert c.source is Source.DESIGN_CHAT
        assert c.project_uuid == "p1"
        assert c.summary is None

    def test_memory_source(self) -> None:
        c = Conversation(
            uuid="mem",
            title="Memoria curada",
            source=Source.MEMORY,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert c.project_uuid is None
        assert c.source is Source.MEMORY


class TestMessage:
    def test_human_message(self) -> None:
        m = Message(
            uuid="m1",
            conversation_uuid="c1",
            sender=Sender.HUMAN,
            text="hola",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert m.parent_uuid is None
        assert m.raw_content is None
        assert m.has_tool_use is False

    def test_tool_use_metadata(self) -> None:
        m = Message(
            uuid="m2",
            conversation_uuid="c1",
            parent_uuid="m1",
            sender=Sender.ASSISTANT,
            text="[tool_use: search] query",
            raw_content=[{"type": "tool_use", "name": "search", "input": {"q": "x"}}],
            has_tool_use=True,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert m.has_tool_use is True
        assert m.raw_content is not None
        assert m.raw_content[0]["type"] == "tool_use"


class TestChunk:
    def test_basic(self) -> None:
        ch = Chunk(
            conversation_uuid="c1",
            text="contenido",
            char_start=0,
            char_end=9,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert ch.id is None  # antes del INSERT
        assert ch.message_uuid is None
        assert ch.sender is None

    def test_negative_char_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Chunk(
                conversation_uuid="c1",
                text="x",
                char_start=-1,
                char_end=0,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )


class TestSearchHit:
    def test_compose(self) -> None:
        chunk = Chunk(
            id=1,
            conversation_uuid="c1",
            text="texto",
            char_start=0,
            char_end=5,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        conv = Conversation(
            uuid="c1",
            title="chat",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        hit = SearchHit(chunk=chunk, conversation=conv, distance=0.1, snippet="texto")
        assert hit.distance == 0.1
        assert hit.chunk.id == 1

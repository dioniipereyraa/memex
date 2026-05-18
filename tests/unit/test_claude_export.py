"""Tests de los parsers del export oficial de Claude.ai.

Usa fixtures JSON inline (no toca el export real del usuario).
"""

from __future__ import annotations

from datetime import UTC, datetime

from memex.core.ingest.claude_export import (
    parse_conversations_list,
    parse_design_chat,
    parse_memories,
    parse_project,
)
from memex.core.models import Sender, Source

PROJECT_JSON = {
    "uuid": "proj-1",
    "name": "Memex",
    "description": "Spin-off de SyncChat",
    "prompt_template": "Sos un asistente útil.",
    "is_private": True,
    "is_starter_project": False,
    "creator": {"full_name": "Dionisio", "uuid": "user-1"},
    "created_at": "2026-04-01T12:00:00.000Z",
    "updated_at": "2026-05-01T12:00:00.000Z",
    "docs": [],
}

STARTER_PROJECT_JSON = {
    "uuid": "proj-starter",
    "name": "",
    "description": "",
    "prompt_template": "",
    "is_private": False,
    "is_starter_project": True,
    "creator": {},
    "created_at": "2026-01-01T00:00:00.000Z",
    "updated_at": "2026-01-01T00:00:00.000Z",
    "docs": [],
}

CONVERSATION_JSON = {
    "uuid": "conv-1",
    "name": "Bootstrap del repo",
    "summary": "Charla sobre cómo arrancar Memex.",
    "created_at": "2026-05-18T15:00:00.000Z",
    "updated_at": "2026-05-18T16:00:00.000Z",
    "account": {"uuid": "acct-1"},
    "chat_messages": [
        {
            "uuid": "msg-1",
            "text": "hola",
            "content": [{"type": "text", "text": "hola"}],
            "sender": "human",
            "created_at": "2026-05-18T15:00:00.000Z",
            "updated_at": "2026-05-18T15:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": None,
        },
        {
            "uuid": "msg-2",
            "text": "vamos",
            "content": [{"type": "text", "text": "vamos"}],
            "sender": "assistant",
            "created_at": "2026-05-18T15:00:10.000Z",
            "updated_at": "2026-05-18T15:00:10.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": "msg-1",
        },
    ],
}

DESIGN_CHAT_JSON = {
    "uuid": "design-1",
    "title": "Diseño de schema",
    "project": {"name": "Memex", "uuid": "proj-1"},
    "created_at": "2026-05-18T17:00:00.000Z",
    "updated_at": "2026-05-18T17:30:00.000Z",
    "messages": [
        {
            "uuid": "msg-d-1",
            "text": "qué tablas necesitamos",
            "content": [{"type": "text", "text": "qué tablas necesitamos"}],
            "sender": "human",
            "created_at": "2026-05-18T17:00:00.000Z",
            "updated_at": "2026-05-18T17:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": "",
        }
    ],
}

MEMORIES_JSON = [
    {
        "conversations_memory": "Resumen curado de Claude.ai sobre los últimos chats.",
        "account_uuid": "acct-1",
    }
]


class TestParseProject:
    def test_basic_project(self) -> None:
        p = parse_project(PROJECT_JSON)
        assert p.uuid == "proj-1"
        assert p.name == "Memex"
        assert p.description == "Spin-off de SyncChat"
        assert p.prompt_template == "Sos un asistente útil."
        assert p.is_private is True
        assert p.creator_uuid == "user-1"
        assert p.creator_name == "Dionisio"

    def test_starter_with_empty_fields(self) -> None:
        p = parse_project(STARTER_PROJECT_JSON)
        assert p.uuid == "proj-starter"
        assert p.name == ""
        # `description` y `prompt_template` vienen vacíos en starter projects, los normalizamos a None.
        assert p.description is None
        assert p.prompt_template is None
        assert p.is_starter_project is True
        assert p.creator_uuid is None
        assert p.creator_name is None


class TestParseConversationsList:
    def test_basic(self) -> None:
        results = parse_conversations_list([CONVERSATION_JSON])
        assert len(results) == 1
        conv, messages = results[0]
        assert conv.uuid == "conv-1"
        assert conv.title == "Bootstrap del repo"
        assert conv.summary == "Charla sobre cómo arrancar Memex."
        assert conv.source is Source.CONVERSATIONS
        assert conv.project_uuid is None
        assert conv.account_uuid == "acct-1"
        assert len(messages) == 2

    def test_message_fields(self) -> None:
        _, messages = parse_conversations_list([CONVERSATION_JSON])[0]
        m1, m2 = messages
        assert m1.sender is Sender.HUMAN
        assert m1.text == "hola"
        assert m1.parent_uuid is None
        assert m2.sender is Sender.ASSISTANT
        assert m2.parent_uuid == "msg-1"

    def test_empty_list(self) -> None:
        assert parse_conversations_list([]) == []

    def test_uses_legacy_text_when_content_empty(self) -> None:
        data = {
            **CONVERSATION_JSON,
            "uuid": "conv-fallback",
            "chat_messages": [
                {
                    "uuid": "msg-legacy",
                    "text": "viene solo en el campo legacy",
                    "content": [],
                    "sender": "human",
                    "created_at": "2026-05-18T15:00:00.000Z",
                    "updated_at": "2026-05-18T15:00:00.000Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": None,
                }
            ],
        }
        _, messages = parse_conversations_list([data])[0]
        assert messages[0].text == "viene solo en el campo legacy"

    def test_tool_use_renders_with_markers(self) -> None:
        data = {
            **CONVERSATION_JSON,
            "uuid": "conv-tools",
            "chat_messages": [
                {
                    "uuid": "msg-tool",
                    "text": "",
                    "content": [
                        {"type": "tool_use", "name": "search", "input": {"q": "memex"}},
                    ],
                    "sender": "assistant",
                    "created_at": "2026-05-18T15:00:00.000Z",
                    "updated_at": "2026-05-18T15:00:00.000Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": None,
                }
            ],
        }
        _, messages = parse_conversations_list([data])[0]
        msg = messages[0]
        assert "[tool_use: search]" in msg.text
        assert msg.has_tool_use is True

    def test_attachments_flag(self) -> None:
        data = {
            **CONVERSATION_JSON,
            "uuid": "conv-att",
            "chat_messages": [
                {
                    "uuid": "msg-att",
                    "text": "x",
                    "content": [{"type": "text", "text": "x"}],
                    "sender": "human",
                    "created_at": "2026-05-18T15:00:00.000Z",
                    "updated_at": "2026-05-18T15:00:00.000Z",
                    "attachments": [],
                    "files": [{"name": "foo.pdf"}],
                    "parent_message_uuid": None,
                }
            ],
        }
        _, messages = parse_conversations_list([data])[0]
        assert messages[0].has_attachments is True

    def test_empty_parent_uuid_normalized_to_none(self) -> None:
        """Algunos exports vienen con parent_message_uuid='' en vez de null."""
        _conv, messages = parse_design_chat(DESIGN_CHAT_JSON)
        assert messages[0].parent_uuid is None

    def test_missing_updated_at_falls_back_to_created_at(self) -> None:
        """Algunos mensajes de design_chats no traen updated_at."""
        data = {
            **DESIGN_CHAT_JSON,
            "uuid": "design-no-upd",
            "messages": [
                {
                    "uuid": "msg-no-upd",
                    "text": "x",
                    "content": [{"type": "text", "text": "x"}],
                    "sender": "human",
                    "created_at": "2026-05-18T17:00:00.000Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": None,
                }
            ],
        }
        _conv, messages = parse_design_chat(data)
        assert messages[0].created_at == messages[0].updated_at


class TestParseDesignChat:
    def test_basic(self) -> None:
        conv, messages = parse_design_chat(DESIGN_CHAT_JSON)
        assert conv.uuid == "design-1"
        assert conv.title == "Diseño de schema"
        assert conv.source is Source.DESIGN_CHAT
        assert conv.project_uuid == "proj-1"
        assert len(messages) == 1
        assert messages[0].sender is Sender.HUMAN


class TestParseMemories:
    def test_basic(self) -> None:
        fixed_now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
        result = parse_memories(MEMORIES_JSON, now=fixed_now)
        assert result is not None
        conv, msg = result
        assert conv.source is Source.MEMORY
        assert conv.uuid == "memory-acct-1"
        assert conv.account_uuid == "acct-1"
        assert conv.created_at == fixed_now
        assert msg.uuid == "memory-acct-1-msg"
        assert msg.conversation_uuid == conv.uuid
        assert msg.sender is Sender.ASSISTANT
        assert msg.text == "Resumen curado de Claude.ai sobre los últimos chats."

    def test_dict_input(self) -> None:
        """memories.json a veces puede venir como dict directo, no como lista."""
        data = MEMORIES_JSON[0]
        result = parse_memories(data, now=datetime(2026, 5, 18, tzinfo=UTC))
        assert result is not None

    def test_returns_none_when_empty(self) -> None:
        assert parse_memories([], now=datetime(2026, 5, 18, tzinfo=UTC)) is None
        assert (
            parse_memories(
                [{"conversations_memory": "", "account_uuid": "a"}],
                now=datetime(2026, 5, 18, tzinfo=UTC),
            )
            is None
        )
        assert (
            parse_memories(
                [{"account_uuid": "a"}],
                now=datetime(2026, 5, 18, tzinfo=UTC),
            )
            is None
        )

    def test_stable_uuid(self) -> None:
        """Re-parsear el mismo memories.json debe dar el mismo uuid (idempotente)."""
        r1 = parse_memories(MEMORIES_JSON, now=datetime(2026, 5, 18, tzinfo=UTC))
        r2 = parse_memories(MEMORIES_JSON, now=datetime(2027, 1, 1, tzinfo=UTC))
        assert r1 is not None and r2 is not None
        assert r1[0].uuid == r2[0].uuid
        assert r1[1].uuid == r2[1].uuid

    def test_fallback_uuid_without_account(self) -> None:
        data = [{"conversations_memory": "blob"}]
        result = parse_memories(data, now=datetime(2026, 5, 18, tzinfo=UTC))
        assert result is not None
        conv, _ = result
        assert conv.uuid == "memory-default"

"""Tests del repo SQLite + sqlite-vec."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memex.core.models import Chunk, Conversation, Message, Project, Source
from memex.core.storage import repo
from memex.core.storage.db import init_schema, schema_version


class TestSchema:
    def test_version_registered(self, db: sqlite3.Connection) -> None:
        assert schema_version(db) == "1"

    def test_init_is_idempotent(self, db: sqlite3.Connection) -> None:
        init_schema(db)
        init_schema(db)
        assert schema_version(db) == "1"

    def test_required_tables_exist(self, db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        for expected in ("projects", "conversations", "messages", "chunks", "vec_chunks", "schema_meta"):
            assert expected in names, f"falta tabla {expected} en {sorted(names)}"

    def test_foreign_keys_enabled(self, db: sqlite3.Connection) -> None:
        row = db.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


class TestProjectRepo:
    def test_insert_and_get(self, db: sqlite3.Connection, project: Project) -> None:
        repo.insert_project(db, project)
        loaded = repo.get_project(db, project.uuid)
        assert loaded is not None
        assert loaded.uuid == project.uuid
        assert loaded.name == project.name
        assert loaded.is_private is True

    def test_upsert(self, db: sqlite3.Connection, project: Project) -> None:
        repo.insert_project(db, project)
        updated = project.model_copy(update={"name": "Memex v2"})
        repo.insert_project(db, updated)
        loaded = repo.get_project(db, project.uuid)
        assert loaded is not None
        assert loaded.name == "Memex v2"

    def test_get_missing_returns_none(self, db: sqlite3.Connection) -> None:
        assert repo.get_project(db, "nope") is None


class TestConversationRepo:
    def test_insert_with_project_fk(
        self, db: sqlite3.Connection, project: Project, conversation: Conversation
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        loaded = repo.get_conversation(db, conversation.uuid)
        assert loaded is not None
        assert loaded.project_uuid == project.uuid
        assert loaded.source is Source.DESIGN_CHAT

    def test_insert_without_project(self, db: sqlite3.Connection) -> None:
        conv = Conversation(
            uuid="solo",
            title="Suelta",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        loaded = repo.get_conversation(db, "solo")
        assert loaded is not None
        assert loaded.project_uuid is None

    def test_fk_violation_raises(self, db: sqlite3.Connection) -> None:
        conv = Conversation(
            uuid="orphan",
            title="huerfana",
            source=Source.DESIGN_CHAT,
            project_uuid="proj-inexistente",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_conversation(db, conv)

    def test_list_recent_orders_by_updated_at_desc(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        repo.insert_project(db, project)
        old = Conversation(
            uuid="old",
            title="vieja",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        new = Conversation(
            uuid="new",
            title="nueva",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, old)
        repo.insert_conversation(db, new)
        rows = repo.list_recent_conversations(db, limit=10)
        assert [c.uuid for c in rows] == ["new", "old"]


class TestMessageRepo:
    def test_insert_and_get(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
        human_message: Message,
        assistant_message: Message,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        repo.insert_message(db, human_message)
        repo.insert_message(db, assistant_message)
        loaded = repo.get_messages_for_conversation(db, conversation.uuid)
        assert [m.uuid for m in loaded] == [human_message.uuid, assistant_message.uuid]
        assert loaded[1].parent_uuid == human_message.uuid
        assert loaded[1].raw_content is not None
        assert loaded[1].raw_content[0]["type"] == "text"

    def test_cascade_delete_on_conversation(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
        human_message: Message,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        repo.insert_message(db, human_message)
        db.execute("DELETE FROM conversations WHERE uuid = ?", (conversation.uuid,))
        assert repo.get_messages_for_conversation(db, conversation.uuid) == []


class TestChunkRepoAndVectorSearch:
    def test_add_chunk_and_search(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
        chunk: Chunk,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        embedding = [0.1] * 768
        chunk_id = repo.add_chunk(db, chunk, embedding)
        assert chunk_id > 0
        assert repo.count_chunks(db) == 1
        loaded = repo.get_chunk(db, chunk_id)
        assert loaded is not None
        assert loaded.text == chunk.text

        hits = repo.vector_search(db, embedding, limit=5)
        assert len(hits) == 1
        assert hits[0].chunk.id == chunk_id
        assert hits[0].conversation.uuid == conversation.uuid
        assert hits[0].distance == pytest.approx(0.0, abs=1e-6)

    def test_chunk_linked_to_message(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
        human_message: Message,
        assistant_message: Message,
        chunk_with_message: Chunk,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        repo.insert_message(db, human_message)
        repo.insert_message(db, assistant_message)
        embedding = [0.2] * 768
        chunk_id = repo.add_chunk(db, chunk_with_message, embedding)
        loaded = repo.get_chunk(db, chunk_id)
        assert loaded is not None
        assert loaded.message_uuid == assistant_message.uuid

    def test_chunk_orphan_message_fk_rejected(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        orphan_chunk = Chunk(
            conversation_uuid=conversation.uuid,
            message_uuid="msg-no-existe",
            text="texto",
            char_start=0,
            char_end=5,
            created_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.add_chunk(db, orphan_chunk, [0.0] * 768)

    def test_search_ranks_closer_first(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)

        def mk_chunk(text: str) -> Chunk:
            return Chunk(
                conversation_uuid=conversation.uuid,
                text=text,
                char_start=0,
                char_end=len(text),
                created_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
            )

        repo.add_chunk(db, mk_chunk("close"), [1.0] + [0.0] * 767)
        repo.add_chunk(db, mk_chunk("far"), [0.0] + [1.0] + [0.0] * 766)

        hits = repo.vector_search(db, [1.0] + [0.0] * 767, limit=5)
        assert len(hits) == 2
        assert hits[0].chunk.text == "close"
        assert hits[0].distance < hits[1].distance

"""Tests del pipeline de ingest. Usa FakeEmbedder y DB in-memory."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.ingest.pipeline import ingest_export
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init


def _build_zip(tmp_path: Path, files: dict[str, str]) -> Path:
    """Construye un zip con los archivos dados (path → contenido string)."""
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return zip_path


PROJECT_JSON = {
    "uuid": "p-1",
    "name": "Proyecto Test",
    "description": "Para tests",
    "prompt_template": "Sos un asistente.",
    "is_private": True,
    "is_starter_project": False,
    "creator": {"full_name": "Dioni", "uuid": "user-1"},
    "created_at": "2026-05-01T12:00:00.000Z",
    "updated_at": "2026-05-01T12:00:00.000Z",
    "docs": [],
}

CONVERSATION_JSON = {
    "uuid": "conv-1",
    "name": "Hola mundo",
    "summary": "Una charla corta.",
    "created_at": "2026-05-10T10:00:00.000Z",
    "updated_at": "2026-05-10T10:05:00.000Z",
    "account": {"uuid": "acct-1"},
    "chat_messages": [
        {
            "uuid": "msg-1",
            "text": "hola",
            "content": [{"type": "text", "text": "hola"}],
            "sender": "human",
            "created_at": "2026-05-10T10:00:00.000Z",
            "updated_at": "2026-05-10T10:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": None,
        },
        {
            "uuid": "msg-2",
            "text": "qué tal",
            "content": [{"type": "text", "text": "qué tal"}],
            "sender": "assistant",
            "created_at": "2026-05-10T10:00:10.000Z",
            "updated_at": "2026-05-10T10:00:10.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": "msg-1",
        },
    ],
}

DESIGN_CHAT_JSON = {
    "uuid": "design-1",
    "title": "Diseño",
    "project": {"name": "Proyecto Test", "uuid": "p-1"},
    "created_at": "2026-05-12T10:00:00.000Z",
    "updated_at": "2026-05-12T10:30:00.000Z",
    "messages": [
        {
            "uuid": "msg-d-1",
            "text": "diseñemos el schema",
            "content": [{"type": "text", "text": "diseñemos el schema"}],
            "sender": "human",
            "created_at": "2026-05-12T10:00:00.000Z",
            "updated_at": "2026-05-12T10:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": None,
        }
    ],
}

MEMORIES_JSON = [{"conversations_memory": "Memoria curada de prueba.", "account_uuid": "acct-1"}]


class TestPipelineEndToEnd:
    def test_ingest_full_export(self, tmp_path: Path) -> None:
        zip_path = _build_zip(
            tmp_path,
            {
                "projects/p-1.json": json.dumps(PROJECT_JSON),
                "conversations.json": json.dumps([CONVERSATION_JSON]),
                "design_chats/design-1.json": json.dumps(DESIGN_CHAT_JSON),
                "memories.json": json.dumps(MEMORIES_JSON),
            },
        )
        db = connect_and_init(":memory:")
        try:
            summary = ingest_export(db, zip_path, FakeEmbedder(dim=768))

            assert summary.projects == 1
            assert summary.conversations == 3  # 1 sueltas + 1 design_chat + 1 memory
            assert summary.messages == 4  # 2 + 1 + 1
            assert summary.chunks > 0
            assert summary.errors == []

            assert repo.get_project(db, "p-1") is not None
            assert repo.get_conversation(db, "conv-1") is not None
            assert repo.get_conversation(db, "design-1") is not None

            memory_conv = repo.get_conversation(db, "memory-acct-1")
            assert memory_conv is not None
        finally:
            db.close()

    def test_search_after_ingest_returns_relevant_hit(self, tmp_path: Path) -> None:
        zip_path = _build_zip(
            tmp_path,
            {"conversations.json": json.dumps([CONVERSATION_JSON])},
        )
        db = connect_and_init(":memory:")
        try:
            embedder = FakeEmbedder(dim=768)
            ingest_export(db, zip_path, embedder)

            # Como FakeEmbedder es determinístico, buscar la frase exacta debería traerla primero.
            full_text = "[human]\nhola\n\n[assistant]\nqué tal\n\n"
            query_vec = embedder.embed_one(full_text)
            hits = repo.vector_search(db, query_vec, limit=3)
            assert len(hits) >= 1
            assert hits[0].conversation.uuid == "conv-1"
        finally:
            db.close()

    def test_reingest_is_idempotent(self, tmp_path: Path) -> None:
        """Re-ingestar el mismo export no debe duplicar chunks."""
        zip_path = _build_zip(
            tmp_path,
            {"conversations.json": json.dumps([CONVERSATION_JSON])},
        )
        db = connect_and_init(":memory:")
        try:
            embedder = FakeEmbedder(dim=768)
            ingest_export(db, zip_path, embedder)
            chunks_first = repo.count_chunks(db)

            ingest_export(db, zip_path, embedder)
            chunks_second = repo.count_chunks(db)

            assert chunks_first == chunks_second
            assert chunks_first > 0
        finally:
            db.close()

    def test_handles_empty_messages(self, tmp_path: Path) -> None:
        """Mensajes sin texto se cuentan como skipped pero no rompen el ingest."""
        data = {
            **CONVERSATION_JSON,
            "uuid": "conv-empty",
            "chat_messages": [
                {
                    "uuid": "msg-empty",
                    "text": "",
                    "content": [],
                    "sender": "human",
                    "created_at": "2026-05-10T10:00:00.000Z",
                    "updated_at": "2026-05-10T10:00:00.000Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": None,
                },
                {
                    "uuid": "msg-with-text",
                    "text": "hola igual",
                    "content": [{"type": "text", "text": "hola igual"}],
                    "sender": "human",
                    "created_at": "2026-05-10T10:00:10.000Z",
                    "updated_at": "2026-05-10T10:00:10.000Z",
                    "attachments": [],
                    "files": [],
                    "parent_message_uuid": None,
                },
            ],
        }
        zip_path = _build_zip(tmp_path, {"conversations.json": json.dumps([data])})
        db = connect_and_init(":memory:")
        try:
            summary = ingest_export(db, zip_path, FakeEmbedder(dim=768))
            assert summary.skipped_empty_messages == 1
            assert summary.messages == 2
            assert summary.chunks >= 1
        finally:
            db.close()

    def test_design_chat_with_missing_project_is_orphaned(self, tmp_path: Path) -> None:
        """Si un design_chat apunta a un project que no está en el export, ingesta
        igual con project_uuid=None (no rompe el FK)."""
        orphan_design = {
            **DESIGN_CHAT_JSON,
            "uuid": "design-orphan",
            "project": {"name": "Inexistente", "uuid": "project-que-no-existe"},
        }
        zip_path = _build_zip(
            tmp_path,
            {"design_chats/design-orphan.json": json.dumps(orphan_design)},
        )
        db = connect_and_init(":memory:")
        try:
            summary = ingest_export(db, zip_path, FakeEmbedder(dim=768))
            assert summary.errors == []
            assert summary.conversations == 1
            loaded = repo.get_conversation(db, "design-orphan")
            assert loaded is not None
            assert loaded.project_uuid is None
        finally:
            db.close()

    def test_no_memories_file_is_fine(self, tmp_path: Path) -> None:
        """Si el zip no trae memories.json, el ingest sigue normal."""
        zip_path = _build_zip(
            tmp_path,
            {"conversations.json": json.dumps([CONVERSATION_JSON])},
        )
        db = connect_and_init(":memory:")
        try:
            summary = ingest_export(db, zip_path, FakeEmbedder(dim=768))
            assert summary.conversations == 1
            assert repo.get_conversation(db, "memory-acct-1") is None
        finally:
            db.close()

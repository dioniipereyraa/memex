"""Tests del pipeline de ingest. Usa FakeEmbedder y DB in-memory."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from memex.core.embeddings.base import Embedder, EmbedderError
from memex.core.embeddings.fake import FakeEmbedder
from memex.core.ingest.pipeline import ingest_export, ingest_single_conversation
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


class FailingEmbedder(Embedder):
    """Embedder que falla en ciertas llamadas a `embed()`.

    Wrappea un FakeEmbedder para que las llamadas que no fallan devuelvan
    vectores válidos (así los chunks que entran antes del fallo lo hacen con
    embeddings reales, no ceros). Simula que el servicio de embeddings cae
    a mitad del ingest de una conversación con muchos batches.
    """

    def __init__(self, fail_on_calls: set[int] | None = None, dim: int = 768) -> None:
        self._inner = FakeEmbedder(dim=dim)
        self._fail_on_calls: set[int] = fail_on_calls or {2}
        self.calls = 0

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def model_name(self) -> str:
        return "failing-fake"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls in self._fail_on_calls:
            raise EmbedderError(f"simulated failure on call {self.calls}")
        return self._inner.embed(texts)


def _long_text_payload(uuid: str, n_chars: int = 20_000) -> dict:
    """Payload tipo `conversations.json` item con texto suficiente para forzar
    múltiples chunks (y por ende múltiples batches con batch_size chico).

    Pisa tanto `text` como `content[]` porque el parser usa `content[]` primero
    y solo cae a `text` como fallback si el render del content queda vacío.
    """
    long_text = "palabra " * (n_chars // 8)
    content = [{"type": "text", "text": long_text}]
    return {
        **CONVERSATION_JSON,
        "uuid": uuid,
        "chat_messages": [
            {
                **CONVERSATION_JSON["chat_messages"][0],
                "uuid": f"{uuid}-m1",
                "text": long_text,
                "content": content,
            },
            {
                **CONVERSATION_JSON["chat_messages"][1],
                "uuid": f"{uuid}-m2",
                "text": long_text,
                "content": content,
            },
        ],
    }


class TestPipelineRollback:
    """Verifica que un fallo del embedder a mitad del ingest no deja una
    conversación parcialmente persistida.

    Si esta property se rompe, una refactor futura del flow de commit /
    rollback puede dejar conversaciones fantasma en la base (con messages
    pero sin chunks, o con chunks parciales), y nadie se entera hasta que
    aparece un bug raro de retrieval. Estos tests son la red de seguridad.
    """

    def test_ingest_single_failure_rolls_back_and_reraises(self) -> None:
        payload = _long_text_payload("conv-rollback")
        db = connect_and_init(":memory:")
        try:
            embedder = FailingEmbedder(fail_on_calls={2})
            with pytest.raises(EmbedderError):
                ingest_single_conversation(db, embedder, payload, batch_size=2)

            # Rollback: la conv no debe estar en la base.
            assert repo.get_conversation(db, "conv-rollback") is None
            # Y tampoco sus mensajes.
            msgs = db.execute(
                "SELECT 1 FROM messages WHERE conversation_uuid = ?",
                ("conv-rollback",),
            ).fetchall()
            assert msgs == []
        finally:
            db.close()

    def test_ingest_export_failure_reports_error_without_persisting(self, tmp_path: Path) -> None:
        payload = _long_text_payload("conv-rollback")
        zip_path = _build_zip(tmp_path, {"conversations.json": json.dumps([payload])})
        db = connect_and_init(":memory:")
        try:
            embedder = FailingEmbedder(fail_on_calls={2})
            summary = ingest_export(db, zip_path, embedder, batch_size=2)

            # El error de la conv debe estar reportado.
            assert any("conv-rollback" in e for e in summary.errors)
            # Y la conv no debe haber quedado persistida.
            assert repo.get_conversation(db, "conv-rollback") is None
        finally:
            db.close()

    def test_failure_in_one_conv_does_not_affect_others(self, tmp_path: Path) -> None:
        """Si una conv falla a mitad, las otras del mismo export entran OK."""
        bad = _long_text_payload("conv-bad")
        good = {**CONVERSATION_JSON, "uuid": "conv-good"}
        zip_path = _build_zip(tmp_path, {"conversations.json": json.dumps([bad, good])})
        db = connect_and_init(":memory:")
        try:
            # "conv-bad" genera ≥2 batches con batch_size=2: el segundo call falla.
            # "conv-good" genera 1 batch (texto corto): es el 3er call, que NO está
            # en fail_on_calls={2}, así que pasa.
            embedder = FailingEmbedder(fail_on_calls={2})
            summary = ingest_export(db, zip_path, embedder, batch_size=2)

            assert repo.get_conversation(db, "conv-bad") is None
            assert repo.get_conversation(db, "conv-good") is not None
            assert any("conv-bad" in e for e in summary.errors)
            # "conv-good" no genera errores.
            assert not any("conv-good" in e for e in summary.errors)
        finally:
            db.close()


class TestPipelineContentHash:
    """Tests del campo `content_hash` que se persiste en cada ingest.

    El pipeline NO genera summaries (eso vive ahora en `tools.search_chats`,
    on-demand). Pero sí computa y persiste `content_hash` para que el lazy
    summarizer pueda detectar si el contenido cambió desde la última
    generación.
    """

    def _zip_with_conv(self, tmp_path: Path, payload: dict) -> Path:
        return _build_zip(tmp_path, {"conversations.json": json.dumps([payload])})

    def test_content_hash_is_persisted(self, tmp_path: Path) -> None:
        zip_path = self._zip_with_conv(tmp_path, CONVERSATION_JSON)
        db = connect_and_init(":memory:")
        try:
            ingest_export(db, zip_path, FakeEmbedder(dim=768))
            conv = repo.get_conversation(db, "conv-1")
            assert conv is not None
            assert conv.content_hash is not None
            assert len(conv.content_hash) == 64  # SHA-256 hex
            # El summary del export oficial se conserva tal cual (ningún
            # paso del pipeline lo pisa).
            assert conv.summary == "Una charla corta."
        finally:
            db.close()

    def test_reingest_same_content_preserves_cached_summary(self, tmp_path: Path) -> None:
        """Si una conv tiene summary cacheado (generado lazy por search_chats)
        y se re-ingesta sin cambios de contenido, el summary NO se pisa con
        el del parser. Si el hash cambia, el upsert se comporta como antes
        (toma el del parser, que típicamente es None o el del export).
        """
        zip_path = self._zip_with_conv(tmp_path, CONVERSATION_JSON)
        db = connect_and_init(":memory:")
        try:
            ingest_export(db, zip_path, FakeEmbedder(dim=768))
            # Simular un summary cacheado generado lazy.
            repo.update_conversation_summary(db, "conv-1", "Resumen lazy preexistente.")
            db.commit()

            # Re-ingest sin cambios de contenido.
            ingest_export(db, zip_path, FakeEmbedder(dim=768))
            conv = repo.get_conversation(db, "conv-1")
            assert conv is not None
            # El summary lazy se conserva, no se pisa con "Una charla corta."
            # del export.
            assert conv.summary == "Resumen lazy preexistente."
        finally:
            db.close()

    def test_reingest_modified_content_invalidates_cached_summary(self, tmp_path: Path) -> None:
        """Si el contenido cambia (hash distinto), el summary cacheado deja
        de coincidir y el upsert lo reemplaza por el del parser. La idea es
        que el próximo `search_chats` regenere lazy con el contenido nuevo.
        """
        db = connect_and_init(":memory:")
        try:
            zip1 = self._zip_with_conv(tmp_path, CONVERSATION_JSON)
            ingest_export(db, zip1, FakeEmbedder(dim=768))
            repo.update_conversation_summary(db, "conv-1", "Resumen viejo.")
            db.commit()

            # Agregar un mensaje cambia el hash.
            modified = {
                **CONVERSATION_JSON,
                "chat_messages": [
                    *CONVERSATION_JSON["chat_messages"],
                    {
                        "uuid": "msg-3",
                        "text": "mensaje nuevo",
                        "content": [{"type": "text", "text": "mensaje nuevo"}],
                        "sender": "human",
                        "created_at": "2026-05-10T10:01:00.000Z",
                        "updated_at": "2026-05-10T10:01:00.000Z",
                        "attachments": [],
                        "files": [],
                        "parent_message_uuid": "msg-2",
                    },
                ],
            }
            zip2 = self._zip_with_conv(tmp_path, modified)
            ingest_export(db, zip2, FakeEmbedder(dim=768))
            conv = repo.get_conversation(db, "conv-1")
            assert conv is not None
            # El summary lazy quedó stale; el upsert lo reemplaza con el
            # del export (en este caso "Una charla corta.").
            assert conv.summary == "Una charla corta."
        finally:
            db.close()

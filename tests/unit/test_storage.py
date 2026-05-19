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

    def test_delete_chunks_for_conversation_removes_chunks_and_vectors(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)

        for i in range(3):
            chunk = Chunk(
                conversation_uuid=conversation.uuid,
                text=f"texto {i}",
                char_start=i * 10,
                char_end=i * 10 + 7,
                created_at=datetime(2026, 3, 5, tzinfo=UTC),
            )
            repo.add_chunk(db, chunk, [0.1] * 768)

        assert repo.count_chunks(db) == 3
        n_vecs = db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert n_vecs == 3

        deleted = repo.delete_chunks_for_conversation(db, conversation.uuid)
        assert deleted == 3
        assert repo.count_chunks(db) == 0
        n_vecs_after = db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert n_vecs_after == 0

    def test_delete_chunks_returns_zero_when_no_chunks(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)
        assert repo.delete_chunks_for_conversation(db, conversation.uuid) == 0
        assert repo.delete_chunks_for_conversation(db, "no-existe") == 0

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

    def test_dedupe_by_conversation_keeps_best_per_chat(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        """Búsqueda con dedup devuelve a lo sumo un chunk por conversación."""
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)

        # Otra conversación para tener variedad.
        other_conv = Conversation(
            uuid="conv-otra",
            title="Otra charla",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 3, 6, tzinfo=UTC),
            updated_at=datetime(2026, 3, 6, tzinfo=UTC),
        )
        repo.insert_conversation(db, other_conv)

        def mk_chunk(conv_uuid: str, text: str) -> Chunk:
            return Chunk(
                conversation_uuid=conv_uuid,
                text=text,
                char_start=0,
                char_end=len(text),
                created_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
            )

        # 3 chunks de conv-0001, 1 chunk de conv-otra, todos con embeddings parecidos.
        repo.add_chunk(db, mk_chunk(conversation.uuid, "chunk1 muy parecido"), [1.0] + [0.01] * 767)
        repo.add_chunk(db, mk_chunk(conversation.uuid, "chunk2 algo parecido"), [1.0] + [0.02] * 767)
        repo.add_chunk(db, mk_chunk(conversation.uuid, "chunk3 menos parecido"), [1.0] + [0.05] * 767)
        repo.add_chunk(db, mk_chunk(other_conv.uuid, "chunk4 distinto chat"), [0.99] + [0.03] * 767)

        # Sin dedup: devuelve los 4 chunks.
        all_hits = repo.vector_search(db, [1.0] + [0.0] * 767, limit=10, dedupe_by_conversation=False)
        assert len(all_hits) == 4

        # Con dedup: a lo sumo 1 por conversación, total 2.
        deduped = repo.vector_search(db, [1.0] + [0.0] * 767, limit=10, dedupe_by_conversation=True)
        assert len(deduped) == 2
        uuids = {h.conversation.uuid for h in deduped}
        assert uuids == {conversation.uuid, other_conv.uuid}

    def test_dedupe_is_default(
        self,
        db: sqlite3.Connection,
        project: Project,
        conversation: Conversation,
    ) -> None:
        """Por default, vector_search dedupea."""
        repo.insert_project(db, project)
        repo.insert_conversation(db, conversation)

        for i in range(3):
            chunk = Chunk(
                conversation_uuid=conversation.uuid,
                text=f"chunk {i}",
                char_start=i * 10,
                char_end=i * 10 + 5,
                created_at=datetime(2026, 3, 5, tzinfo=UTC),
            )
            repo.add_chunk(db, chunk, [1.0] + [0.0] * 767)

        hits = repo.vector_search(db, [1.0] + [0.0] * 767, limit=5)
        assert len(hits) == 1

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

        # Ambos chunks viven en la misma conversación, así que con dedup activado
        # solo veríamos uno. Acá testeamos el ranking puro, sin dedup.
        hits = repo.vector_search(
            db, [1.0] + [0.0] * 767, limit=5, dedupe_by_conversation=False
        )
        assert len(hits) == 2
        assert hits[0].chunk.text == "close"
        assert hits[0].distance < hits[1].distance


class TestTextSearchAndFTS:
    def _seed(self, db: sqlite3.Connection, project: Project) -> None:
        repo.insert_project(db, project)
        convs = [
            ("conv-amarok", "Mi Volkswagen Amarok 2020"),
            ("conv-otra", "Una pregunta de calculo"),
            ("conv-mezcla", "Notas variadas sobre la facu y otros temas"),
        ]
        for uuid, title in convs:
            conv = Conversation(
                uuid=uuid,
                title=title,
                source=Source.CONVERSATIONS,
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            repo.insert_conversation(db, conv)

        texts = [
            ("conv-amarok", "Estoy pensando en comprar una Amarok diesel V6, qué opinás del precio"),
            ("conv-otra", "Calculá la derivada parcial respecto de x en el punto dado"),
            ("conv-mezcla", "Mañana tengo parcial de álgebra lineal en la facu"),
        ]
        for conv_uuid, text in texts:
            chunk = Chunk(
                conversation_uuid=conv_uuid,
                text=text,
                char_start=0,
                char_end=len(text),
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            # Embeddings random pero consistentes; lo importante para FTS es el texto.
            repo.add_chunk(db, chunk, [0.1] * 768)

    def test_text_search_finds_exact_word(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        """FTS5 encuentra 'Amarok' aunque la query semántica fallaría."""
        self._seed(db, project)
        hits = repo.text_search(db, "Amarok", limit=5)
        assert len(hits) >= 1
        assert hits[0].conversation.uuid == "conv-amarok"

    def test_text_search_is_case_insensitive(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        self._seed(db, project)
        hits = repo.text_search(db, "amarok", limit=5)
        assert any(h.conversation.uuid == "conv-amarok" for h in hits)

    def test_text_search_empty_query_returns_empty(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        self._seed(db, project)
        assert repo.text_search(db, "", limit=5) == []
        assert repo.text_search(db, "   ", limit=5) == []

    def test_text_search_handles_special_characters(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        """Operadores raros se sanitizan; no debe propagar OperationalError."""
        self._seed(db, project)
        # Estos serían inválidos en FTS5 sin sanitizar.
        for bad in ("(", '"', ":", "AND OR"):
            result = repo.text_search(db, bad, limit=5)
            assert isinstance(result, list)

    def test_text_search_dedupes_by_conversation(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        self._seed(db, project)
        # Insertar otro chunk con el mismo texto en la misma conversación.
        extra = Chunk(
            conversation_uuid="conv-amarok",
            text="Amarok otra mención del modelo",
            char_start=100,
            char_end=130,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.add_chunk(db, extra, [0.1] * 768)
        deduped = repo.text_search(db, "Amarok", limit=5)
        all_chunks = repo.text_search(db, "Amarok", limit=5, dedupe_by_conversation=False)
        assert len(deduped) <= len(all_chunks)
        assert len({h.conversation.uuid for h in deduped}) == len(deduped)

    def test_rebuild_fts_index_repopulates(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        """Vaciar el índice FTS y reconstruirlo deja la búsqueda funcional otra vez."""
        self._seed(db, project)
        db.execute("DELETE FROM fts_chunks")
        assert repo.text_search(db, "Amarok", limit=5) == []
        n = repo.rebuild_fts_index(db)
        assert n == 3  # 3 chunks sembrados por _seed
        hits = repo.text_search(db, "Amarok", limit=5)
        assert hits and hits[0].conversation.uuid == "conv-amarok"


class TestHybridSearch:
    def test_hybrid_finds_when_only_text_matches(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        """Caso Amarok: vector no encuentra (vectores random), pero FTS sí.
        El híbrido tiene que rescatarlo."""
        repo.insert_project(db, project)
        conv = Conversation(
            uuid="conv-amarok",
            title="Amarok",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        other = Conversation(
            uuid="conv-otra",
            title="Otra",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        repo.insert_conversation(db, other)

        chunk_a = Chunk(
            conversation_uuid="conv-amarok",
            text="Mi Amarok V6 anda increíble",
            char_start=0,
            char_end=30,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        chunk_b = Chunk(
            conversation_uuid="conv-otra",
            text="Hablemos de cualquier otra cosa",
            char_start=0,
            char_end=30,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        # Embedding del chunk-b es CERCANO al query embedding (gana vector),
        # pero el chunk-a tiene la palabra exacta en el texto (gana FTS).
        repo.add_chunk(db, chunk_a, [0.0] * 768)  # lejos del query
        repo.add_chunk(db, chunk_b, [1.0] + [0.0] * 767)  # cerca del query

        query_vec = [1.0] + [0.0] * 767

        # Vector solo: el otro chat gana.
        vec_only = repo.vector_search(db, query_vec, limit=5)
        assert vec_only[0].conversation.uuid == "conv-otra"

        # Híbrido: el Amarok aparece arriba por la mitad lexical.
        hybrid = repo.hybrid_search(db, "Amarok", query_vec, limit=5)
        uuids = [h.conversation.uuid for h in hybrid]
        assert "conv-amarok" in uuids

    def test_hybrid_dedupes_by_conversation(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        repo.insert_project(db, project)
        conv = Conversation(
            uuid="conv-a",
            title="A",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        for i in range(3):
            chunk = Chunk(
                conversation_uuid="conv-a",
                text=f"Amarok mención {i}",
                char_start=i * 10,
                char_end=i * 10 + 20,
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            repo.add_chunk(db, chunk, [1.0] + [0.0] * 767)
        hits = repo.hybrid_search(db, "Amarok", [1.0] + [0.0] * 767, limit=5)
        assert len({h.conversation.uuid for h in hits}) == len(hits)

    def test_hybrid_empty_when_no_match(
        self, db: sqlite3.Connection, project: Project
    ) -> None:
        repo.insert_project(db, project)
        # Base vacía: ni vector ni texto encuentran nada.
        hits = repo.hybrid_search(db, "anything", [0.0] * 768, limit=5)
        assert hits == []

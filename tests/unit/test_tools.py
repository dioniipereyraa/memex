"""Tests de las tools puras de `memex.transports.tools`.

Tests directos sobre las funciones que devuelven dicts. La capa MCP de
`mcp_server.py` solo serializa estos dicts a JSON, así que testear acá cubre
toda la lógica de retrieval que va a expose el servidor.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memex.core.embeddings.base import Embedder, EmbedderError
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

    def test_empty_query_returns_error(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="   ", limit=5)
        assert "error" in result
        assert "empty" in result["error"].lower()

    def test_invalid_source_returns_error(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="x", source="inventado")
        assert "error" in result
        assert "inventado" in result["error"]

    def test_limit_clamped_to_max(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="x", limit=999)
        assert "results" in result
        assert len(result["results"]) <= 50

    def test_limit_clamped_to_min(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="x", limit=0)
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

    def test_result_shape_includes_required_fields(self, populated_db: sqlite3.Connection) -> None:
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

    def test_embedder_error_becomes_json_error(self, populated_db: sqlite3.Connection) -> None:
        """`tools.search_chats` atrapa EmbedderError y devuelve un dict con `error`.

        Esto es lo que permite que `mcp_server.search_chats` no necesite atraparlo:
        el dict ya viene formateado para serializar a JSON.
        """

        class _BrokenEmbedder(Embedder):
            @property
            def dim(self) -> int:
                return 768

            @property
            def model_name(self) -> str:
                return "broken"

            def embed(self, texts):  # type: ignore[override]
                raise EmbedderError("Ollama no responde")

        result = tools.search_chats(populated_db, _BrokenEmbedder(), query="x")
        assert "error" in result
        assert "Ollama no responde" in result["error"]

    def test_invalid_mode_returns_error(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(), query="x", mode="inventado")
        assert "error" in result
        assert "inventado" in result["error"]

    def test_default_mode_is_hybrid(self, populated_db: sqlite3.Connection) -> None:
        result = tools.search_chats(populated_db, FakeEmbedder(dim=768), query="hola")
        assert result.get("mode") == "hybrid"

    def test_lexical_mode_skips_embedder(self, populated_db: sqlite3.Connection) -> None:
        """Modo lexical no debe pedirle nada al embedder (no necesita Ollama)."""

        class _ExplodingEmbedder(Embedder):
            @property
            def dim(self) -> int:
                return 768

            @property
            def model_name(self) -> str:
                return "explosivo"

            def embed(self, texts):  # type: ignore[override]
                raise AssertionError("No debería llamarse en modo lexical")

        result = tools.search_chats(
            populated_db, _ExplodingEmbedder(), query="hola", mode="lexical"
        )
        # No error: el embedder no se invocó.
        assert result.get("mode") == "lexical"
        assert "results" in result

    def test_long_summary_is_truncated(self, db: sqlite3.Connection, project: Project) -> None:
        """Summaries de varios miles de chars hinchan el response. Se truncan."""
        repo.insert_project(db, project)
        huge_summary = "S" * 3000
        conv = Conversation(
            uuid="big-summary",
            title="Conv",
            summary=huge_summary,
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
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

        result = tools.search_chats(db, embedder, query="hola", limit=1)
        assert result["results"]
        assert len(result["results"][0]["summary"]) < 3000
        assert result["results"][0]["summary"].endswith("…[truncated]")


class TestSearchChatsLazySummaries:
    """Tests del wire del Summarizer on-demand en `tools.search_chats`."""

    def _populate_n_chats(
        self,
        db: sqlite3.Connection,
        n: int,
        with_summary: list[bool] | None = None,
    ) -> list[str]:
        """Crea N conversaciones con un chunk cada una. `with_summary[i]`
        indica si la conv `i` arranca con summary cacheado."""
        embedder = FakeEmbedder(dim=768)
        uuids: list[str] = []
        flags = with_summary if with_summary is not None else [False] * n
        for i in range(n):
            uuid = f"conv-{i:03d}"
            conv = Conversation(
                uuid=uuid,
                title=f"Chat {i}",
                summary=f"Summary cacheado de {i}" if flags[i] else None,
                source=Source.CONVERSATIONS,
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            repo.insert_conversation(db, conv)
            msg = Message(
                uuid=f"msg-{i:03d}",
                conversation_uuid=uuid,
                sender=Sender.HUMAN,
                text=f"hola mundo desde el chat {i}",
                created_at=conv.created_at,
                updated_at=conv.updated_at,
            )
            repo.insert_message(db, msg)
            chunk = Chunk(
                conversation_uuid=uuid,
                text=msg.text,
                char_start=0,
                char_end=len(msg.text),
                created_at=conv.updated_at,
            )
            repo.add_chunk(db, chunk, embedder.embed_one(msg.text))
            uuids.append(uuid)
        return uuids

    def test_no_summarizer_means_no_generation(self, db: sqlite3.Connection) -> None:
        """Sin summarizer pasado, search_chats devuelve summary=None tal cual está en DB."""
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 2)
        summarizer = FakeSummarizer()
        # Pasar summarizer=None explícito.
        result = tools.search_chats(db, FakeEmbedder(dim=768), query="hola", summarizer=None)
        assert result["count"] >= 1
        for r in result["results"]:
            assert r["summary"] is None
        # El summarizer no se llamó (sentinel: pasamos uno, pero como None,
        # el flow no debería tocarlo).
        assert summarizer.calls == 0

    def test_lazy_generates_for_results_without_summary(self, db: sqlite3.Connection) -> None:
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 2)
        summarizer = FakeSummarizer(max_words=4)
        result = tools.search_chats(
            db, FakeEmbedder(dim=768), query="hola", limit=2, summarizer=summarizer
        )
        # Los 2 results vienen sin summary cacheado → 2 generaciones.
        assert summarizer.calls == 2
        for r in result["results"]:
            assert r["summary"] is not None
            assert r["summary"].startswith("Chat ")  # FakeSummarizer usa el title.

    def test_lazy_skips_already_cached(self, db: sqlite3.Connection) -> None:
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 2, with_summary=[True, False])
        summarizer = FakeSummarizer(max_words=4)
        result = tools.search_chats(
            db, FakeEmbedder(dim=768), query="hola", limit=2, summarizer=summarizer
        )
        # Solo 1 generación (el que no tenía summary). El otro se respeta tal cual.
        assert summarizer.calls == 1
        summaries = {r["conversation_uuid"]: r["summary"] for r in result["results"]}
        assert summaries["conv-000"] == "Summary cacheado de 0"

    def test_lazy_caps_at_three(self, db: sqlite3.Connection) -> None:
        """Si hay más de SEARCH_SUMMARY_LAZY_CAP candidatos sin summary, solo
        se generan los primeros 3 (los más relevantes). El resto queda sin
        summary en este response, pero el siguiente search puede generarlos."""
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 5)
        summarizer = FakeSummarizer()
        tools.search_chats(db, FakeEmbedder(dim=768), query="hola", limit=5, summarizer=summarizer)
        assert summarizer.calls == tools.SEARCH_SUMMARY_LAZY_CAP
        # Los summaries generados están persistidos.
        with_summary = db.execute(
            "SELECT COUNT(*) FROM conversations WHERE summary IS NOT NULL"
        ).fetchone()[0]
        assert with_summary == tools.SEARCH_SUMMARY_LAZY_CAP

    def test_lazy_persists_to_db(self, db: sqlite3.Connection) -> None:
        """Después de un search, el summary queda persistido (próximo search es cache hit)."""
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 1)
        summarizer = FakeSummarizer()
        # Primera búsqueda: genera 1.
        tools.search_chats(db, FakeEmbedder(dim=768), query="hola", summarizer=summarizer)
        assert summarizer.calls == 1
        # En DB queda el summary.
        conv = repo.get_conversation(db, "conv-000")
        assert conv is not None
        assert conv.summary is not None

        # Segunda búsqueda: cache hit, no se llama de nuevo.
        tools.search_chats(db, FakeEmbedder(dim=768), query="hola", summarizer=summarizer)
        assert summarizer.calls == 1  # sin cambios

    def test_lazy_silent_fail_per_chat(self, db: sqlite3.Connection) -> None:
        """Si el summarizer levanta SummarizerError, ese result viene sin
        summary pero los demás siguen normales y el search no aborta."""
        from memex.core.summaries.fake import FakeSummarizer

        self._populate_n_chats(db, 2)
        # FakeSummarizer(fail=True) falla siempre.
        summarizer = FakeSummarizer(fail=True)
        result = tools.search_chats(
            db, FakeEmbedder(dim=768), query="hola", limit=2, summarizer=summarizer
        )
        # El search NO aborta.
        assert result.get("count", 0) >= 1
        # Ninguno tiene summary nuevo.
        for r in result["results"]:
            assert r["summary"] is None
        # No quedó persistido (no hubo summary exitoso).
        with_summary = db.execute(
            "SELECT COUNT(*) FROM conversations WHERE summary IS NOT NULL"
        ).fetchone()[0]
        assert with_summary == 0


class TestSearchChatsRepoBoost:
    """Tests of the `repo=...` boost in `tools.search_chats`.

    `_apply_repo_boost` and `_resolve_repo_key` are tested directly for
    determinism; the integration cases use the FakeEmbedder + populated DB
    fixture to verify the param plumbs through end-to-end.
    """

    def test_apply_boost_lowers_distance_for_matched_hits(self) -> None:
        """Hits in the boost map get their distance reduced by weight * confidence."""
        from memex.core.models import Chunk, Conversation, SearchHit, Source
        from memex.transports.tools import REPO_BOOST_WEIGHT, _apply_repo_boost

        def _hit(uuid: str, distance: float) -> SearchHit:
            return SearchHit(
                chunk=Chunk(
                    conversation_uuid=uuid,
                    text="x",
                    char_start=0,
                    char_end=1,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                conversation=Conversation(
                    uuid=uuid,
                    title=uuid,
                    source=Source.CONVERSATIONS,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                distance=distance,
                snippet=uuid,
            )

        # Three hits: c1 is ahead, c2 is behind by a small margin, c3 is far.
        hits = [_hit("c1", 0.10), _hit("c2", 0.15), _hit("c3", 0.40)]
        # c2 belongs to the repo with high confidence; c3 with low; c1 not at all.
        result = _apply_repo_boost(hits, {"c2": 1.0, "c3": 0.5})
        by_uuid = {h.conversation.uuid: h.distance for h in result}
        # c2 distance is 0.15 - 0.3 * 1.0 = -0.15.
        assert by_uuid["c2"] == pytest.approx(0.15 - REPO_BOOST_WEIGHT * 1.0)
        # c3 distance is 0.40 - 0.3 * 0.5 = 0.25.
        assert by_uuid["c3"] == pytest.approx(0.40 - REPO_BOOST_WEIGHT * 0.5)
        # c1 unchanged.
        assert by_uuid["c1"] == 0.10
        # Ordering after boost: c2 (-0.15), c1 (0.10), c3 (0.25).
        assert [h.conversation.uuid for h in result] == ["c2", "c1", "c3"]

    def test_resolve_repo_key_accepts_canonical_key(self, db: sqlite3.Connection) -> None:
        from memex.core.repos.discovery import RepoInfo
        from memex.transports.tools import _resolve_repo_key

        repo.insert_repo(
            db,
            RepoInfo(
                key="github.com/me/proj",
                path="/dev/proj",
                remote_url="git@github.com:me/proj.git",
                name="proj",
                manifest_name=None,
            ),
        )
        assert _resolve_repo_key(db, "github.com/me/proj") == "github.com/me/proj"

    def test_resolve_repo_key_accepts_remote_url(self, db: sqlite3.Connection) -> None:
        from memex.core.repos.discovery import RepoInfo
        from memex.transports.tools import _resolve_repo_key

        repo.insert_repo(
            db,
            RepoInfo(
                key="github.com/me/proj",
                path=None,
                remote_url="git@github.com:me/proj.git",
                name="proj",
                manifest_name=None,
            ),
        )
        # Pass HTTPS form; resolver normalizes to the canonical key.
        assert _resolve_repo_key(db, "https://github.com/me/proj.git") == "github.com/me/proj"

    def test_resolve_repo_key_unknown_returns_none(self, db: sqlite3.Connection) -> None:
        from memex.transports.tools import _resolve_repo_key

        assert _resolve_repo_key(db, "github.com/nobody/none") is None

    def test_unknown_repo_arg_returns_error(self, db: sqlite3.Connection) -> None:
        """An unregistered `repo_arg` short-circuits with an actionable error."""
        result = tools.search_chats(
            db, FakeEmbedder(dim=768), query="anything", repo_arg="github.com/nobody/none"
        )
        assert "error" in result
        assert "github.com/nobody/none" in result["error"]
        assert "memex repos add" in result["error"]

    def test_no_repo_arg_means_no_boost(self, db: sqlite3.Connection) -> None:
        """Sanity: without `repo_arg` the search runs as before."""
        # Populate a couple of chats.
        TestSearchChatsLazySummaries()._populate_n_chats(db, 2)
        result = tools.search_chats(db, FakeEmbedder(dim=768), query="hola", limit=2)
        assert result["count"] >= 1
        # results have a `distance` field present.
        for r in result["results"]:
            assert "distance" in r

    def test_boost_changes_ranking(self, db: sqlite3.Connection) -> None:
        """End-to-end: a chat associated to the queried repo outranks an unrelated
        chat that would normally rank similar or better.

        We use FakeEmbedder so the base ranking is arbitrary (hash-based) but
        stable. We pick the chat that ranked second (or worse) without boost
        and associate it. After boost, it should appear above whatever ranked
        first before.
        """
        from memex.core.repos.discovery import RepoInfo

        uuids = TestSearchChatsLazySummaries()._populate_n_chats(db, 3)
        # Run without boost to see the base order.
        baseline = tools.search_chats(db, FakeEmbedder(dim=768), query="hola", limit=3)
        baseline_order = [r["conversation_uuid"] for r in baseline["results"]]
        assert len(baseline_order) >= 2

        # Pick the chat that ranks LAST in baseline. Associate it to a repo.
        loser = baseline_order[-1]
        repo.insert_repo(
            db,
            RepoInfo(
                key="github.com/test/boostme",
                path=None,
                remote_url="github.com/test/boostme",
                name="boostme",
                manifest_name=None,
            ),
        )
        repo.associate_chat_repo(
            db, loser, "github.com/test/boostme", source="auto", confidence=1.0
        )
        db.commit()

        boosted = tools.search_chats(
            db,
            FakeEmbedder(dim=768),
            query="hola",
            limit=3,
            repo_arg="github.com/test/boostme",
        )
        boosted_order = [r["conversation_uuid"] for r in boosted["results"]]
        # The loser is now first (the boost was strong enough to flip the order).
        assert boosted_order[0] == loser
        # All baseline UUIDs are still present (boost re-ranks, does not filter).
        assert set(boosted_order) == set(baseline_order)
        assert uuids  # populated something


class TestFindRelated:
    """Tests for the `find_related(context, limit, repo)` MCP tool."""

    def _populate(self, db: sqlite3.Connection, n: int) -> list[str]:
        """Insert n chats with a chunk each. Reuses the lazy-summaries helper."""
        return TestSearchChatsLazySummaries()._populate_n_chats(db, n)

    def test_empty_context_returns_error(self, db: sqlite3.Connection) -> None:
        result = tools.find_related(db, FakeEmbedder(dim=768), context="   ")
        assert "error" in result
        assert "empty" in result["error"].lower()

    def test_returns_results_with_expected_shape(self, db: sqlite3.Connection) -> None:
        self._populate(db, 3)
        result = tools.find_related(db, FakeEmbedder(dim=768), context="some long text here")
        assert "results" in result
        assert "count" in result
        assert "context_chars" in result
        if result["results"]:
            r0 = result["results"][0]
            for key in (
                "rank",
                "conversation_uuid",
                "title",
                "source",
                "distance",
                "snippet",
            ):
                assert key in r0

    def test_truncates_long_context(self, db: sqlite3.Connection) -> None:
        """If context exceeds FIND_RELATED_MAX_INPUT_CHARS, only the prefix is used."""
        self._populate(db, 1)
        long_ctx = "x" * (tools.FIND_RELATED_MAX_INPUT_CHARS + 1000)
        result = tools.find_related(db, FakeEmbedder(dim=768), context=long_ctx)
        assert result["context_chars"] == tools.FIND_RELATED_MAX_INPUT_CHARS

    def test_limit_clamped(self, db: sqlite3.Connection) -> None:
        self._populate(db, 3)
        result = tools.find_related(db, FakeEmbedder(dim=768), context="hi", limit=9999)
        assert len(result["results"]) <= tools.SEARCH_LIMIT_MAX

    def test_unknown_repo_returns_error(self, db: sqlite3.Connection) -> None:
        result = tools.find_related(
            db, FakeEmbedder(dim=768), context="hi", repo_arg="github.com/no/such"
        )
        assert "error" in result
        assert "no/such" in result["error"]

    def test_repo_boost_reorders(self, db: sqlite3.Connection) -> None:
        """Same boost mechanic as search_chats: associated chats outrank unrelated ones."""
        from memex.core.repos.discovery import RepoInfo

        uuids = self._populate(db, 3)
        # Baseline: top-1 without boost.
        baseline = tools.find_related(db, FakeEmbedder(dim=768), context="hola", limit=3)
        assert len(baseline["results"]) >= 2
        baseline_first = baseline["results"][0]["conversation_uuid"]

        # Pick a non-first chat and associate to a repo.
        non_first = next(
            r["conversation_uuid"]
            for r in baseline["results"]
            if r["conversation_uuid"] != baseline_first
        )
        repo.insert_repo(
            db,
            RepoInfo(
                key="boost/repo",
                path=None,
                remote_url=None,
                name="repo",
                manifest_name=None,
            ),
        )
        repo.associate_chat_repo(db, non_first, "boost/repo", source="auto", confidence=1.0)
        db.commit()

        boosted = tools.find_related(
            db, FakeEmbedder(dim=768), context="hola", limit=3, repo_arg="boost/repo"
        )
        assert boosted["results"][0]["conversation_uuid"] == non_first
        assert uuids  # populated something

    def test_embedder_error_becomes_json_error(self, db: sqlite3.Connection) -> None:
        class _BrokenEmbedder(Embedder):
            @property
            def dim(self) -> int:
                return 768

            @property
            def model_name(self) -> str:
                return "broken"

            def embed(self, texts):  # type: ignore[override]
                raise EmbedderError("Ollama down")

        result = tools.find_related(db, _BrokenEmbedder(), context="hi")
        assert "error" in result
        assert "Ollama down" in result["error"]


class TestGetChat:
    def test_basic_get(self, populated_db: sqlite3.Connection, conversation: Conversation) -> None:
        result = tools.get_chat(populated_db, conversation.uuid)
        assert result["uuid"] == conversation.uuid
        assert result["title"] == conversation.title
        assert result["total_messages"] == 2
        assert result["messages_returned"] == 2
        assert result["truncated"] is False
        assert len(result["messages"]) == 2
        assert result["messages"][0]["sender"] == Sender.HUMAN.value
        assert result["messages"][1]["sender"] == Sender.ASSISTANT.value

    def test_raw_content_is_stripped(
        self, populated_db: sqlite3.Connection, conversation: Conversation
    ) -> None:
        """raw_content pesa mucho y rara vez es útil; no debe estar en el response."""
        result = tools.get_chat(populated_db, conversation.uuid)
        for msg in result["messages"]:
            assert "raw_content" not in msg

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

    def test_get_unknown_uuid_returns_error(self, populated_db: sqlite3.Connection) -> None:
        result = tools.get_chat(populated_db, "no-existe-este-uuid")
        assert "error" in result
        assert "no-existe-este-uuid" in result["error"]

    def test_get_empty_uuid_returns_error(self, populated_db: sqlite3.Connection) -> None:
        result = tools.get_chat(populated_db, "  ")
        assert "error" in result

    def test_messages_are_in_chronological_order(
        self, populated_db: sqlite3.Connection, conversation: Conversation
    ) -> None:
        result = tools.get_chat(populated_db, conversation.uuid)
        timestamps = [m["created_at"] for m in result["messages"]]
        assert timestamps == sorted(timestamps)


class TestGetChatPagination:
    @pytest.fixture
    def long_chat(self, db: sqlite3.Connection) -> str:
        """Crea una conversación con 50 mensajes para testear pagination."""
        conv = Conversation(
            uuid="long-chat",
            title="Chat largo",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        for i in range(50):
            msg = Message(
                uuid=f"msg-{i:03d}",
                conversation_uuid=conv.uuid,
                sender=Sender.HUMAN if i % 2 == 0 else Sender.ASSISTANT,
                text=f"mensaje número {i}",
                created_at=datetime(2026, 5, 1, 10, i, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 10, i, tzinfo=UTC),
            )
            repo.insert_message(db, msg)
        return conv.uuid

    def test_default_returns_first_10(self, db: sqlite3.Connection, long_chat: str) -> None:
        result = tools.get_chat(db, long_chat)
        assert result["total_messages"] == 50
        assert result["messages_returned"] == 10
        assert result["messages_offset"] == 0
        assert result["truncated"] is True
        assert result["messages"][0]["uuid"] == "msg-000"
        assert result["messages"][-1]["uuid"] == "msg-009"

    def test_offset_paginates(self, db: sqlite3.Connection, long_chat: str) -> None:
        result = tools.get_chat(db, long_chat, messages_limit=20, messages_offset=20)
        assert result["messages_offset"] == 20
        assert result["messages_returned"] == 20
        assert result["truncated"] is True
        assert result["messages"][0]["uuid"] == "msg-020"
        assert result["messages"][-1]["uuid"] == "msg-039"

    def test_last_page_truncated_false(self, db: sqlite3.Connection, long_chat: str) -> None:
        result = tools.get_chat(db, long_chat, messages_limit=20, messages_offset=40)
        assert result["messages_returned"] == 10
        assert result["truncated"] is False

    def test_limit_clamped_to_max(self, db: sqlite3.Connection, long_chat: str) -> None:
        result = tools.get_chat(db, long_chat, messages_limit=9999)
        assert result["messages_returned"] <= 100

    def test_limit_clamped_to_min(self, db: sqlite3.Connection, long_chat: str) -> None:
        result = tools.get_chat(db, long_chat, messages_limit=0)
        assert result["messages_returned"] >= 1

    def test_offset_beyond_total_returns_empty(
        self, db: sqlite3.Connection, long_chat: str
    ) -> None:
        """Si el offset es >= total_messages, la ventana queda vacía pero el chat existe."""
        result = tools.get_chat(db, long_chat, messages_offset=1000)
        assert result["total_messages"] == 50
        assert result["messages_returned"] == 0
        assert result["truncated"] is False
        assert result["messages"] == []

    def test_message_text_truncated_when_too_long(self, db: sqlite3.Connection) -> None:
        conv = Conversation(
            uuid="truncate-test",
            title="Test",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        long_text = "x" * 5000  # más de GET_CHAT_MESSAGE_TEXT_MAX_CHARS (1500)
        msg = Message(
            uuid="m1",
            conversation_uuid=conv.uuid,
            sender=Sender.ASSISTANT,
            text=long_text,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_message(db, msg)
        result = tools.get_chat(db, conv.uuid)
        msg_out = result["messages"][0]
        assert len(msg_out["text"]) < 5000
        assert msg_out["text"].endswith("…[truncated]")


class TestListRecentChats:
    def test_empty_db_returns_empty_list(self, db: sqlite3.Connection) -> None:
        result = tools.list_recent_chats(db, limit=10)
        assert result["count"] == 0
        assert result["chats"] == []

    def test_ordering_by_updated_at_desc(self, db: sqlite3.Connection, project: Project) -> None:
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

    def test_long_summary_is_truncated(self, db: sqlite3.Connection) -> None:
        # Like search_chats/find_related: summaries can weigh 2-3k chars, and up
        # to 100 of them in one list response would blow the MCP client token cap.
        conv = Conversation(
            uuid="big-summary",
            title="Conv",
            summary="S" * 3000,
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        repo.insert_conversation(db, conv)
        result = tools.list_recent_chats(db, limit=10)
        summary = result["chats"][0]["summary"]
        assert len(summary) < 3000
        assert summary.endswith("…[truncated]")

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

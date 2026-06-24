"""Tests for multi-device sync (Phase 1): peer store, server endpoints, pull."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.sync import client
from memex.sync.peers import Peer, add_peer, get_peer, load_peers, remove_peer
from memex.transports import http_ingest

TOKEN = "sync-test-token"
EMBEDDER = FakeEmbedder(dim=768)


def _now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


def _populate_source(conn) -> None:
    """Seed a source DB with one conversation (2 messages, 2 chunks + vectors)."""
    now = _now()
    repo.insert_conversation(
        conn,
        Conversation(
            uuid="c1",
            title="Drag racing ideas",
            summary="A chat about the game.",
            source=Source.CONVERSATIONS,
            account_uuid="acct-1",
            created_at=now,
            updated_at=now,
            content_hash="hash-v1",
        ),
    )
    repo.insert_message(
        conn,
        Message(
            uuid="m1",
            conversation_uuid="c1",
            sender=Sender.HUMAN,
            text="how should the gearbox feel",
            created_at=now,
            updated_at=now,
        ),
    )
    repo.insert_message(
        conn,
        Message(
            uuid="m2",
            conversation_uuid="c1",
            parent_uuid="m1",
            sender=Sender.ASSISTANT,
            text="snappy with a short shift window",
            created_at=now,
            updated_at=now,
        ),
    )
    for text in ("how should the gearbox feel", "snappy with a short shift window"):
        repo.add_chunk(
            conn,
            Chunk(
                conversation_uuid="c1",
                message_uuid="m1",
                sender="human",
                text=text,
                char_start=0,
                char_end=len(text),
                created_at=now,
            ),
            EMBEDDER.embed_one(text),
        )


@pytest.fixture
def source_client() -> Iterator[TestClient]:
    """A TestClient over the http_ingest app backed by a populated source DB."""
    conn = connect_and_init(":memory:", check_same_thread=False)
    _populate_source(conn)

    orig_conn = http_ingest._conn
    orig_embedder = http_ingest._embedder
    orig_token = http_ingest._token
    http_ingest._conn = conn
    http_ingest._embedder = EMBEDDER
    http_ingest._token = TOKEN
    try:
        with TestClient(http_ingest.app, base_url="http://127.0.0.1") as c:
            yield c
    finally:
        http_ingest._conn = orig_conn
        http_ingest._embedder = orig_embedder
        http_ingest._token = orig_token
        conn.close()


def _transport(source_client: TestClient):
    """Bind the sync client's HTTP hooks to a TestClient on the source app."""

    def manifest_fn(_peer: Peer) -> dict:
        r = source_client.get("/sync/manifest", headers={"X-Memex-Token": TOKEN})
        assert r.status_code == 200, r.text
        return r.json()

    def fetch_fn(_peer: Peer, uuids: list[str]) -> dict:
        r = source_client.post(
            "/sync/conversations",
            json={"uuids": uuids},
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 200, r.text
        return r.json()

    return manifest_fn, fetch_fn


PEER = Peer(name="src", url="http://127.0.0.1:5777", token=TOKEN)


# --------------------------------------------------------------------------
# Peer store
# --------------------------------------------------------------------------


class TestPeerStore:
    def test_round_trip(self, tmp_path) -> None:
        path = tmp_path / "peers.json"
        add_peer(Peer(name="mac", url="http://100.1.2.3:5777", token="t1"), path)
        add_peer(Peer(name="linux", url="http://100.4.5.6:5777/", token="t2"), path)
        peers = load_peers(path)
        assert {p.name for p in peers} == {"mac", "linux"}
        # Trailing slash is normalized away.
        assert get_peer("linux", path).url == "http://100.4.5.6:5777"

    def test_add_is_keyed_by_name(self, tmp_path) -> None:
        path = tmp_path / "peers.json"
        add_peer(Peer(name="mac", url="http://a:5777", token="t1"), path)
        add_peer(Peer(name="mac", url="http://b:5777", token="t2"), path)
        peers = load_peers(path)
        assert len(peers) == 1
        assert peers[0].url == "http://b:5777"
        assert peers[0].token == "t2"

    def test_remove(self, tmp_path) -> None:
        path = tmp_path / "peers.json"
        add_peer(Peer(name="mac", url="http://a:5777", token="t"), path)
        assert remove_peer("mac", path) is True
        assert load_peers(path) == []
        assert remove_peer("mac", path) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
    def test_file_is_user_only(self, tmp_path) -> None:
        path = tmp_path / "peers.json"
        add_peer(Peer(name="mac", url="http://a:5777", token="secret"), path)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_url_must_be_http(self) -> None:
        with pytest.raises(ValueError):
            Peer(name="x", url="ftp://nope", token="t")

    def test_missing_file_is_empty(self, tmp_path) -> None:
        assert load_peers(tmp_path / "nope.json") == []


# --------------------------------------------------------------------------
# Server endpoints (auth + shape)
# --------------------------------------------------------------------------


class TestSyncEndpointsAuth:
    def test_manifest_requires_token(self, source_client: TestClient) -> None:
        assert source_client.get("/sync/manifest").status_code == 401

    def test_manifest_rejects_wrong_token(self, source_client: TestClient) -> None:
        r = source_client.get("/sync/manifest", headers={"X-Memex-Token": "wrong"})
        assert r.status_code == 401

    def test_conversations_requires_token(self, source_client: TestClient) -> None:
        r = source_client.post("/sync/conversations", json={"uuids": ["c1"]})
        assert r.status_code == 401

    def test_endpoints_do_not_require_origin(self, source_client: TestClient) -> None:
        # No Origin header (a peer is not a browser): token alone authorizes.
        r = source_client.get("/sync/manifest", headers={"X-Memex-Token": TOKEN})
        assert r.status_code == 200


class TestSyncEndpointShape:
    def test_manifest_lists_conversations(self, source_client: TestClient) -> None:
        r = source_client.get("/sync/manifest", headers={"X-Memex-Token": TOKEN})
        body = r.json()
        assert body["embed_model"] == "fake"
        assert body["embed_dim"] == 768
        assert len(body["conversations"]) == 1
        entry = body["conversations"][0]
        assert entry["uuid"] == "c1"
        assert entry["content_hash"] == "hash-v1"
        assert entry["source"] == "conversations"

    def test_conversations_returns_full_record_with_vectors(
        self, source_client: TestClient
    ) -> None:
        r = source_client.post(
            "/sync/conversations",
            json={"uuids": ["c1"]},
            headers={"X-Memex-Token": TOKEN},
        )
        body = r.json()
        assert body["embed_model"] == "fake"
        assert len(body["conversations"]) == 1
        rec = body["conversations"][0]
        assert rec["uuid"] == "c1"
        assert len(rec["messages"]) == 2
        assert len(rec["chunks"]) == 2
        for chunk in rec["chunks"]:
            assert len(chunk["embedding"]) == 768

    def test_conversations_skips_unknown_uuid(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/conversations",
            json={"uuids": ["c1", "does-not-exist"]},
            headers={"X-Memex-Token": TOKEN},
        )
        assert len(r.json()["conversations"]) == 1

    def test_conversations_rejects_oversized_list(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/conversations",
            json={"uuids": [f"u{i}" for i in range(http_ingest._MAX_SYNC_UUIDS + 1)]},
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 413


# --------------------------------------------------------------------------
# Client pull (end to end against the source app)
# --------------------------------------------------------------------------


class TestPull:
    def _dest(self):
        return connect_and_init(":memory:", check_same_thread=False)

    def test_pull_inserts_conversation_with_vectors(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        summary = client.pull(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
        )
        assert summary.manifest_total == 1
        assert summary.to_fetch == 1
        assert summary.inserted == 1
        assert summary.failed == 0

        conv = repo.get_conversation(dest, "c1")
        assert conv is not None
        assert conv.content_hash == "hash-v1"
        assert len(repo.get_messages_for_conversation(dest, "c1")) == 2
        # chunks / vec / fts stay consistent.
        n_chunks = dest.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
        n_vec = dest.execute("SELECT count(*) AS n FROM vec_chunks").fetchone()["n"]
        n_fts = dest.execute("SELECT count(*) AS n FROM fts_chunks").fetchone()["n"]
        assert n_chunks == n_vec == n_fts == 2
        dest.close()

    def test_pulled_conversation_is_searchable(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        client.pull(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
        )
        hits = repo.vector_search(dest, EMBEDDER.embed_one("gearbox feel"), limit=5)
        assert any(h.conversation.uuid == "c1" for h in hits)
        dest.close()

    def test_repull_is_noop(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        kwargs = dict(local_model="fake", local_dim=768, manifest_fn=manifest_fn, fetch_fn=fetch_fn)
        client.pull(dest, PEER, **kwargs)
        again = client.pull(dest, PEER, **kwargs)
        assert again.to_fetch == 0
        assert again.inserted == 0
        # No duplicate chunks accumulated.
        assert dest.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 2
        dest.close()

    def test_changed_content_hash_refetches_and_replaces(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        kwargs = dict(local_model="fake", local_dim=768, manifest_fn=manifest_fn, fetch_fn=fetch_fn)
        client.pull(dest, PEER, **kwargs)

        # Mutate the source conversation (new hash, new chunk count).
        assert http_ingest._conn is not None
        src = http_ingest._conn
        repo.delete_chunks_for_conversation(src, "c1")
        src.execute("UPDATE conversations SET content_hash='hash-v2' WHERE uuid='c1'")
        repo.add_chunk(
            src,
            Chunk(
                conversation_uuid="c1",
                message_uuid="m1",
                sender="human",
                text="now a single chunk",
                char_start=0,
                char_end=18,
                created_at=_now(),
            ),
            EMBEDDER.embed_one("now a single chunk"),
        )
        src.commit()

        again = client.pull(dest, PEER, **kwargs)
        assert again.to_fetch == 1
        assert again.inserted == 1
        assert repo.get_conversation(dest, "c1").content_hash == "hash-v2"
        # Old chunks replaced, not appended.
        assert dest.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 1
        assert dest.execute("SELECT count(*) AS n FROM vec_chunks").fetchone()["n"] == 1
        dest.close()

    def test_model_mismatch_is_refused(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        summary = client.pull(
            dest,
            PEER,
            local_model="some-other-model",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
        )
        assert summary.refused_mismatch is True
        assert summary.inserted == 0
        assert repo.get_conversation(dest, "c1") is None
        dest.close()

    def test_design_chat_with_project_syncs(self, source_client: TestClient) -> None:
        # Add a Project + a design_chat referencing it to the source.
        src = http_ingest._conn
        assert src is not None
        now = _now()
        repo.insert_project(
            src,
            Project(
                uuid="p1",
                name="Diolumen",
                description="A project about interpretability.",
                created_at=now,
                updated_at=now,
            ),
        )
        repo.insert_conversation(
            src,
            Conversation(
                uuid="d1",
                title="Project chat",
                source=Source.DESIGN_CHAT,
                project_uuid="p1",
                created_at=now,
                updated_at=now,
                content_hash="d-hash",
            ),
        )
        repo.add_chunk(
            src,
            Chunk(
                conversation_uuid="d1",
                text="a design chat chunk",
                char_start=0,
                char_end=19,
                created_at=now,
            ),
            EMBEDDER.embed_one("a design chat chunk"),
        )
        src.commit()

        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        client.pull(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
        )
        # The project travelled, so the FK is satisfied and the link is preserved.
        assert repo.get_project(dest, "p1") is not None
        conv = repo.get_conversation(dest, "d1")
        assert conv is not None
        assert conv.project_uuid == "p1"
        dest.close()

    def test_missing_project_degrades_to_null_link(self) -> None:
        # A record that references a project but does not ship it must still
        # insert (FK would otherwise fail), dropping the project link.
        dest = self._dest()
        now_iso = _now().isoformat()
        record = {
            "uuid": "d2",
            "title": "Orphan project chat",
            "summary": None,
            "source": "design_chat",
            "project_uuid": "ghost",
            "project": None,
            "account_uuid": None,
            "created_at": now_iso,
            "updated_at": now_iso,
            "content_hash": "h",
            "messages": [],
            "chunks": [],
        }
        client._insert_record(dest, record, 768)
        conv = repo.get_conversation(dest, "d2")
        assert conv is not None
        assert conv.project_uuid is None
        dest.close()

    def test_dim_mismatch_is_refused(self, source_client: TestClient) -> None:
        dest = self._dest()
        manifest_fn, fetch_fn = _transport(source_client)
        summary = client.pull(
            dest,
            PEER,
            local_model="fake",
            local_dim=384,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
        )
        assert summary.refused_mismatch is True
        assert repo.get_conversation(dest, "c1") is None
        dest.close()

"""Tests for multi-device sync: peer store, server endpoints, pull/push/reconcile,
the master enable gate, status, conflict forks, and the hardening guards."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from memex.cli.sync import sync_app
from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.sync import client, records
from memex.sync import state as sync_state
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
    orig_gate = http_ingest._sync_enabled_override
    http_ingest._conn = conn
    http_ingest._embedder = EMBEDDER
    http_ingest._token = TOKEN
    # Enable sync via the in-memory override so the endpoints serve (the gate is
    # off by default and reads a per-user file we must not touch in tests).
    http_ingest._sync_enabled_override = True
    try:
        with TestClient(http_ingest.app, base_url="http://127.0.0.1") as c:
            yield c
    finally:
        http_ingest._conn = orig_conn
        http_ingest._embedder = orig_embedder
        http_ingest._token = orig_token
        http_ingest._sync_enabled_override = orig_gate
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


def _push_fn(source_client: TestClient):
    """Bind the sync client's push hook to a TestClient on the source app."""

    def push_fn(_peer: Peer, model: str, dim: int, conversations: list[dict]) -> dict:
        r = source_client.post(
            "/sync/push",
            json={"embed_model": model, "embed_dim": dim, "conversations": conversations},
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 200, r.text
        return r.json()

    return push_fn


PEER = Peer(name="src", url="http://127.0.0.1:5777", token=TOKEN)


def _make_conv(uuid: str, content_hash: str, updated_at: datetime, text: str = "local chunk"):
    """Build a (conversation, message, chunk-text) triple for seeding a dest DB."""
    conv = Conversation(
        uuid=uuid,
        title=f"conv {uuid}",
        source=Source.CONVERSATIONS,
        created_at=_now(),
        updated_at=updated_at,
        content_hash=content_hash,
    )
    msg = Message(
        uuid=f"{uuid}-m",
        conversation_uuid=uuid,
        sender=Sender.HUMAN,
        text=text,
        created_at=_now(),
        updated_at=updated_at,
    )
    return conv, msg, text


def _seed(
    conn, uuid: str, content_hash: str, updated_at: datetime, text: str = "local chunk"
) -> None:
    conv, msg, chunk_text = _make_conv(uuid, content_hash, updated_at, text)
    repo.insert_conversation(conn, conv)
    repo.insert_message(conn, msg)
    repo.add_chunk(
        conn,
        Chunk(
            conversation_uuid=uuid,
            message_uuid=msg.uuid,
            text=chunk_text,
            char_start=0,
            char_end=len(chunk_text),
            created_at=_now(),
        ),
        EMBEDDER.embed_one(chunk_text),
    )


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


# --------------------------------------------------------------------------
# /sync/push endpoint
# --------------------------------------------------------------------------


def _record(uuid: str, content_hash: str, updated_at: datetime) -> dict:
    iso = updated_at.isoformat()
    text = "pushed chunk"
    return {
        "uuid": uuid,
        "title": f"conv {uuid}",
        "summary": None,
        "source": "conversations",
        "project_uuid": None,
        "project": None,
        "account_uuid": None,
        "created_at": _now().isoformat(),
        "updated_at": iso,
        "content_hash": content_hash,
        "messages": [],
        "chunks": [
            {
                "message_uuid": None,
                "sender": "human",
                "text": text,
                "char_start": 0,
                "char_end": len(text),
                "created_at": _now().isoformat(),
                "embedding": EMBEDDER.embed_one(text),
            }
        ],
    }


class TestSyncPushEndpoint:
    def test_push_requires_token(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/push", json={"embed_model": "fake", "embed_dim": 768, "conversations": []}
        )
        assert r.status_code == 401

    def test_push_inserts_record(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/push",
            json={
                "embed_model": "fake",
                "embed_dim": 768,
                "conversations": [_record("pushed-1", "ph", _now())],
            },
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 200
        assert r.json() == {"inserted": 1, "failed": 0}
        assert repo.get_conversation(http_ingest._conn, "pushed-1") is not None

    def test_push_rejects_model_mismatch(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/push",
            json={
                "embed_model": "other-model",
                "embed_dim": 768,
                "conversations": [_record("pushed-x", "ph", _now())],
            },
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 409
        assert repo.get_conversation(http_ingest._conn, "pushed-x") is None

    def test_push_rejects_oversized_list(self, source_client: TestClient) -> None:
        r = source_client.post(
            "/sync/push",
            json={
                "embed_model": "fake",
                "embed_dim": 768,
                "conversations": [{} for _ in range(http_ingest._MAX_SYNC_UUIDS + 1)],
            },
            headers={"X-Memex-Token": TOKEN},
        )
        assert r.status_code == 413


# --------------------------------------------------------------------------
# Client push + bidirectional reconcile
# --------------------------------------------------------------------------


class TestPushAndReconcile:
    def _dest(self):
        return connect_and_init(":memory:", check_same_thread=False)

    def test_push_sends_local_only_conversations(self, source_client: TestClient) -> None:
        dest = self._dest()
        _seed(dest, "local-1", "h", _now())  # source does not have this
        manifest_fn, _ = _transport(source_client)
        push_fn = _push_fn(source_client)
        summary = client.push(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            push_fn=push_fn,
        )
        assert summary.to_push == 1
        assert summary.pushed == 1
        # The source (peer) now has the local conversation.
        assert repo.get_conversation(http_ingest._conn, "local-1") is not None
        dest.close()

    def test_reconcile_makes_both_equal(self, source_client: TestClient) -> None:
        dest = self._dest()
        _seed(dest, "only-on-dest", "hd", _now())  # source lacks this
        manifest_fn, fetch_fn = _transport(source_client)
        push_fn = _push_fn(source_client)
        summary = client.reconcile(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
            push_fn=push_fn,
        )
        # Pulled the source's c1, pushed dest's only-on-dest.
        assert summary.pulled == 1
        assert summary.pushed == 1
        src = http_ingest._conn
        # Both DBs now hold both conversations.
        for uuid in ("c1", "only-on-dest"):
            assert repo.get_conversation(dest, uuid) is not None
            assert repo.get_conversation(src, uuid) is not None
        dest.close()

    def test_reconcile_is_idempotent(self, source_client: TestClient) -> None:
        dest = self._dest()
        _seed(dest, "only-on-dest", "hd", _now())
        manifest_fn, fetch_fn = _transport(source_client)
        push_fn = _push_fn(source_client)
        kwargs = dict(
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
            push_fn=push_fn,
        )
        client.reconcile(dest, PEER, **kwargs)
        again = client.reconcile(dest, PEER, **kwargs)
        assert again.pulled == 0
        assert again.pushed == 0
        dest.close()

    def test_reconcile_newer_wins_no_overwrite_with_older(self, source_client: TestClient) -> None:
        # Same uuid on both, dest strictly newer: reconcile must push dest's
        # version to the source, never pull the source's older one.
        dest = self._dest()
        old = datetime(2026, 6, 1, tzinfo=UTC)
        new = datetime(2026, 6, 24, tzinfo=UTC)
        src = http_ingest._conn
        # Source has c1 already (updated _now() = 2026-06-24 12:00). Give it an
        # OLDER divergent copy of a shared uuid; dest gets the NEWER copy.
        _seed(src, "shared", "src-old", old)
        src.commit()
        _seed(dest, "shared", "dest-new", new)

        manifest_fn, fetch_fn = _transport(source_client)
        push_fn = _push_fn(source_client)
        client.reconcile(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
            push_fn=push_fn,
        )
        # Dest kept its newer version; source took dest's newer version.
        assert repo.get_conversation(dest, "shared").content_hash == "dest-new"
        assert repo.get_conversation(src, "shared").content_hash == "dest-new"
        dest.close()


# --------------------------------------------------------------------------
# Auto-sync wrapper
# --------------------------------------------------------------------------


class TestAutoSync:
    @pytest.fixture(autouse=True)
    def _enable_and_isolate(self, monkeypatch) -> None:
        # Enable the gate (off by default) and keep record_sync off the real
        # per-user history file.
        monkeypatch.setattr(http_ingest, "_sync_enabled_override", True)
        monkeypatch.setattr(http_ingest.sync_state, "record_sync", lambda *a, **k: None)

    def test_skips_when_disabled(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(http_ingest, "_sync_enabled_override", False)
        monkeypatch.setattr(http_ingest.peers, "load_peers", lambda: [PEER])
        called: list[str] = []
        monkeypatch.setattr(http_ingest.client, "reconcile", lambda *a, **k: called.append("x"))
        http_ingest._auto_sync_once(db_path=tmp_path / "memex.db")
        assert called == []  # the master gate is off

    def test_skips_when_ingest_lock_busy(self, tmp_path, monkeypatch) -> None:
        from memex import ingest_lock

        db = tmp_path / "memex.db"
        monkeypatch.setattr(http_ingest.peers, "load_peers", lambda: [PEER])
        called: list[str] = []
        monkeypatch.setattr(
            http_ingest.client,
            "reconcile",
            lambda *a, **k: called.append("reconciled"),
        )
        handle = ingest_lock.acquire_nonblocking(db)  # hold the lock
        try:
            http_ingest._auto_sync_once(db_path=db)
        finally:
            ingest_lock.release(handle)
        assert called == []  # skipped because an ingest holds the lock

    def test_reconciles_each_peer_when_free(self, tmp_path, monkeypatch) -> None:
        db = tmp_path / "memex.db"
        p2 = Peer(name="src2", url="http://127.0.0.1:5778", token="t2")
        monkeypatch.setattr(http_ingest.peers, "load_peers", lambda: [PEER, p2])
        seen: list[str] = []

        def fake_reconcile(conn, peer, **kwargs):
            seen.append(peer.name)
            return client.ReconcileSummary(peer=peer.name, pulled=0, pushed=0, failed=0)

        monkeypatch.setattr(http_ingest.client, "reconcile", fake_reconcile)
        http_ingest._auto_sync_once(db_path=db)
        assert seen == ["src", "src2"]

    def test_no_peers_is_a_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(http_ingest.peers, "load_peers", lambda: [])
        called: list[str] = []
        monkeypatch.setattr(http_ingest.client, "reconcile", lambda *a, **k: called.append("x"))
        http_ingest._auto_sync_once(db_path=tmp_path / "memex.db")
        assert called == []


# --------------------------------------------------------------------------
# Phase 3: master gate state
# --------------------------------------------------------------------------


class TestSyncState:
    def test_default_disabled(self, tmp_path) -> None:
        assert sync_state.is_enabled(tmp_path / "sync_state.json") is False

    def test_enable_disable_roundtrip(self, tmp_path) -> None:
        p = tmp_path / "sync_state.json"
        sync_state.set_enabled(True, p)
        assert sync_state.is_enabled(p) is True
        sync_state.set_enabled(False, p)
        assert sync_state.is_enabled(p) is False

    def test_record_and_get_history(self, tmp_path) -> None:
        h = tmp_path / "sync_history.json"
        when = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
        sync_state.record_sync("mac", pulled=2, pushed=1, failed=0, when=when, path=h)
        entry = sync_state.get_peer_history("mac", h)
        assert entry is not None
        assert entry["pulled"] == 2
        assert entry["pushed"] == 1
        assert entry["last_sync_at"].startswith("2026-06-24")
        assert sync_state.get_peer_history("absent", h) is None

    def test_history_does_not_clobber_gate(self, tmp_path) -> None:
        # The gate and the history are separate files on purpose, so a frequent
        # history write can never race-clobber the enabled flag.
        state_p = tmp_path / "sync_state.json"
        hist_p = tmp_path / "sync_history.json"
        sync_state.set_enabled(True, state_p)
        sync_state.record_sync("mac", pulled=1, pushed=0, failed=0, path=hist_p)
        assert sync_state.is_enabled(state_p) is True

    def test_corrupt_state_is_fail_closed(self, tmp_path) -> None:
        p = tmp_path / "sync_state.json"
        p.write_text("{ not valid json", encoding="utf-8")
        assert sync_state.is_enabled(p) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
    def test_state_file_is_0600(self, tmp_path) -> None:
        p = tmp_path / "sync_state.json"
        sync_state.set_enabled(True, p)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# Phase 3: the gate hides the endpoints when sync is off
# --------------------------------------------------------------------------


class TestSyncGateEndpoints:
    def test_endpoints_404_when_disabled(self, source_client, monkeypatch) -> None:
        monkeypatch.setattr(http_ingest, "_sync_enabled_override", False)
        h = {"X-Memex-Token": TOKEN}
        assert source_client.get("/sync/manifest", headers=h).status_code == 404
        assert (
            source_client.post("/sync/conversations", json={"uuids": []}, headers=h).status_code
            == 404
        )
        assert (
            source_client.post("/sync/push", json={"conversations": []}, headers=h).status_code
            == 404
        )

    def test_disabled_404s_before_the_token_check(self, source_client, monkeypatch) -> None:
        # A disabled device returns 404 even to a bad/absent token, so it does not
        # reveal (via a 401) that the endpoint exists.
        monkeypatch.setattr(http_ingest, "_sync_enabled_override", False)
        assert source_client.get("/sync/manifest").status_code == 404
        assert (
            source_client.get("/sync/manifest", headers={"X-Memex-Token": "wrong"}).status_code
            == 404
        )

    def test_health_still_works_when_disabled(self, source_client, monkeypatch) -> None:
        monkeypatch.setattr(http_ingest, "_sync_enabled_override", False)
        assert source_client.get("/health").status_code == 200


# --------------------------------------------------------------------------
# Phase 3: conflict forks + cross-device dedup
# --------------------------------------------------------------------------


class TestReconcileForks:
    def test_select_reconcile_flags_same_timestamp_fork(self) -> None:
        t = "2026-06-24T12:00:00+00:00"
        local = [{"uuid": "c", "content_hash": "a", "updated_at": t, "source": "conversations"}]
        remote = [{"uuid": "c", "content_hash": "b", "updated_at": t, "source": "conversations"}]
        to_pull, to_push, forks = records.select_reconcile(local, remote)
        assert to_pull == []
        assert to_push == []
        assert forks == ["c"]

    def test_reconcile_reports_fork_and_overwrites_neither(self, source_client) -> None:
        # Same uuid on both, SAME updated_at, different content: a fork. Neither
        # side is overwritten and the summary reports it.
        dest = connect_and_init(":memory:", check_same_thread=False)
        same = _now()
        src = http_ingest._conn
        _seed(src, "forked", "src-content", same)
        src.commit()
        _seed(dest, "forked", "dest-content", same)
        manifest_fn, fetch_fn = _transport(source_client)
        push_fn = _push_fn(source_client)
        summary = client.reconcile(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
            push_fn=push_fn,
        )
        assert summary.forks == 1
        # Each side keeps its own divergent copy.
        assert repo.get_conversation(dest, "forked").content_hash == "dest-content"
        assert repo.get_conversation(src, "forked").content_hash == "src-content"
        dest.close()


class TestSyncDedup:
    def test_reconcile_keeps_one_row_per_uuid(self, source_client) -> None:
        # dest already holds an identical copy of the source's c1 (same uuid +
        # content_hash). Reconcile must be a no-op for it: one conversation row,
        # never a duplicate, so a cross-device search returns it once.
        dest = connect_and_init(":memory:", check_same_thread=False)
        _seed(dest, "c1", "hash-v1", _now())  # matches the source's c1 content_hash
        manifest_fn, fetch_fn = _transport(source_client)
        push_fn = _push_fn(source_client)
        summary = client.reconcile(
            dest,
            PEER,
            local_model="fake",
            local_dim=768,
            manifest_fn=manifest_fn,
            fetch_fn=fetch_fn,
            push_fn=push_fn,
        )
        n = dest.execute("SELECT COUNT(*) AS n FROM conversations WHERE uuid = 'c1'").fetchone()[
            "n"
        ]
        assert n == 1
        assert summary.pulled == 0  # identical copy, nothing to pull
        dest.close()


# --------------------------------------------------------------------------
# Phase 3: insert_record hardening (red-team)
# --------------------------------------------------------------------------


class TestInsertRecordHardening:
    def _conn(self):
        return connect_and_init(":memory:", check_same_thread=False)

    def test_rejects_too_many_chunks(self, monkeypatch) -> None:
        conn = self._conn()
        monkeypatch.setattr(records.settings, "max_chunks_per_conversation", 2)
        rec = _record("big", "h", _now())
        rec["chunks"] = [
            {
                "message_uuid": None,
                "sender": "human",
                "text": f"c{i}",
                "char_start": 0,
                "char_end": 2,
                "created_at": _now().isoformat(),
                "embedding": [0.0] * 768,
            }
            for i in range(3)
        ]
        with pytest.raises(ValueError, match="chunks"):
            records.insert_record(conn, rec, 768)
        conn.close()

    def test_rejects_non_list_chunks(self) -> None:
        conn = self._conn()
        rec = _record("x", "h", _now())
        rec["chunks"] = "not-a-list"
        with pytest.raises(ValueError, match="lists"):
            records.insert_record(conn, rec, 768)
        conn.close()

    def test_rejects_non_list_messages(self) -> None:
        conn = self._conn()
        rec = _record("y", "h", _now())
        rec["messages"] = "nope"
        with pytest.raises(ValueError, match="lists"):
            records.insert_record(conn, rec, 768)
        conn.close()


# --------------------------------------------------------------------------
# Phase 3: CLI enable / disable / status + the data-command gate
# --------------------------------------------------------------------------


class TestSyncCLI:
    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        # Point the gate/peer files under tmp and avoid loading a real embedder.
        monkeypatch.setattr(sync_state.settings, "db_path", tmp_path / "memex.db")
        monkeypatch.setattr("memex.cli.sync.get_default_embedder", lambda: EMBEDDER)
        return tmp_path

    def test_status_default_disabled(self, isolated) -> None:
        result = CliRunner().invoke(sync_app, ["status"])
        assert result.exit_code == 0
        assert "disabled" in result.output

    def test_enable_then_status_enabled(self, isolated) -> None:
        assert CliRunner().invoke(sync_app, ["enable"]).exit_code == 0
        assert sync_state.is_enabled() is True
        result = CliRunner().invoke(sync_app, ["status"])
        assert "enabled" in result.output

    def test_disable_turns_it_off(self, isolated) -> None:
        CliRunner().invoke(sync_app, ["enable"])
        assert CliRunner().invoke(sync_app, ["disable"]).exit_code == 0
        assert sync_state.is_enabled() is False

    def test_pull_refused_when_disabled(self, isolated) -> None:
        result = CliRunner().invoke(sync_app, ["pull"])
        assert result.exit_code == 2
        assert "disabled" in result.output

    def test_reconcile_refused_when_disabled(self, isolated) -> None:
        result = CliRunner().invoke(sync_app, ["reconcile"])
        assert result.exit_code == 2
        assert "disabled" in result.output

    def test_corrupt_state_file_keeps_status_working(self, isolated) -> None:
        # A garbage gate file is fail-closed, and `status` still runs.
        (isolated / "sync_state.json").write_text("{bad", encoding="utf-8")
        result = CliRunner().invoke(sync_app, ["status"])
        assert result.exit_code == 0
        assert "disabled" in result.output

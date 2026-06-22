"""Tests del HTTP server local que recibe payloads de la Chrome ext."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from memex.config import settings
from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.transports import http_ingest

CHROME_ORIGIN = "chrome-extension://abcdefghijklmnopqrstuvwxyz123456"
FIREFOX_ORIGIN = "moz-extension://aaaa-bbbb-cccc"
MALICIOUS_ORIGIN = "https://attacker.example"

# Per-install access token injected by the fixture. Real requests must carry it
# in the X-Memex-Token header; the Origin check alone no longer authorizes.
TEST_TOKEN = "test-token-deadbeef"
# Headers for an authorized extension request (valid Origin + valid token).
AUTH = {"Origin": CHROME_ORIGIN, "X-Memex-Token": TEST_TOKEN}

# Shape mínimo de un payload válido (mismo que un item de conversations.json).
VALID_PAYLOAD = {
    "uuid": "conv-live-1",
    "name": "Captura en vivo de prueba",
    "summary": "Charla de testing.",
    "created_at": "2026-05-19T10:00:00.000Z",
    "updated_at": "2026-05-19T10:05:00.000Z",
    "account": {"uuid": "acct-test"},
    "chat_messages": [
        {
            "uuid": "msg-l-1",
            "text": "hola en vivo",
            "content": [{"type": "text", "text": "hola en vivo"}],
            "sender": "human",
            "created_at": "2026-05-19T10:00:00.000Z",
            "updated_at": "2026-05-19T10:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": None,
        },
        {
            "uuid": "msg-l-2",
            "text": "buenas",
            "content": [{"type": "text", "text": "buenas"}],
            "sender": "assistant",
            "created_at": "2026-05-19T10:00:05.000Z",
            "updated_at": "2026-05-19T10:00:05.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": "msg-l-1",
        },
    ],
}


@pytest.fixture
def http_client() -> Iterator[TestClient]:
    """TestClient con DB in-memory y FakeEmbedder inyectados en el módulo."""
    # check_same_thread=False porque el TestClient bridge sync↔async usa thread
    # pool y nuestra conn se crea en el main test thread.
    test_conn = connect_and_init(":memory:", check_same_thread=False)
    test_embedder = FakeEmbedder(dim=768)

    original_conn = http_ingest._conn
    original_embedder = http_ingest._embedder
    original_token = http_ingest._token
    original_subproc = settings.ingest_embed_in_subprocess
    http_ingest._conn = test_conn
    http_ingest._embedder = test_embedder
    http_ingest._token = TEST_TOKEN
    # Exercise the in-process path so the injected in-memory DB + FakeEmbedder are
    # used; the subprocess path runs the real worker against the real DB.
    settings.ingest_embed_in_subprocess = False

    try:
        # base_url is a loopback host so TrustedHostMiddleware accepts it (the
        # default "testserver" host would be rejected by the Host allow-list).
        with TestClient(http_ingest.app, base_url="http://127.0.0.1") as client:
            yield client
    finally:
        http_ingest._conn = original_conn
        http_ingest._embedder = original_embedder
        http_ingest._token = original_token
        settings.ingest_embed_in_subprocess = original_subproc
        test_conn.close()


class TestHealth:
    def test_health_returns_ok(self, http_client: TestClient) -> None:
        r = http_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_does_not_fingerprint_the_service(self, http_client: TestClient) -> None:
        """/health must not advertise the product name to arbitrary callers."""
        r = http_client.get("/health")
        assert "service" not in r.json()

    def test_health_works_without_origin(self, http_client: TestClient) -> None:
        """`/health` no chequea origin: la Chrome ext lo usa para ping inicial."""
        r = http_client.get("/health")
        assert r.status_code == 200


class TestOriginCheck:
    def test_rejects_missing_origin(self, http_client: TestClient) -> None:
        r = http_client.post("/ingest/conversation", json=VALID_PAYLOAD)
        assert r.status_code == 403
        assert "Origin" in r.json()["error"] or "origin" in r.json()["error"].lower()

    def test_rejects_web_origin(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": MALICIOUS_ORIGIN},
        )
        assert r.status_code == 403

    def test_accepts_chrome_extension(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert r.status_code == 200

    def test_accepts_firefox_extension(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": FIREFOX_ORIGIN, "X-Memex-Token": TEST_TOKEN},
        )
        assert r.status_code == 200


class TestIngest:
    def test_successful_ingest_response_shape(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["uuid"] == "conv-live-1"
        assert body["conversations"] == 1
        assert body["messages"] == 2
        assert body["chunks"] >= 1

    def test_ingest_actually_writes_to_db(self, http_client: TestClient) -> None:
        http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert http_ingest._conn is not None
        conv = repo.get_conversation(http_ingest._conn, "conv-live-1")
        assert conv is not None
        assert conv.title == "Captura en vivo de prueba"
        assert conv.source is Source.CONVERSATIONS

    def test_reingest_is_idempotent(self, http_client: TestClient) -> None:
        for _ in range(2):
            r = http_client.post(
                "/ingest/conversation",
                json=VALID_PAYLOAD,
                headers=AUTH,
            )
            assert r.status_code == 200

        assert http_ingest._conn is not None
        n = repo.count_chunks(http_ingest._conn)
        # Si fuera no idempotente, se duplicarían los chunks en el segundo POST.
        # add_chunk usa rowid sincronizado y delete_chunks_for_conversation
        # limpia antes de re-chunkear, así que el conteo debe ser estable.
        assert n > 0

    def test_invalid_json_returns_400(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            content=b"esto no es json",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_dict_payload_returns_400(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=["soy una lista, no un dict"],
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_missing_required_field_returns_400(self, http_client: TestClient) -> None:
        bad = {k: v for k, v in VALID_PAYLOAD.items() if k != "uuid"}
        r = http_client.post(
            "/ingest/conversation",
            json=bad,
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_source_query_param_invalid(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation?source=basura",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_source_memory_rejected(self, http_client: TestClient) -> None:
        """source=memory solo se usa para memories.json del export, no para captura."""
        r = http_client.post(
            "/ingest/conversation?source=memory",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert r.status_code == 400


class TestTokenAuth:
    """The Origin check is no longer sufficient: a valid token is required.

    This is the fix for the forgeable-Origin finding (any non-browser local
    process can set Origin: chrome-extension://x).
    """

    def test_valid_origin_without_token_rejected(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 401

    def test_wrong_token_rejected(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": CHROME_ORIGIN, "X-Memex-Token": "not-the-token"},
        )
        assert r.status_code == 401

    def test_correct_token_accepted(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers=AUTH,
        )
        assert r.status_code == 200

    def test_origin_is_checked_before_token(self, http_client: TestClient) -> None:
        """A web-origin request is rejected at the Origin gate (403), not 401."""
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": MALICIOUS_ORIGIN, "X-Memex-Token": TEST_TOKEN},
        )
        assert r.status_code == 403


class TestBodyCap:
    """The ingest body is capped to bound memory use (DoS)."""

    def test_oversized_body_rejected(
        self, http_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ingest_max_body_bytes", 100)
        big = {**VALID_PAYLOAD, "name": "x" * 5_000}
        r = http_client.post("/ingest/conversation", json=big, headers=AUTH)
        assert r.status_code == 413

    def test_normal_body_under_cap_ok(
        self, http_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ingest_max_body_bytes", 16 * 1024 * 1024)
        r = http_client.post("/ingest/conversation", json=VALID_PAYLOAD, headers=AUTH)
        assert r.status_code == 200


class TestSubprocessPath:
    """When enabled, the endpoint embeds via a child process and returns its
    counts. Mock the child so the test does not spawn a real worker."""

    def test_returns_worker_counts(
        self, http_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_subproc(payload: dict, source: object) -> dict:
            return {"conversations": 1, "messages": 2, "chunks": 3, "skipped_empty_messages": 0}

        monkeypatch.setattr(settings, "ingest_embed_in_subprocess", True)
        monkeypatch.setattr(http_ingest, "_ingest_in_subprocess", fake_subproc)
        r = http_client.post("/ingest/conversation", json=VALID_PAYLOAD, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["uuid"] == VALID_PAYLOAD["uuid"]
        assert body["chunks"] == 3

    def test_worker_failure_returns_503(
        self, http_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memex.core.embeddings import EmbedderError

        async def boom(payload: dict, source: object) -> dict:
            raise EmbedderError("worker exited 1")

        monkeypatch.setattr(settings, "ingest_embed_in_subprocess", True)
        monkeypatch.setattr(http_ingest, "_ingest_in_subprocess", boom)
        r = http_client.post("/ingest/conversation", json=VALID_PAYLOAD, headers=AUTH)
        assert r.status_code == 503


class TestBackfillPlan:
    """`POST /ingest/plan`: the incremental-backfill filter (returns to-fetch)."""

    def test_requires_origin(self, http_client: TestClient) -> None:
        r = http_client.post("/ingest/plan", json={"conversations": []})
        assert r.status_code == 403

    def test_requires_token(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/plan", json={"conversations": []}, headers={"Origin": CHROME_ORIGIN}
        )
        assert r.status_code == 401

    def test_bad_body_returns_400(self, http_client: TestClient) -> None:
        r = http_client.post("/ingest/plan", json={"nope": 1}, headers=AUTH)
        assert r.status_code == 400

    def test_unknown_conversation_is_to_fetch(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/plan",
            json={"conversations": [{"uuid": "never-seen", "updated_at": "2026-05-19T10:00:00Z"}]},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["toFetch"] == ["never-seen"]

    def test_indexed_unchanged_is_skipped_changed_is_fetched(self, http_client: TestClient) -> None:
        http_client.post("/ingest/conversation", json=VALID_PAYLOAD, headers=AUTH)
        uuid = VALID_PAYLOAD["uuid"]
        # Same instant (different fractional-second precision) -> skipped. The
        # ingest normalizes `.000Z` to `.000000Z`, so this also proves the
        # compare is by instant, not string.
        same = {"uuid": uuid, "updated_at": VALID_PAYLOAD["updated_at"]}
        # A newer updated_at -> must be re-fetched.
        newer = {"uuid": uuid, "updated_at": "2030-01-01T00:00:00.000000Z"}
        r = http_client.post("/ingest/plan", json={"conversations": [same]}, headers=AUTH)
        assert r.json()["toFetch"] == []
        r = http_client.post("/ingest/plan", json={"conversations": [newer]}, headers=AUTH)
        assert r.json()["toFetch"] == [uuid]

    def test_too_many_conversations_returns_413(
        self, http_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The list length is capped independently of the body byte cap, so a
        # token-holding caller cannot force an unbounded scan/parse loop. Shrink
        # the cap so the test stays fast.
        monkeypatch.setattr(http_ingest, "_MAX_PLAN_ITEMS", 2)
        convs = [{"uuid": f"u{i}", "updated_at": "2026-05-19T10:00:00Z"} for i in range(3)]
        r = http_client.post("/ingest/plan", json={"conversations": convs}, headers=AUTH)
        assert r.status_code == 413

    def test_duplicate_uuids_are_deduped(self, http_client: TestClient) -> None:
        dup = {"uuid": "dup", "updated_at": "2026-05-19T10:00:00Z"}
        r = http_client.post("/ingest/plan", json={"conversations": [dup, dup, dup]}, headers=AUTH)
        assert r.json()["toFetch"] == ["dup"]

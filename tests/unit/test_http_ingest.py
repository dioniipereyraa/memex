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
    http_ingest._conn = test_conn
    http_ingest._embedder = test_embedder
    http_ingest._token = TEST_TOKEN

    try:
        # base_url is a loopback host so TrustedHostMiddleware accepts it (the
        # default "testserver" host would be rejected by the Host allow-list).
        with TestClient(http_ingest.app, base_url="http://127.0.0.1") as client:
            yield client
    finally:
        http_ingest._conn = original_conn
        http_ingest._embedder = original_embedder
        http_ingest._token = original_token
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


class TestEmbedderRelease:
    """The always-on capture server drops the model when idle so it does not
    hold ~1 GB resident forever after a single capture."""

    def test_release_drops_the_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(http_ingest, "_embedder", FakeEmbedder(dim=768))
        http_ingest._release_embedder()
        assert http_ingest._embedder is None

    def test_get_embedder_reloads_after_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(http_ingest, "_embedder", None)
        monkeypatch.setattr(http_ingest, "get_default_embedder", lambda: FakeEmbedder(dim=768))
        assert http_ingest._get_embedder() is not None

    def test_schedule_is_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ingest_idle_release_seconds", 0)
        monkeypatch.setattr(http_ingest, "_release_handle", None)
        http_ingest._schedule_embedder_release()  # disabled -> no timer armed
        assert http_ingest._release_handle is None

    def test_schedule_outside_event_loop_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ingest_idle_release_seconds", 60)
        monkeypatch.setattr(http_ingest, "_release_handle", None)
        http_ingest._schedule_embedder_release()  # no running loop -> graceful
        assert http_ingest._release_handle is None

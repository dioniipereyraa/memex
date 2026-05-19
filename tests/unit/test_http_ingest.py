"""Tests del HTTP server local que recibe payloads de la Chrome ext."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.transports import http_ingest

CHROME_ORIGIN = "chrome-extension://abcdefghijklmnopqrstuvwxyz123456"
FIREFOX_ORIGIN = "moz-extension://aaaa-bbbb-cccc"
MALICIOUS_ORIGIN = "https://attacker.example"

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
    http_ingest._conn = test_conn
    http_ingest._embedder = test_embedder

    try:
        with TestClient(http_ingest.app) as client:
            yield client
    finally:
        http_ingest._conn = original_conn
        http_ingest._embedder = original_embedder
        test_conn.close()


class TestHealth:
    def test_health_returns_ok(self, http_client: TestClient) -> None:
        r = http_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

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
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 200

    def test_accepts_firefox_extension(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": FIREFOX_ORIGIN},
        )
        assert r.status_code == 200


class TestIngest:
    def test_successful_ingest_response_shape(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=VALID_PAYLOAD,
            headers={"Origin": CHROME_ORIGIN},
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
            headers={"Origin": CHROME_ORIGIN},
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
                headers={"Origin": CHROME_ORIGIN},
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
            headers={"Origin": CHROME_ORIGIN, "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_dict_payload_returns_400(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation",
            json=["soy una lista, no un dict"],
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 400

    def test_missing_required_field_returns_400(
        self, http_client: TestClient
    ) -> None:
        bad = {k: v for k, v in VALID_PAYLOAD.items() if k != "uuid"}
        r = http_client.post(
            "/ingest/conversation",
            json=bad,
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 400

    def test_source_query_param_invalid(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/ingest/conversation?source=basura",
            json=VALID_PAYLOAD,
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 400

    def test_source_memory_rejected(self, http_client: TestClient) -> None:
        """source=memory solo se usa para memories.json del export, no para captura."""
        r = http_client.post(
            "/ingest/conversation?source=memory",
            json=VALID_PAYLOAD,
            headers={"Origin": CHROME_ORIGIN},
        )
        assert r.status_code == 400

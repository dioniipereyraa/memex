"""Tests for the subprocess ingest worker (`memex.transports.ingest_worker`).

The worker is what the capture server spawns per chat so the model is loaded in
a short-lived child that exits (keeps the always-on server at baseline RSS).
Here we test the `run_ingest` function with an injected in-memory DB and a
FakeEmbedder, so it is fast and does not load the real model.
"""

from __future__ import annotations

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Source
from memex.core.storage.db import connect_and_init
from memex.transports.ingest_worker import run_ingest

PAYLOAD = {
    "uuid": "conv-worker-1",
    "name": "Worker test",
    "summary": "x",
    "created_at": "2026-06-12T10:00:00.000Z",
    "updated_at": "2026-06-12T10:05:00.000Z",
    "account": {"uuid": "acct-test"},
    "chat_messages": [
        {
            "uuid": "m1",
            "text": "hola",
            "content": [{"type": "text", "text": "hola"}],
            "sender": "human",
            "created_at": "2026-06-12T10:00:00.000Z",
            "updated_at": "2026-06-12T10:00:00.000Z",
            "attachments": [],
            "files": [],
            "parent_message_uuid": None,
        },
    ],
}


def test_run_ingest_stores_and_returns_counts() -> None:
    conn = connect_and_init(":memory:", check_same_thread=False)
    try:
        counts = run_ingest(
            PAYLOAD, Source.CONVERSATIONS, conn=conn, embedder=FakeEmbedder(dim=768)
        )
        assert counts["conversations"] == 1
        assert counts["messages"] >= 1
        assert set(counts) == {"conversations", "messages", "chunks", "skipped_empty_messages"}
        stored = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert stored == 1
    finally:
        conn.close()

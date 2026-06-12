"""Subprocess worker: ingest ONE conversation, then exit.

The always-on capture server (`memex serve`) must not hold the embedding model
(~0.5 GB: model weights + the onnxruntime arena) resident between captures.
Embedding in-process and dropping the model afterwards does NOT return the RSS
to the OS on macOS (the freed arena stays in the process heap), so the only
reliable way to reclaim is to embed in a short-lived process that exits.

So each captured conversation is ingested by spawning this module: it loads the
model, runs the full parse / chunk / embed / store for that one conversation,
prints the ingest summary as a single JSON line to stdout, and exits, letting
the OS reclaim all of its memory. The server itself stays at its baseline.

Protocol (kept dead simple so the parent can parse it):
- argv[1]: the `Source` value (e.g. `conversations`, `design_chat`).
- stdin: the conversation payload as JSON.
- stdout: exactly one JSON object with the summary counts.
- stderr: logs / warnings (so stdout carries only the result).
- exit code: 0 on success, 1 on any error (detail on stderr).

The DB path comes from the environment (`MEMEX_DB_PATH`), which the parent sets
to an absolute path so the child does not depend on the working directory.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from typing import Any

from memex.core.embeddings import Embedder, get_default_embedder
from memex.core.ingest.pipeline import ingest_single_conversation
from memex.core.models import Source
from memex.core.storage.db import connect_and_init


def run_ingest(
    payload: dict[str, Any],
    source: Source,
    *,
    conn: sqlite3.Connection | None = None,
    embedder: Embedder | None = None,
) -> dict[str, int]:
    """Ingest one conversation and return the summary as a plain dict.

    `conn` / `embedder` are injectable for tests; in the real subprocess they
    are built from config (a fresh DB connection and the default embedder).
    """
    own_conn = conn is None
    conn = conn or connect_and_init()
    try:
        emb = embedder or get_default_embedder()
        summary = ingest_single_conversation(conn, emb, payload, source=source)
        return {
            "conversations": summary.conversations,
            "messages": summary.messages,
            "chunks": summary.chunks,
            "skipped_empty_messages": summary.skipped_empty_messages,
        }
    finally:
        if own_conn:
            conn.close()


def main() -> int:
    # Logs/warnings to stderr so stdout is purely the result JSON.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        source = Source(sys.argv[1])
        payload = json.load(sys.stdin)
        result = run_ingest(payload, source)
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
        return 0
    except Exception as e:
        sys.stderr.write(f"ingest_worker error: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

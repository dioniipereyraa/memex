"""Tests for the shared single-flight ingest lock (`memex.ingest_lock`).

The lock keeps the CLI ingest and the live-capture worker from each loading the
embedding model at the same time (which would stack ~0.5 GB per loader).
"""

from __future__ import annotations

import time

from memex import ingest_lock


class TestNonBlocking:
    def test_second_acquire_is_blocked_then_released(self, tmp_path) -> None:
        db = tmp_path / "memex.db"
        first = ingest_lock.acquire_nonblocking(db)
        assert first is not None

        # While held, a second non-blocking acquire on the same path fails.
        second = ingest_lock.acquire_nonblocking(db)
        assert second is None

        # After release, the lock is free again.
        ingest_lock.release(first)
        third = ingest_lock.acquire_nonblocking(db)
        assert third is not None
        ingest_lock.release(third)


class TestBlocking:
    def test_acquires_when_free(self, tmp_path) -> None:
        db = tmp_path / "memex.db"
        handle = ingest_lock.acquire_blocking(db, timeout=1.0)
        assert handle is not None
        ingest_lock.release(handle)

    def test_times_out_while_held(self, tmp_path) -> None:
        db = tmp_path / "memex.db"
        held = ingest_lock.acquire_nonblocking(db)
        assert held is not None

        start = time.monotonic()
        blocked = ingest_lock.acquire_blocking(db, timeout=0.3)
        elapsed = time.monotonic() - start
        assert blocked is None  # could not get it within the timeout
        assert elapsed >= 0.3  # it actually waited

        # Once the holder releases, a blocking acquire succeeds.
        ingest_lock.release(held)
        after = ingest_lock.acquire_blocking(db, timeout=1.0)
        assert after is not None
        ingest_lock.release(after)


class TestRelease:
    def test_release_handles_sentinel_and_none(self) -> None:
        # A bare sentinel (best-effort path) and None must not raise.
        ingest_lock.release(object())
        ingest_lock.release(None)

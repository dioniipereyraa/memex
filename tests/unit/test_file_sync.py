"""Tests for file-based sync (the dual-boot mode): export/import via a folder.

The core logic (export_snapshot / import_once / sync_once) takes explicit
dir/device/model/dim arguments, so it is tested without any global state or a
live socket. Two in-memory DBs stand in for two devices sharing a directory.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import pytest

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.models import Chunk, Conversation, Message, Sender, Source
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init
from memex.sync import file_sync, records

EMBEDDER = FakeEmbedder(dim=768)
MODEL, DIM = EMBEDDER.model_name, EMBEDDER.dim


def _dt(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=UTC)


def _seed(
    conn,
    uuid: str,
    content_hash: str,
    updated_at: datetime,
    *,
    text: str = "a chunk of text",
    n_chunks: int = 1,
    title: str = "title",
    source: Source = Source.CONVERSATIONS,
) -> None:
    """Insert one conversation with a message and `n_chunks` embedded chunks."""
    repo.insert_conversation(
        conn,
        Conversation(
            uuid=uuid,
            title=title,
            source=source,
            account_uuid="acct-1",
            created_at=updated_at,
            updated_at=updated_at,
            content_hash=content_hash,
        ),
    )
    repo.insert_message(
        conn,
        Message(
            uuid=f"{uuid}-m1",
            conversation_uuid=uuid,
            sender=Sender.HUMAN,
            text=text,
            created_at=updated_at,
            updated_at=updated_at,
        ),
    )
    for i in range(n_chunks):
        body = f"{text} {i}"
        repo.add_chunk(
            conn,
            Chunk(
                conversation_uuid=uuid,
                message_uuid=f"{uuid}-m1",
                sender="human",
                text=body,
                char_start=0,
                char_end=len(body),
                created_at=updated_at,
            ),
            EMBEDDER.embed_one(body),
        )


def _conn():
    return connect_and_init(":memory:", check_same_thread=False)


# --------------------------------------------------------------------------
# Naming + header
# --------------------------------------------------------------------------


class TestNamingAndHeader:
    def test_slug_sanitizes(self) -> None:
        assert file_sync._slug("My Laptop!") == "My-Laptop"
        assert file_sync._slug("  ") == "device"
        assert file_sync._slug("linux/win:box") == "linux-win-box"

    def test_snapshot_path(self, tmp_path) -> None:
        p = file_sync.snapshot_path(tmp_path, "Linux Box")
        assert p.name == "Linux-Box.memexsync.gz"
        assert p.parent == tmp_path

    def test_export_then_read_header(self, tmp_path) -> None:
        conn = _conn()
        _seed(conn, "c1", "h1", _dt(24))
        path, exported = file_sync.export_snapshot(conn, tmp_path, "dev", MODEL, DIM)
        assert exported is True
        header = file_sync.read_header(path)
        assert header is not None
        assert header["format"] == file_sync.FORMAT_VERSION
        assert header["device"] == "dev"
        assert header["embed_model"] == MODEL
        assert header["embed_dim"] == DIM
        assert isinstance(header["manifest"], list)
        assert {m["uuid"] for m in header["manifest"]} == {"c1"}

    def test_read_header_missing_file(self, tmp_path) -> None:
        assert file_sync.read_header(tmp_path / "nope.memexsync.gz") is None


# --------------------------------------------------------------------------
# Export skip-if-unchanged
# --------------------------------------------------------------------------


class TestExportSkip:
    def test_unchanged_store_skips_rewrite(self, tmp_path) -> None:
        conn = _conn()
        _seed(conn, "c1", "h1", _dt(24))
        _, first = file_sync.export_snapshot(conn, tmp_path, "dev", MODEL, DIM)
        _, second = file_sync.export_snapshot(conn, tmp_path, "dev", MODEL, DIM)
        assert first is True
        assert second is False  # fingerprint unchanged -> no rewrite

    def test_changed_store_rewrites(self, tmp_path) -> None:
        conn = _conn()
        _seed(conn, "c1", "h1", _dt(24))
        file_sync.export_snapshot(conn, tmp_path, "dev", MODEL, DIM)
        _seed(conn, "c2", "h2", _dt(25))  # new content
        _, exported = file_sync.export_snapshot(conn, tmp_path, "dev", MODEL, DIM)
        assert exported is True


# --------------------------------------------------------------------------
# Import + convergence
# --------------------------------------------------------------------------


class TestImportConvergence:
    def test_b_imports_a(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24), text="alpha content", n_chunks=2)
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)

        summary = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert summary.pulled == 1
        assert summary.failed == 0
        got = repo.get_conversation(b, "c1")
        assert got is not None
        assert got.content_hash == "h1"
        # Vectors travelled (no re-embed): the chunk rows exist with embeddings.
        chunks = repo.get_chunks_with_embeddings_for_conversation(b, "c1")
        assert len(chunks) == 2
        assert all(len(emb) == DIM for _, emb in chunks)

    def test_newer_remote_wins_older_does_not_overwrite(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        # Same uuid, A newer (day 25) than B (day 24).
        _seed(a, "c1", "hA", _dt(25), text="newer")
        _seed(b, "c1", "hB", _dt(24), text="older")
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        file_sync.export_snapshot(b, tmp_path, "devb", MODEL, DIM)

        # B imports A's snapshot: A is newer -> B takes A's version.
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.pulled == 1
        assert repo.get_conversation(b, "c1").content_hash == "hA"

        # A imports B's snapshot: B is older -> A keeps its own (no overwrite).
        sa = file_sync.import_once(a, tmp_path, "deva", MODEL, DIM)
        assert sa.pulled == 0
        assert repo.get_conversation(a, "c1").content_hash == "hA"

    def test_fork_left_untouched(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        # Same uuid + same updated_at, different content = a fork.
        _seed(a, "c1", "hA", _dt(24), text="A-side")
        _seed(b, "c1", "hB", _dt(24), text="B-side")
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)

        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.forks == 1
        assert sb.pulled == 0
        assert repo.get_conversation(b, "c1").content_hash == "hB"  # untouched

    def test_would_push_reported(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        _seed(a, "c1", "hA", _dt(25))  # A has something B lacks
        _seed(b, "c2", "hB", _dt(25))  # B has something A lacks
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        # B sees A's file: A has c1 (B will pull), B has c2 (A will pull later).
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.pulled == 1  # c1 pulled from A
        assert sb.would_push == 1  # c2 will reach A on its next run

    def test_sync_once_two_pass_convergence(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        _seed(a, "ca", "ha", _dt(24), text="from A")
        _seed(b, "cb", "hb", _dt(25), text="from B")
        # Pass 1: each device imports what exists, then exports.
        file_sync.sync_once(a, tmp_path, "deva", MODEL, DIM)
        file_sync.sync_once(b, tmp_path, "devb", MODEL, DIM)
        # Pass 2: now each peer file has the other's data.
        file_sync.sync_once(a, tmp_path, "deva", MODEL, DIM)
        file_sync.sync_once(b, tmp_path, "devb", MODEL, DIM)
        for conn in (a, b):
            assert repo.get_conversation(conn, "ca") is not None
            assert repo.get_conversation(conn, "cb") is not None

    def test_three_devices(self, tmp_path) -> None:
        a, b, c = _conn(), _conn(), _conn()
        _seed(a, "ca", "ha", _dt(24))
        _seed(c, "cc", "hc", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        file_sync.export_snapshot(c, tmp_path, "devc", MODEL, DIM)
        # B sees both A and C files and pulls from each.
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.peers_seen == 2
        assert sb.pulled == 2
        assert repo.get_conversation(b, "ca") is not None
        assert repo.get_conversation(b, "cc") is not None


# --------------------------------------------------------------------------
# Safety: model mismatch, ownership, malformed input
# --------------------------------------------------------------------------


class TestSafety:
    def test_model_mismatch_refused(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        # A's snapshot advertises a different embedding model.
        file_sync.export_snapshot(a, tmp_path, "deva", "other-model", DIM)
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.incompatible == 1
        assert sb.pulled == 0
        assert repo.get_conversation(b, "c1") is None

    def test_dim_mismatch_refused(self, tmp_path) -> None:
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, 512)
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.incompatible == 1
        assert sb.pulled == 0

    def test_does_not_import_own_file(self, tmp_path) -> None:
        a = _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        # Importing with the SAME device name must not read its own snapshot.
        peers = file_sync.list_peer_snapshots(tmp_path, "deva")
        assert peers == []
        sa = file_sync.import_once(a, tmp_path, "deva", MODEL, DIM)
        assert sa.peers_seen == 0
        assert sa.pulled == 0

    def test_corrupt_peer_file_skipped(self, tmp_path) -> None:
        b = _conn()
        bad = tmp_path / "junk.memexsync.gz"
        bad.write_bytes(b"not gzip at all")
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        # The bad file is counted as seen but yields nothing, never crashes.
        assert sb.peers_seen == 1
        assert sb.pulled == 0
        assert sb.failed == 0

    def test_oversized_line_refused(self, tmp_path, monkeypatch) -> None:
        # A crafted gzip whose first line exceeds the cap must not be read into
        # unbounded memory; read_header returns None (skipped), never OOMs.
        monkeypatch.setattr(file_sync, "_MAX_LINE_BYTES", 64)
        path = tmp_path / "huge.memexsync.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("x" * 500)  # one 500-byte line, no newline, over the 64 cap
        assert file_sync.read_header(path) is None

    def test_oversized_header_refused(self, tmp_path, monkeypatch) -> None:
        # The header line is parsed whole into memory (json.loads + the diff), so
        # it has a tighter cap than record lines. An over-cap header must be
        # refused (read_header None), never parsed, even if it is under the record
        # cap. Shrink the header cap for a cheap check.
        monkeypatch.setattr(file_sync, "_MAX_HEADER_BYTES", 64)
        path = tmp_path / "bighdr.memexsync.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"format": 1, "pad": "x" * 500}))  # >64B header, no NL
        assert file_sync.read_header(path) is None

    def test_deeply_nested_header_refused(self, tmp_path) -> None:
        # A header whose JSON is nested absurdly deep makes json.loads raise
        # RecursionError (a tiny file, so no size cap catches it). read_header must
        # catch it and fail soft to None, not propagate out of the import loop.
        path = tmp_path / "nested.memexsync.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("[" * 5000 + "]" * 5000)
        assert file_sync.read_header(path) is None

    def test_total_decompression_budget_refused(self, tmp_path, monkeypatch) -> None:
        # Many small lines (each under the per-line cap) must still be bounded by
        # the total-decompressed-bytes budget, so a bomb of small lines cannot
        # force unbounded decompression. Shrink the total budget for the test.
        monkeypatch.setattr(file_sync, "_MAX_TOTAL_BYTES", 256 * 1024)
        path = tmp_path / "manylines.memexsync.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"format": 1}) + "\n")
            for _ in range(50):
                fh.write("y" * 20000 + "\n")  # ~1 MB decompressed total
        with pytest.raises(ValueError, match="decompressed bytes"):
            list(file_sync._iter_records(path, {"never"}))

    def test_poisoned_early_file_does_not_abort_other_peers(self, tmp_path) -> None:
        # A poisoned snapshot that sorts BEFORE a healthy one (glob is sorted) must
        # skip only itself; the healthy peer must still import. Before the fix a
        # RecursionError from the poisoned header propagated out of the whole loop,
        # so the healthy peer silently stopped converging.
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        # Healthy peer file, name sorts LAST.
        file_sync.export_snapshot(a, tmp_path, "zzz-good", MODEL, DIM)
        # Poisoned file, name sorts FIRST, with a RecursionError-triggering header.
        poisoned = tmp_path / "aaa-bad.memexsync.gz"
        with gzip.open(poisoned, "wt", encoding="utf-8") as fh:
            fh.write("[" * 5000 + "]" * 5000)
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.peers_seen == 2
        assert sb.pulled == 1  # the healthy peer's conversation still imported
        assert repo.get_conversation(b, "c1") is not None

    def test_malformed_record_line_skipped(self, tmp_path) -> None:
        # A valid header followed by a non-JSON record line: the record is skipped.
        path = tmp_path / "mix.memexsync.gz"
        header = {
            "format": 1,
            "device": "x",
            "embed_model": MODEL,
            "embed_dim": DIM,
            "manifest": [{"uuid": "c1"}],
        }
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            fh.write("{not valid json\n")
            fh.write(json.dumps({"uuid": "c1", "real": True}) + "\n")
        got = list(file_sync._iter_records(path, {"c1"}))
        assert got == [{"uuid": "c1", "real": True}]

    def test_insert_record_chunk_cap_enforced(self, tmp_path, monkeypatch) -> None:
        # The file insert path shares insert_record, so the chunk cap still applies.
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24), n_chunks=3)
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        monkeypatch.setattr(records.settings, "max_chunks_per_conversation", 1)
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.failed == 1  # the 3-chunk record exceeds the cap of 1
        assert sb.pulled == 0
        assert repo.get_conversation(b, "c1") is None

    def test_truncated_gzip_peer_skips_only_itself(self, tmp_path) -> None:
        # A partially-copied peer file (cloud-synced folder, USB, power cut mid
        # copy) makes gzip raise EOFError, which is NOT an OSError subclass. It
        # must skip only that file: the healthy peer sorting after it still
        # imports, and read_header fails soft to None.
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "zzz-good", MODEL, DIM)
        full = (tmp_path / "zzz-good.memexsync.gz").read_bytes()
        (tmp_path / "aaa-truncated.memexsync.gz").write_bytes(full[: len(full) // 2])
        assert file_sync.read_header(tmp_path / "aaa-truncated.memexsync.gz") is None
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.peers_seen == 2
        assert sb.pulled == 1
        assert repo.get_conversation(b, "c1") is not None

    def test_bitflipped_gzip_peer_skips_only_itself(self, tmp_path) -> None:
        # Corruption INSIDE the deflate stream raises zlib.error (not OSError,
        # not BadGzipFile). Whatever the exact exception, the poisoned file must
        # skip only itself.
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "zzz-good", MODEL, DIM)
        blob = bytearray((tmp_path / "zzz-good.memexsync.gz").read_bytes())
        for i in range(len(blob) // 2, len(blob) // 2 + 8):
            blob[i] ^= 0xFF
        (tmp_path / "aaa-corrupt.memexsync.gz").write_bytes(bytes(blob))
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.peers_seen == 2
        assert sb.pulled == 1
        assert repo.get_conversation(b, "c1") is not None

    def test_non_string_manifest_uuid_skipped(self, tmp_path) -> None:
        # A crafted header whose manifest carries a non-string uuid (a list is
        # unhashable) must not blow up the diff and abort the import loop; the
        # bad entries are ignored and the healthy peer still imports.
        a, b = _conn(), _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "zzz-good", MODEL, DIM)
        header = {
            "format": 1,
            "device": "aaa-bad",
            "embed_model": MODEL,
            "embed_dim": DIM,
            "manifest": [
                {"uuid": ["not", "hashable"], "content_hash": "h", "updated_at": "2026"},
                {"uuid": 7, "content_hash": "h", "updated_at": "2026"},
            ],
        }
        with gzip.open(tmp_path / "aaa-bad.memexsync.gz", "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
        sb = file_sync.import_once(b, tmp_path, "devb", MODEL, DIM)
        assert sb.peers_seen == 2
        assert sb.pulled == 1
        assert repo.get_conversation(b, "c1") is not None

    def test_sync_once_still_exports_past_bad_peer_file(self, tmp_path) -> None:
        # A bad peer file must not stop sync_once from writing this device's own
        # snapshot (before the fix the raise skipped the export, so the OTHER
        # device also stopped receiving updates).
        b = _conn()
        _seed(b, "c-local", "h1", _dt(24))
        (tmp_path / "aaa-truncated.memexsync.gz").write_bytes(b"\x1f\x8b\x08\x00trunc")
        summary = file_sync.sync_once(b, tmp_path, "devb", MODEL, DIM)
        assert summary.exported is True
        assert (tmp_path / "devb.memexsync.gz").exists()

    def test_export_self_heals_own_truncated_snapshot(self, tmp_path) -> None:
        # export_snapshot reads its OWN file for the fingerprint skip-check. A
        # truncated own file must read as absent and be REWRITTEN (self-heal),
        # never crash the export forever.
        a = _conn()
        _seed(a, "c1", "h1", _dt(24))
        file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        own = tmp_path / "deva.memexsync.gz"
        own.write_bytes(own.read_bytes()[:20])
        _, exported = file_sync.export_snapshot(a, tmp_path, "deva", MODEL, DIM)
        assert exported is True
        assert file_sync.read_header(own) is not None  # valid again


# --------------------------------------------------------------------------
# Resolution of dir/device from settings + persisted state
# --------------------------------------------------------------------------


class TestResolution:
    def test_resolve_sync_dir_env_wins(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(file_sync.settings, "sync_dir", tmp_path / "env")
        assert file_sync.resolve_sync_dir() == tmp_path / "env"

    def test_resolve_sync_dir_persisted(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(file_sync.settings, "sync_dir", None)
        monkeypatch.setattr(file_sync.sync_state, "get_sync_dir", lambda: "/persisted")
        from pathlib import Path

        assert file_sync.resolve_sync_dir() == Path("/persisted")

    def test_resolve_sync_dir_none(self, monkeypatch) -> None:
        monkeypatch.setattr(file_sync.settings, "sync_dir", None)
        monkeypatch.setattr(file_sync.sync_state, "get_sync_dir", lambda: None)
        assert file_sync.resolve_sync_dir() is None

    def test_resolve_device_name_persisted_wins(self, monkeypatch) -> None:
        monkeypatch.setattr(file_sync.sync_state, "get_device_name", lambda: "persisted-name")
        assert file_sync.resolve_device_name() == "persisted-name"

    def test_resolve_device_name_falls_back_to_setting(self, monkeypatch) -> None:
        monkeypatch.setattr(file_sync.sync_state, "get_device_name", lambda: None)
        monkeypatch.setattr(file_sync.settings, "device_name", "hostnamed")
        assert file_sync.resolve_device_name() == "hostnamed"


@pytest.fixture(autouse=True)
def _isolate_resolution(monkeypatch):
    """Keep resolve_* off the real per-user gate file by default."""
    monkeypatch.setattr(file_sync.sync_state, "get_sync_dir", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(file_sync.sync_state, "get_device_name", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(file_sync.settings, "sync_dir", None, raising=True)

"""Tests for ingest_claude_code_sessions (incremental + repo association)."""

from __future__ import annotations

import json

from memex.core.embeddings.fake import FakeEmbedder
from memex.core.ingest.pipeline import ingest_claude_code_sessions
from memex.core.models import Source
from memex.core.repos import canonical_repo_key, normalize_path
from memex.core.repos.discovery import RepoInfo
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init


def make_session(projects_root, encoded_dir, session_id, lines):
    """Write a session file under <projects_root>/<encoded_dir>/<id>.jsonl."""
    d = projects_root / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return p


def turn(role, text, uuid, cwd, ts="2026-06-10T10:00:00.000Z"):
    content = text if role == "user" else [{"type": "text", "text": text}]
    return {
        "type": role,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": cwd,
        "message": {"role": role, "content": content},
    }


class TestIngestClaudeCodeSessions:
    def test_basic_ingest(self, tmp_path):
        root = tmp_path / "projects"
        make_session(
            root,
            "-Users-d-proj",
            "sess-1",
            [
                turn("user", "arreglá el login", "u1", "/Users/d/proj"),
                turn("assistant", "hecho", "a1", "/Users/d/proj"),
            ],
        )
        db = connect_and_init(":memory:")
        try:
            summary = ingest_claude_code_sessions(db, FakeEmbedder(dim=768), root)
            assert summary.conversations == 1
            assert summary.messages == 2
            assert summary.chunks > 0
            conv = repo.get_conversation(db, "sess-1")
            assert conv is not None
            assert conv.source is Source.CLAUDE_CODE
        finally:
            db.close()

    def test_incremental_skip_unchanged(self, tmp_path):
        root = tmp_path / "projects"
        make_session(
            root,
            "-Users-d-proj",
            "sess-1",
            [
                turn("user", "hola", "u1", "/Users/d/proj"),
                turn("assistant", "chau", "a1", "/Users/d/proj"),
            ],
        )
        db = connect_and_init(":memory:")
        try:
            emb = FakeEmbedder(dim=768)
            first = ingest_claude_code_sessions(db, emb, root)
            assert first.conversations == 1
            assert first.skipped_unchanged_conversations == 0

            # Re-run without changes: the session must be skipped, not re-embedded.
            second = ingest_claude_code_sessions(db, emb, root)
            assert second.conversations == 0
            assert second.skipped_unchanged_conversations == 1
            assert second.chunks == 0
        finally:
            db.close()

    def test_reingest_after_change(self, tmp_path):
        root = tmp_path / "projects"
        path = make_session(
            root,
            "-Users-d-proj",
            "sess-1",
            [
                turn("user", "primera", "u1", "/Users/d/proj"),
                turn("assistant", "ok", "a1", "/Users/d/proj"),
            ],
        )
        db = connect_and_init(":memory:")
        try:
            emb = FakeEmbedder(dim=768)
            ingest_claude_code_sessions(db, emb, root)

            # Append a new turn (sessions grow append-only) and re-scan.
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\n"
                + json.dumps(turn("user", "segunda pregunta nueva", "u2", "/Users/d/proj")),
                encoding="utf-8",
            )
            second = ingest_claude_code_sessions(db, emb, root)
            assert second.conversations == 1
            assert second.skipped_unchanged_conversations == 0
        finally:
            db.close()

    def test_associates_session_to_repo_of_cwd(self, tmp_path):
        # Register a repo whose path is the session's cwd.
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        cwd = str(proj_dir)
        key = canonical_repo_key(cwd)

        root = tmp_path / "projects"
        make_session(
            root,
            "-proj",
            "sess-1",
            [
                turn("user", "trabajando en este repo", "u1", cwd),
                turn("assistant", "ok", "a1", cwd),
            ],
        )
        db = connect_and_init(":memory:")
        try:
            repo.insert_repo(
                db,
                RepoInfo(
                    key=key,
                    path=normalize_path(cwd),
                    remote_url=None,
                    name="proj",
                    manifest_name=None,
                ),
            )
            ingest_claude_code_sessions(db, FakeEmbedder(dim=768), root)
            assocs = repo.list_repos_for_conversation(db, "sess-1")
            assert any(a.repo.key == key and a.confidence == 1.0 for a in assocs)
        finally:
            db.close()

    def test_associates_session_to_repo_with_git_remote(self, tmp_path):
        # The real case: a repo registered with a git remote is keyed by the
        # remote URL, NOT its path. The cwd→repo association must still fire.
        proj_dir = tmp_path / "realproj"
        proj_dir.mkdir()
        cwd = str(proj_dir)
        remote = "git@github.com:dioni/realproj.git"
        key = canonical_repo_key(cwd, remote_url=remote)  # -> github.com/dioni/realproj
        assert key == "github.com/dioni/realproj"

        root = tmp_path / "projects"
        make_session(
            root,
            "-realproj",
            "sess-r",
            [
                turn("user", "laburando en el repo con remote", "u1", cwd),
                turn("assistant", "ok", "a1", cwd),
            ],
        )
        db = connect_and_init(":memory:")
        try:
            repo.insert_repo(
                db,
                RepoInfo(
                    key=key,
                    path=normalize_path(cwd),
                    remote_url=remote,
                    name="realproj",
                    manifest_name=None,
                ),
            )
            ingest_claude_code_sessions(db, FakeEmbedder(dim=768), root)
            assocs = repo.list_repos_for_conversation(db, "sess-r")
            assert any(a.repo.key == key and a.confidence == 1.0 for a in assocs), (
                "session must associate to a remote-keyed repo via its cwd path"
            )
        finally:
            db.close()

    def test_missing_root_reports_error(self, tmp_path):
        db = connect_and_init(":memory:")
        try:
            summary = ingest_claude_code_sessions(db, FakeEmbedder(dim=768), tmp_path / "nope")
            assert summary.conversations == 0
            assert summary.errors
        finally:
            db.close()

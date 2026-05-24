"""Tests for the `memex repos` sub-app and `memex tag`/`untag` commands.

Uses `typer.testing.CliRunner` against a real SQLite DB on disk
(`tmp_path / memex.db`) so each test exercises the full CLI -> repo -> DB
loop without mocking the storage layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from memex.cli.main import app
from memex.core.models import Conversation, Source
from memex.core.repos.discovery import RepoInfo
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init

runner = CliRunner()


def _seed_conv(db_path: Path, uuid: str, *, text: str = "hola") -> None:
    """Insert a single-message conversation directly via the repo layer."""
    conn = connect_and_init(db_path)
    try:
        conv = Conversation(
            uuid=uuid,
            title=f"Conv {uuid}",
            source=Source.CONVERSATIONS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        repo.insert_conversation(conn, conv)
        from memex.core.models import Message, Sender

        repo.insert_message(
            conn,
            Message(
                uuid=f"{uuid}-m1",
                conversation_uuid=uuid,
                sender=Sender.HUMAN,
                text=text,
                created_at=conv.created_at,
                updated_at=conv.updated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_fake_repo(base: Path, name: str, remote: str | None = None) -> Path:
    """Make a minimal directory that `parse_repo` can read."""
    repo_path = base / name
    repo_path.mkdir()
    (repo_path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    if remote:
        (repo_path / ".git").mkdir()
        (repo_path / ".git" / "config").write_text(
            f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8"
        )
    return repo_path


class TestReposAdd:
    def test_add_registers_a_repo(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        fake = _make_fake_repo(tmp_path, "myrepo", remote="git@github.com:me/myrepo.git")

        result = runner.invoke(app, ["repos", "add", str(fake), "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Registered" in result.output
        assert "myrepo" in result.output

        # Persisted to DB.
        conn = connect_and_init(db)
        try:
            assert repo.get_repo(conn, "github.com/me/myrepo") is not None
        finally:
            conn.close()

    def test_add_twice_refreshes_without_dup(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        fake = _make_fake_repo(tmp_path, "myrepo", remote="git@github.com:me/myrepo.git")

        runner.invoke(app, ["repos", "add", str(fake), "--db", str(db)])
        result = runner.invoke(app, ["repos", "add", str(fake), "--db", str(db)])
        assert result.exit_code == 0
        assert "Updated" in result.output

        conn = connect_and_init(db)
        try:
            assert len(repo.list_repos(conn)) == 1
        finally:
            conn.close()

    def test_add_nonexistent_path_exits_error(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        result = runner.invoke(
            app,
            ["repos", "add", str(tmp_path / "does-not-exist"), "--db", str(db)],
        )
        assert result.exit_code == 1


class TestReposList:
    def test_empty_shows_friendly_message(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        result = runner.invoke(app, ["repos", "list", "--db", str(db)])
        assert result.exit_code == 0
        assert "No repos registered" in result.output

    def test_shows_registered_repos(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(
                    key="github.com/me/proj",
                    path="/dev/proj",
                    remote_url="git@github.com:me/proj.git",
                    name="proj",
                    manifest_name="proj",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(app, ["repos", "list", "--db", str(db)])
        assert result.exit_code == 0
        assert "github.com/me/proj" in result.output
        assert "proj" in result.output


class TestReposRemove:
    def test_remove_existing(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(key="k1", path=None, remote_url=None, name="r1", manifest_name=None),
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(app, ["repos", "remove", "k1", "--db", str(db)])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_missing_exits_error(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        result = runner.invoke(app, ["repos", "remove", "nope", "--db", str(db)])
        assert result.exit_code == 1


class TestReposScan:
    def test_no_repos_friendly_message(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        result = runner.invoke(app, ["repos", "scan", "--db", str(db)])
        assert result.exit_code == 0
        assert "No repos registered" in result.output

    def test_no_chats_friendly_message(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(key="k1", path=None, remote_url=None, name="r1", manifest_name=None),
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(app, ["repos", "scan", "--db", str(db)])
        assert result.exit_code == 0
        assert "No conversations" in result.output

    def test_associates_matching_chats(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(
                    key="github.com/me/scan-target",
                    path=None,
                    remote_url="github.com/me/scan-target",
                    name="scan-target",
                    manifest_name=None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Seed a chat that mentions the repo's remote URL.
        _seed_conv(db, "c-match", text="check github.com/me/scan-target out")
        # And one that does not.
        _seed_conv(db, "c-nomatch", text="totally unrelated message")

        result = runner.invoke(app, ["repos", "scan", "--db", str(db)])
        assert result.exit_code == 0
        assert "Scanned" in result.output
        assert "Applied" in result.output

        conn = connect_and_init(db)
        try:
            matched = repo.list_repos_for_conversation(conn, "c-match")
            assert len(matched) == 1
            assert matched[0].repo.key == "github.com/me/scan-target"
            assert matched[0].source == "auto"

            unmatched = repo.list_repos_for_conversation(conn, "c-nomatch")
            assert unmatched == []
        finally:
            conn.close()


class TestTagUntag:
    def _setup(self, tmp_path: Path) -> Path:
        db = tmp_path / "memex.db"
        _seed_conv(db, "c1")
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(key="r1", path=None, remote_url=None, name="r1", manifest_name=None),
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def test_tag_persists_as_manual(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path)
        result = runner.invoke(app, ["tag", "c1", "r1", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Tagged" in result.output

        conn = connect_and_init(db)
        try:
            assocs = repo.list_repos_for_conversation(conn, "c1")
            assert len(assocs) == 1
            assert assocs[0].source == "manual"
        finally:
            conn.close()

    def test_tag_missing_chat_errors(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path)
        result = runner.invoke(app, ["tag", "missing-uuid", "r1", "--db", str(db)])
        assert result.exit_code == 1
        assert "Conversation not found" in result.output

    def test_tag_missing_repo_errors(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path)
        result = runner.invoke(app, ["tag", "c1", "missing-repo", "--db", str(db)])
        assert result.exit_code == 1
        assert "Repo not found" in result.output

    def test_untag_removes_association(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path)
        runner.invoke(app, ["tag", "c1", "r1", "--db", str(db)])
        result = runner.invoke(app, ["untag", "c1", "r1", "--db", str(db)])
        assert result.exit_code == 0
        assert "Untagged" in result.output

        conn = connect_and_init(db)
        try:
            assert repo.list_repos_for_conversation(conn, "c1") == []
        finally:
            conn.close()

    def test_untag_missing_returns_error(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path)
        result = runner.invoke(app, ["untag", "c1", "r1", "--db", str(db)])
        assert result.exit_code == 1


class TestSessionContext:
    """Tests for `memex session-context`, the SessionStart hook helper.

    Avoids relying on cwd auto-detection by passing `--repo` explicitly when
    possible. The one cwd-based test uses `monkeypatch.chdir` to a known
    location.
    """

    def test_no_repo_arg_no_git_prints_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """If `--repo` is omitted and cwd has no .git ancestor, output is empty."""
        db = tmp_path / "memex.db"
        # Use a fresh sandbox with no .git anywhere up.
        sandbox = tmp_path / "no-git-here"
        sandbox.mkdir()
        monkeypatch.chdir(sandbox)
        result = runner.invoke(app, ["session-context", "--db", str(db)])
        assert result.exit_code == 0
        # stderr carries diagnostics; stdout should be the context blob only.
        assert result.stdout.strip() == ""

    def test_unregistered_repo_prints_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        result = runner.invoke(
            app,
            ["session-context", "--repo", "github.com/never/registered", "--db", str(db)],
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_registered_repo_without_associations_prints_nothing(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(
                    key="local/empty",
                    path=None,
                    remote_url=None,
                    name="empty",
                    manifest_name=None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = runner.invoke(app, ["session-context", "--repo", "local/empty", "--db", str(db)])
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_prints_associated_chats(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        _seed_conv(db, "c-auto", text="auto match")
        _seed_conv(db, "c-manual", text="manual tag")
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(
                    key="local/proj",
                    path=None,
                    remote_url=None,
                    name="proj",
                    manifest_name=None,
                ),
            )
            repo.associate_chat_repo(conn, "c-auto", "local/proj", source="auto", confidence=0.9)
            repo.associate_chat_repo(conn, "c-manual", "local/proj", source="manual")
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(app, ["session-context", "--repo", "local/proj", "--db", str(db)])
        assert result.exit_code == 0
        out = result.stdout
        assert "Memex" in out
        assert "proj" in out
        # Manual should come first.
        manual_idx = out.find("c-manual")
        auto_idx = out.find("c-auto")
        assert manual_idx != -1 and auto_idx != -1
        assert manual_idx < auto_idx
        assert "[manual]" in out
        assert "[auto " in out

    def test_limit_respected(self, tmp_path: Path) -> None:
        db = tmp_path / "memex.db"
        for i in range(5):
            _seed_conv(db, f"c{i}")
        conn = connect_and_init(db)
        try:
            repo.insert_repo(
                conn,
                RepoInfo(
                    key="local/many",
                    path=None,
                    remote_url=None,
                    name="many",
                    manifest_name=None,
                ),
            )
            for i in range(5):
                repo.associate_chat_repo(
                    conn, f"c{i}", "local/many", source="auto", confidence=0.5 + i * 0.1
                )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(
            app,
            ["session-context", "--repo", "local/many", "--limit", "2", "--db", str(db)],
        )
        assert result.exit_code == 0
        # Count chat entries: each one is on its own line starting with "- **".
        chat_lines = [line for line in result.stdout.splitlines() if line.startswith("- **")]
        assert len(chat_lines) == 2

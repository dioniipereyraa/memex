"""Tests for repo + chat_repo CRUD helpers in `storage/repo.py`."""

from __future__ import annotations

import sqlite3

import pytest

from memex.core.models import Conversation, Source
from memex.core.repos.discovery import ChatRepoAssociation, RepoInfo
from memex.core.storage import repo


def _info(
    key: str,
    *,
    path: str | None = "d:/dev/example",
    remote_url: str | None = None,
    name: str = "example",
    manifest_name: str | None = None,
) -> RepoInfo:
    return RepoInfo(
        key=key,
        path=path,
        remote_url=remote_url,
        name=name,
        manifest_name=manifest_name,
    )


def _make_conv(db: sqlite3.Connection, uuid: str) -> Conversation:
    from datetime import UTC, datetime

    conv = Conversation(
        uuid=uuid,
        title=f"Conv {uuid}",
        source=Source.CONVERSATIONS,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.insert_conversation(db, conv)
    return conv


class TestRepoCRUD:
    def test_insert_and_get(self, db: sqlite3.Connection) -> None:
        info = _info("github.com/me/proj", remote_url="git@github.com:me/proj.git", name="proj")
        repo.insert_repo(db, info)
        loaded = repo.get_repo(db, "github.com/me/proj")
        assert loaded == info

    def test_get_missing_returns_none(self, db: sqlite3.Connection) -> None:
        assert repo.get_repo(db, "no/such/repo") is None

    def test_list_orders_by_name_ci(self, db: sqlite3.Connection) -> None:
        repo.insert_repo(db, _info("k1", name="Bravo"))
        repo.insert_repo(db, _info("k2", name="alpha"))
        repo.insert_repo(db, _info("k3", name="Charlie"))
        names = [r.name for r in repo.list_repos(db)]
        assert names == ["alpha", "Bravo", "Charlie"]

    def test_upsert_refreshes_fields(self, db: sqlite3.Connection) -> None:
        repo.insert_repo(db, _info("k", path="old/path", name="old-name"))
        repo.insert_repo(db, _info("k", path="new/path", name="new-name"))
        loaded = repo.get_repo(db, "k")
        assert loaded is not None
        assert loaded.path == "new/path"
        assert loaded.name == "new-name"

    def test_delete_removes(self, db: sqlite3.Connection) -> None:
        repo.insert_repo(db, _info("k"))
        assert repo.delete_repo(db, "k") is True
        assert repo.get_repo(db, "k") is None

    def test_delete_missing_returns_false(self, db: sqlite3.Connection) -> None:
        assert repo.delete_repo(db, "nope") is False


class TestChatRepoAssociation:
    def test_associate_auto(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.9)

        assocs = repo.list_repos_for_conversation(db, "c1")
        assert len(assocs) == 1
        assert assocs[0].source == "auto"
        assert assocs[0].confidence == 0.9
        assert assocs[0].repo.key == "r1"

    def test_associate_manual(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="manual")

        assocs = repo.list_repos_for_conversation(db, "c1")
        assert assocs[0].source == "manual"
        assert assocs[0].confidence is None

    def test_invalid_source_raises(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        with pytest.raises(ValueError):
            repo.associate_chat_repo(db, "c1", "r1", source="bogus")

    def test_manual_not_overwritten_by_auto(self, db: sqlite3.Connection) -> None:
        """Once the user manually tagged a chat, a later auto-scan does not undo it."""
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="manual")

        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.9)
        assocs = repo.list_repos_for_conversation(db, "c1")
        assert assocs[0].source == "manual"
        assert assocs[0].confidence is None

    def test_auto_replaced_by_manual(self, db: sqlite3.Connection) -> None:
        """The user can promote an auto association to manual."""
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.9)

        repo.associate_chat_repo(db, "c1", "r1", source="manual")
        assocs = repo.list_repos_for_conversation(db, "c1")
        assert assocs[0].source == "manual"
        assert assocs[0].confidence is None

    def test_reassociate_auto_refreshes_confidence(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.5)
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.9)
        assocs = repo.list_repos_for_conversation(db, "c1")
        assert assocs[0].confidence == 0.9

    def test_dissociate(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=1.0)
        assert repo.dissociate_chat_repo(db, "c1", "r1") is True
        assert repo.list_repos_for_conversation(db, "c1") == []

    def test_dissociate_missing_returns_false(self, db: sqlite3.Connection) -> None:
        assert repo.dissociate_chat_repo(db, "no-conv", "no-repo") is False

    def test_list_repos_for_conversation_ordered_by_confidence(
        self, db: sqlite3.Connection
    ) -> None:
        _make_conv(db, "c1")
        repo.insert_repo(db, _info("r1", name="repo1"))
        repo.insert_repo(db, _info("r2", name="repo2"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.5)
        repo.associate_chat_repo(db, "c1", "r2", source="auto", confidence=0.9)
        assocs = repo.list_repos_for_conversation(db, "c1")
        assert [a.repo.key for a in assocs] == ["r2", "r1"]

    def test_list_conversations_for_repo(self, db: sqlite3.Connection) -> None:
        _make_conv(db, "c1")
        _make_conv(db, "c2")
        _make_conv(db, "c3")
        repo.insert_repo(db, _info("r1"))
        repo.associate_chat_repo(db, "c1", "r1", source="auto", confidence=0.9)
        repo.associate_chat_repo(db, "c2", "r1", source="manual")

        rows = repo.list_conversations_for_repo(db, "r1")
        assert len(rows) == 2
        by_uuid = {uuid: (src, conf) for uuid, src, conf in rows}
        assert by_uuid["c1"] == ("auto", 0.9)
        assert by_uuid["c2"] == ("manual", None)
        assert "c3" not in by_uuid

    def test_repo_info_roundtrip_via_db(self, db: sqlite3.Connection) -> None:
        """A RepoInfo with all fields populated survives insert + list."""
        original = _info(
            "github.com/org/proj",
            path="d:/dev/proj",
            remote_url="git@github.com:org/proj.git",
            name="proj",
            manifest_name="proj-pkg",
        )
        repo.insert_repo(db, original)
        listed = repo.list_repos(db)
        assert len(listed) == 1
        assert listed[0] == original


class TestChatRepoAssociationDataclass:
    """Sanity check that the dataclass equality + hashing work as expected."""

    def test_equal(self) -> None:
        a1 = ChatRepoAssociation(repo=_info("r1"), source="auto", confidence=0.9)
        a2 = ChatRepoAssociation(repo=_info("r1"), source="auto", confidence=0.9)
        assert a1 == a2

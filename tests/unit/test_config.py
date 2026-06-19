"""Tests for the data-dir resolution (Phase B: repo vs wheel install).

The default DB/exports location depends on whether Memex runs from a cloned
repo (keep `<repo>/data`, unchanged for existing installs) or a wheel/PyPI
install (a per-user, OS-conventional directory). These pin both branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memex import config


class TestPlatformDataDir:
    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.sys, "platform", "darwin")
        d = config._platform_data_dir()
        assert d.parts[-3:] == ("Library", "Application Support", "memex")

    def test_windows_uses_localappdata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        assert config._platform_data_dir() == tmp_path / "AppData" / "Local" / "memex"

    def test_linux_uses_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
        assert config._platform_data_dir() == tmp_path / "share" / "memex"

    def test_linux_xdg_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert config._platform_data_dir().parts[-3:] == (".local", "share", "memex")


class TestDefaultDataDir:
    def test_repo_install_uses_repo_data(self) -> None:
        """The suite runs from the cloned repo, so detection returns <repo>/data.

        Guards the back-compat path: an existing repo install must keep using
        its `<repo>/data/memex.db`, now resolved absolutely.
        """
        d = config._default_data_dir()
        assert d.name == "data"
        assert d.is_absolute()
        assert (d.parent / "pyproject.toml").is_file()
        assert (d.parent / "scripts").is_dir()

"""Tests for the CLI with Typer's CliRunner.

Covers the critical paths without needing Ollama: invalid path errors,
messages on empty DB, and stats on a freshly created DB.

Tests that need real ingest (embedder + populated DB) live in
`tests/integration/test_full_flow.py`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memex.cli.main import app

runner = CliRunner()


class TestIngestCommand:
    def test_missing_zip_path_exits_with_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["ingest", str(tmp_path / "does-not-exist.zip"), "--db", str(tmp_path / "x.db")],
        )
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower() or "not found" in result.output.lower()


class TestSearchCommand:
    def test_empty_db_returns_friendly_message(self, tmp_path: Path) -> None:
        """Search against an empty DB should not crash; it should report."""
        # Note: this test would require Ollama if the query is non-trivial,
        # but `vector_search` runs AFTER embed, so the embedder is called
        # first. To avoid the dependency, skip if Ollama is not responding.
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.0) as r:
                if r.status != 200:
                    return  # implicit skip
        except (urllib.error.URLError, TimeoutError, OSError):
            return  # implicit skip if Ollama is not up

        db_path = tmp_path / "empty.db"
        result = runner.invoke(app, ["search", "test query", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "no results" in result.output.lower() or "is the db empty" in result.output.lower()


class TestDoctorCommand:
    """Smoke tests for the `memex doctor` diagnostic command.

    Mocks the live HTTP check so we do not depend on a running server.
    Each test isolates one branch (DB missing, summary enabled w/o key,
    all green, etc.).
    """

    def _mock_server_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force the urllib /health probe to fail with a network error."""
        import urllib.request

        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("simulated network down")

        monkeypatch.setattr(urllib.request, "urlopen", boom)

    def test_missing_db_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_server_down(monkeypatch)
        result = runner.invoke(app, ["doctor", "--db", str(tmp_path / "no.db")])
        # WARN-only state: exit 0, but the table mentions the DB warning.
        assert result.exit_code == 0
        assert "WARN" in result.output
        assert "does not exist" in result.output

    def test_existing_empty_db_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memex.core.storage.db import connect_and_init as _init

        self._mock_server_down(monkeypatch)
        db = tmp_path / "memex.db"
        _init(db).close()
        result = runner.invoke(app, ["doctor", "--db", str(db)])
        # Empty DB: schema OK, but Corpus / Repos are WARN. Exit 0.
        assert result.exit_code == 0
        assert "Database" in result.output
        assert "schema v1" in result.output
        assert "Corpus" in result.output
        assert "Repos" in result.output

    def test_summary_enabled_without_key_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`MEMEX_SUMMARY_ENABLED=true` + missing key = FAIL + exit 1."""
        from memex.config import settings

        self._mock_server_down(monkeypatch)
        monkeypatch.setattr(settings, "summary_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", None)

        result = runner.invoke(app, ["doctor", "--db", str(tmp_path / "no.db")])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "ANTHROPIC_API_KEY missing" in result.output

    def test_summary_disabled_does_not_check_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memex.config import settings

        self._mock_server_down(monkeypatch)
        monkeypatch.setattr(settings, "summary_enabled", False)
        monkeypatch.setattr(settings, "anthropic_api_key", None)

        result = runner.invoke(app, ["doctor", "--db", str(tmp_path / "no.db")])
        # Summary disabled: OK status, never checks key.
        assert result.exit_code == 0
        assert "disabled" in result.output


class TestInstallServiceCommand:
    """Tests for `memex install-service`.

    Mocks subprocess.run and platform.system so the test never actually
    touches the host's service manager.
    """

    def test_invalid_action_returns_error(self) -> None:
        result = runner.invoke(app, ["install-service", "bogus"])
        assert result.exit_code == 2
        assert "Invalid action" in result.output

    def test_windows_calls_powershell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import platform
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["install-service", "install"])
        assert result.exit_code == 0
        cmd = captured["cmd"]
        # Last 2 elements are the script path and the flag.
        assert any("install-autostart.ps1" in part for part in cmd)
        assert "-Install" in cmd

    def test_linux_calls_bash_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import platform
        import subprocess

        captured: dict[str, object] = {}

        def fake_run(cmd, check):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["install-service", "status"])
        assert result.exit_code == 0
        cmd = captured["cmd"]
        # bash + script path + action.
        assert any("install-autostart.sh" in part for part in cmd)
        assert "status" in cmd

    def _fake_macos_repo(self, tmp_path: Path) -> Path:
        """Build a throwaway repo with the two default launchd templates."""
        repo = tmp_path / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        (repo / "pyproject.toml").write_text("")
        for svc in ("serve", "ingest-claude-code"):
            (scripts / f"com.memex.{svc}.plist.template").write_text(
                f"<plist>__REPO__/scripts/{svc}.sh log __REPO__/data/{svc}.log</plist>"
            )
        return repo

    def test_macos_install_loads_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import platform
        import subprocess

        from memex.cli import services

        repo = self._fake_macos_repo(tmp_path)
        agents = tmp_path / "LaunchAgents"
        monkeypatch.setattr(services, "source_repo_root", lambda: repo)
        monkeypatch.setattr(services, "LAUNCH_AGENTS_DIR", agents)

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["install-service", "install"])
        assert result.exit_code == 0
        assert "com.memex.serve" in result.output
        # The plist landed with __REPO__ replaced by the absolute repo path.
        written = (agents / "com.memex.serve.plist").read_text()
        assert "__REPO__" not in written
        assert str(repo) in written
        # launchctl load was invoked once per default agent (serve + ingest).
        assert sum(1 for c in calls if "load" in c) == 2

    def test_macos_wheel_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No repo (wheel install) -> self-contained launchd agents are generated."""
        import platform
        import subprocess
        import sys as _sys

        from memex.cli import services
        from memex.config import settings

        agents = tmp_path / "LaunchAgents"
        monkeypatch.setattr(services, "source_repo_root", lambda: None)
        monkeypatch.setattr(services, "LAUNCH_AGENTS_DIR", agents)
        monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "memex.db")

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["install-service", "install"])
        assert result.exit_code == 0
        assert "com.memex.serve" in result.output
        plist = (agents / "com.memex.serve.plist").read_text()
        assert "__REPO__" not in plist
        assert _sys.executable in plist  # runs the installed interpreter
        assert "memex.cli.main" in plist
        assert str(tmp_path / "data" / "memex.db") in plist  # pinned MEMEX_DB_PATH
        assert sum(1 for c in calls if "load" in c) == 2

    def test_macos_status_reports_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import platform
        import subprocess

        from memex.cli import services

        repo = self._fake_macos_repo(tmp_path)
        agents = tmp_path / "LaunchAgents"
        agents.mkdir()
        # Only the serve agent is installed; ingest is absent.
        (agents / "com.memex.serve.plist").write_text("<plist/>")
        monkeypatch.setattr(services, "source_repo_root", lambda: repo)
        monkeypatch.setattr(services, "LAUNCH_AGENTS_DIR", agents)
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        )
        result = runner.invoke(app, ["install-service", "status"])
        assert result.exit_code == 0
        assert "com.memex.serve" in result.output
        assert "not installed" in result.output

    def test_windows_wheel_install_creates_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No repo on Windows -> a logon Scheduled Task is registered via schtasks."""
        import platform
        import subprocess

        from memex.cli import services

        monkeypatch.setattr(services, "source_repo_root", lambda: None)

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = runner.invoke(app, ["install-service", "install"])
        assert result.exit_code == 0
        assert "MemexServe" in result.output
        create = [c for c in calls if "/Create" in c]
        assert create and "/XML" in create[0] and "MemexServe" in create[0]

    def test_unknown_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Solaris")
        result = runner.invoke(app, ["install-service", "install"])
        assert result.exit_code == 2
        assert "Unsupported" in result.output


class TestServices:
    """Pure helpers in `memex.cli.services` (no OS calls)."""

    def test_render_agent_substitutes_repo(self, tmp_path: Path) -> None:
        from memex.cli import services

        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "com.memex.serve.plist.template").write_text(
            "head __REPO__/data/serve.log tail"
        )
        label, xml = services.render_agent(tmp_path, "serve")
        assert label == "com.memex.serve"
        assert "__REPO__" not in xml
        assert str(tmp_path) in xml

    def test_render_agent_unknown_service(self, tmp_path: Path) -> None:
        from memex.cli import services

        with pytest.raises(ValueError):
            services.render_agent(tmp_path, "bogus")

    def test_render_agent_missing_template(self, tmp_path: Path) -> None:
        from memex.cli import services

        (tmp_path / "scripts").mkdir()
        with pytest.raises(FileNotFoundError):
            services.render_agent(tmp_path, "serve")

    def test_render_wheel_plist_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys as _sys

        from memex.cli import services
        from memex.config import settings

        monkeypatch.setattr(settings, "db_path", tmp_path / "memex.db")
        xml = services.render_wheel_plist("serve", tmp_path)
        assert "__REPO__" not in xml
        assert _sys.executable in xml
        assert "memex.cli.main" in xml
        assert "<string>serve</string>" in xml
        assert str(tmp_path / "memex.db") in xml  # MEMEX_DB_PATH pinned
        assert "KeepAlive" in xml  # serve stays alive

    def test_render_wheel_plist_ingest_is_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memex.cli import services
        from memex.config import settings

        monkeypatch.setattr(settings, "db_path", tmp_path / "memex.db")
        xml = services.render_wheel_plist("ingest-claude-code", tmp_path)
        assert "StartInterval" in xml
        assert "<string>ingest-claude-code</string>" in xml

    def test_render_systemd_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys as _sys

        from memex.cli import services
        from memex.config import settings

        db = tmp_path / "memex.db"
        monkeypatch.setattr(settings, "db_path", db)
        unit = services.render_systemd_unit(tmp_path)
        assert "ExecStart=" in unit
        assert _sys.executable in unit
        assert "memex.cli.main" in unit
        assert f'Environment="MEMEX_DB_PATH={db}"' in unit

    def test_render_windows_task_xml(self) -> None:
        import xml.etree.ElementTree as ET

        from memex.cli import services

        x = services.render_windows_task_xml()
        ET.fromstring(x)  # well-formed XML
        assert "<Arguments>-m memex.cli.main serve</Arguments>" in x
        assert "<LogonTrigger>" in x


class TestHeadlessStreams:
    """`serve` must survive pythonw, where sys.stdout/stderr are None."""

    def test_redirect_reopens_streams_to_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys as _sys

        from memex.cli import main as climain
        from memex.config import settings

        monkeypatch.setattr(settings, "db_path", tmp_path / "memex.db")
        monkeypatch.setattr(_sys, "stderr", None)
        monkeypatch.setattr(_sys, "stdout", None)

        climain._redirect_streams_if_headless()

        assert _sys.stdout is not None
        assert _sys.stderr is not None
        assert (tmp_path / "serve.log").is_file()
        # Close the file we opened so monkeypatch can restore the real streams.
        with contextlib.suppress(Exception):
            _sys.stdout.close()

    def test_noop_when_console_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys as _sys

        from memex.cli import main as climain

        before = _sys.stdout  # pytest's capture object, not None
        climain._redirect_streams_if_headless()
        assert _sys.stdout is before


class _FakeEmbedder:
    """Stand-in so `setup` does not construct a real fastembed embedder."""

    model_name = "fake-model"


class TestSetupCommand:
    """`memex setup` orchestration, with every external call mocked.

    None of these touch the real service manager, the `claude` CLI, the
    embedder, or the token file on disk.
    """

    def _stub_token(self, monkeypatch: pytest.MonkeyPatch, value: str = "TESTTOKEN123") -> None:
        from memex.transports import http_ingest

        monkeypatch.setattr(http_ingest, "load_or_create_ingest_token", lambda: value)

    def _stub_embedder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memex.cli import main as climain

        monkeypatch.setattr(climain, "get_default_embedder", lambda: _FakeEmbedder())

    def test_all_skipped_prints_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_token(monkeypatch)
        self._stub_embedder(monkeypatch)
        result = runner.invoke(
            app, ["setup", "-y", "--no-mcp", "--no-autostart", "--no-ingest"]
        )
        assert result.exit_code == 0
        assert "TESTTOKEN123" in result.output
        assert "chromewebstore" in result.output

    def test_mcp_calls_claude_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil
        import subprocess

        from memex.cli import services

        self._stub_token(monkeypatch)
        self._stub_embedder(monkeypatch)
        # PyPI path -> invocation is the bare console script.
        monkeypatch.setattr(services, "source_repo_root", lambda: None)
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None
        )

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = runner.invoke(app, ["setup", "-y", "--no-autostart", "--no-ingest"])
        assert result.exit_code == 0
        add_calls = [c for c in calls if "add" in c]
        assert add_calls
        assert "memex" in add_calls[0]
        assert "memex-mcp" in add_calls[0]

    def test_mcp_no_claude_cli_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        from memex.cli import services

        self._stub_token(monkeypatch)
        self._stub_embedder(monkeypatch)
        monkeypatch.setattr(services, "source_repo_root", lambda: None)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = runner.invoke(app, ["setup", "-y", "--no-autostart", "--no-ingest"])
        assert result.exit_code == 0
        assert "claude mcp add" in result.output


class TestStatsCommand:
    def test_stats_on_empty_db_works(self, tmp_path: Path) -> None:
        """Stats on a freshly created (empty) DB should show zeros without crashing."""
        db_path = tmp_path / "empty.db"
        result = runner.invoke(app, ["stats", "--db", str(db_path)])
        assert result.exit_code == 0
        # Must mention the categories.
        out = result.output
        assert "Projects" in out
        assert "Conversations" in out
        assert "Messages" in out
        assert "Chunks" in out


class TestServeCommand:
    """Tests del comando `memex serve`.

    Mockean `uvicorn.run` y `connect_and_init` para no levantar un server real.
    Verifican que los flags del CLI llegan correctamente al runtime y que `--db`
    inyecta la conn en el módulo `http_ingest` antes de arrancar uvicorn.
    """

    def test_runs_uvicorn_with_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn

        from memex.transports import http_ingest

        captured: dict[str, object] = {}

        def fake_run(starlette_app: object, **kwargs: object) -> None:
            captured["app"] = starlette_app
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)

        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0, result.output
        assert captured["app"] is http_ingest.app
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 5777
        assert captured["log_level"] == "info"

    def test_passes_host_and_port_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn

        captured: dict[str, object] = {}

        def fake_run(starlette_app: object, **kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)

        result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9999"])
        assert result.exit_code == 0, result.output
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 9999

    def test_db_flag_injects_connection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import uvicorn

        from memex.core.storage import db
        from memex.transports import http_ingest

        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
        monkeypatch.setattr(http_ingest, "_conn", None)

        sentinel = object()
        captured: dict[str, object] = {}

        def fake_connect(db_path: object, **kwargs: object) -> object:
            captured["path"] = db_path
            captured["kwargs"] = kwargs
            return sentinel

        monkeypatch.setattr(db, "connect_and_init", fake_connect)

        db_path = tmp_path / "test.db"
        result = runner.invoke(app, ["serve", "--db", str(db_path)])
        assert result.exit_code == 0, result.output
        assert http_ingest._conn is sentinel
        assert captured["path"] == db_path
        # serve() pasa check_same_thread=False porque el server async usa thread pool.
        assert captured["kwargs"].get("check_same_thread") is False

    def test_no_db_flag_leaves_conn_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn

        from memex.transports import http_ingest

        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
        sentinel = object()
        monkeypatch.setattr(http_ingest, "_conn", sentinel)

        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0, result.output
        assert http_ingest._conn is sentinel


class TestHelpAndStructure:
    def test_help_lists_all_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        out = result.output
        assert "ingest" in out
        assert "search" in out
        assert "stats" in out

    def test_no_args_shows_help(self) -> None:
        """Sin argumentos, typer muestra help (no_args_is_help=True)."""
        result = runner.invoke(app, [])
        assert "ingest" in result.output
        assert "search" in result.output


class TestIngestLock:
    def test_second_acquire_is_blocked(self, tmp_path):
        from memex.cli.main import _acquire_ingest_lock

        db = tmp_path / "memex.db"
        first = _acquire_ingest_lock(db)
        second = _acquire_ingest_lock(db)
        assert first is not None
        assert second is None  # a second ingest must not run concurrently

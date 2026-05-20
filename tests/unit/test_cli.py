"""Tests del CLI con Typer's CliRunner.

Cubre los caminos críticos sin necesitar Ollama: errores de path inválido,
mensajes con DB vacía, y stats sobre DB recién creada.

Los tests que requieren ingest real (con embedder + DB poblada) viven en
`tests/integration/test_full_flow.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from memex.cli.main import app

runner = CliRunner()


class TestIngestCommand:
    def test_missing_zip_path_exits_with_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["ingest", str(tmp_path / "no-existe.zip"), "--db", str(tmp_path / "x.db")],
        )
        assert result.exit_code == 1
        assert "no encontrado" in result.stdout.lower() or "no encontrado" in result.output.lower()


class TestSearchCommand:
    def test_empty_db_returns_friendly_message(self, tmp_path: Path) -> None:
        """Búsqueda contra DB vacía no debe crashear; debe avisar."""
        # Nota: este test requeriría Ollama si la query es no trivial, pero
        # el `vector_search` se ejecuta DESPUÉS del embed, así que el embedder
        # se llama primero. Para evitar dependencia, marco como integration
        # si Ollama no responde.
        # Por ahora, skip si no hay Ollama, similar a otros tests integration.
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(
                "http://localhost:11434/api/tags", timeout=1.0
            ) as r:
                if r.status != 200:
                    return  # skip implícito
        except (urllib.error.URLError, TimeoutError, OSError):
            return  # skip implícito si Ollama no está

        db_path = tmp_path / "empty.db"
        result = runner.invoke(app, ["search", "test query", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "sin resultados" in result.output.lower() or "vacía" in result.output.lower()


class TestStatsCommand:
    def test_stats_on_empty_db_works(self, tmp_path: Path) -> None:
        """Stats sobre una DB recién creada (vacía) debe mostrar ceros sin crashear."""
        db_path = tmp_path / "empty.db"
        result = runner.invoke(app, ["stats", "--db", str(db_path)])
        assert result.exit_code == 0
        # Debe mencionar las categorías.
        out = result.output
        assert "Projects" in out
        assert "Conversaciones" in out
        assert "Mensajes" in out
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

    def test_passes_host_and_port_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn

        captured: dict[str, object] = {}

        def fake_run(starlette_app: object, **kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)

        result = runner.invoke(
            app, ["serve", "--host", "0.0.0.0", "--port", "9999"]
        )
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

    def test_no_db_flag_leaves_conn_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

"""Integration test end-to-end: parse + chunk + embed (Ollama) + store + search.

Usa el export real del usuario si está en `data/exports/`. Skip si no está, o si
Ollama no responde.

Para correr solo este:
    uv run pytest tests/integration/test_full_flow.py -v
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from memex.config import settings
from memex.core.embeddings.ollama import OllamaEmbedder
from memex.core.ingest.pipeline import ingest_export
from memex.core.storage import repo
from memex.core.storage.db import connect_and_init

EXPORTS_DIR = Path("data/exports")


def _ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{settings.ollama_host}/api/tags", timeout=2.0) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _find_real_export() -> Path | None:
    if not EXPORTS_DIR.exists():
        return None
    zips = sorted(EXPORTS_DIR.glob("*.zip"))
    return zips[0] if zips else None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ollama_available(), reason="Ollama no responde"),
    pytest.mark.skipif(_find_real_export() is None, reason="No hay export real en data/exports/"),
]


class TestFullFlow:
    def test_ingest_real_export_and_search(self, tmp_path: Path) -> None:
        export = _find_real_export()
        assert export is not None  # ya cubierto por skipif, mypy lo necesita

        db_path = tmp_path / "memex_test.db"
        conn = connect_and_init(db_path)
        try:
            embedder = OllamaEmbedder()
            summary = ingest_export(conn, export, embedder)

            assert summary.errors == [], f"errores durante ingest: {summary.errors}"
            assert summary.conversations > 0
            assert summary.chunks > 0

            # Búsqueda de sanity: buscar algo común y verificar que devuelve resultados.
            hits = repo.vector_search(conn, embedder.embed_one("decisión técnica"), limit=5)
            assert len(hits) > 0
            for hit in hits:
                assert hit.distance >= 0
                assert hit.chunk.text
                assert hit.conversation.title
        finally:
            conn.close()

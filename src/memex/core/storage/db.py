"""Conexión y bootstrap de la base SQLite + sqlite-vec.

Funciones clave:
- `get_connection(path)`: abre una conexión con la extensión sqlite-vec cargada,
  foreign keys habilitadas y journal en WAL.
- `init_schema(conn)`: aplica `schema.sql` (idempotente, usa IF NOT EXISTS).
- `connect_and_init(path)`: helper de uso común.

`PRAGMA foreign_keys` es per-conexión, así que se setea acá en cada conexión nueva,
no en el SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from memex.config import settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Abre una conexión SQLite con extensiones y PRAGMAs listas.

    Si `db_path` es None, usa el valor de `settings.db_path`.
    Crea el directorio padre si no existe.
    Pasa `:memory:` para una base in-memory (útil en tests).
    """
    if db_path is None:
        target: Path | str = settings.db_path
    else:
        target = db_path

    if isinstance(target, Path) or (isinstance(target, str) and target != ":memory:"):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        connect_target: str = str(path)
    else:
        connect_target = ":memory:"

    conn = sqlite3.connect(connect_target, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)

    # PRAGMAs son per-conexión.
    conn.execute("PRAGMA foreign_keys = ON")
    if connect_target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Aplica `schema.sql`. Idempotente: todos los DDL usan IF NOT EXISTS."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)


def connect_and_init(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Atajo: abre conexión y aplica schema. Devuelve la conexión lista para usar."""
    conn = get_connection(db_path)
    init_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> str | None:
    """Devuelve la versión del schema registrada en `schema_meta`."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return row[0] if row else None

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


def get_connection(
    db_path: Path | str | None = None,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Abre una conexión SQLite con extensiones y PRAGMAs listas.

    Si `db_path` es None, usa el valor de `settings.db_path`.
    Crea el directorio padre si no existe.
    Pasa `:memory:` para una base in-memory (útil en tests).

    `check_same_thread=False` deshabilita el check thread-safety de Python para
    casos como el HTTP server (Starlette corre handlers en thread pool, lo que
    rompería una conn creada en otro thread). El usuario es responsable de no
    ejecutar queries concurrentes sobre la misma conn. SQLite mismo es
    thread-safe a nivel C; el check del cliente Python es lo que se relaja.
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

    conn = sqlite3.connect(
        connect_target,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=check_same_thread,
    )
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
    """Aplica `schema.sql` y migraciones aditivas idempotentes.

    El `schema.sql` cubre fresh installs (todos los DDL usan IF NOT EXISTS).
    Para bases pre-existentes que se actualizan a un schema nuevo, las
    migraciones aditivas viven en `_apply_additive_migrations` (corre después
    del script y suma columnas que el CREATE TABLE no aplicó porque la tabla
    ya existía).
    """
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _apply_additive_migrations(conn)


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN para columnas opcionales agregadas post-v1.

    SQLite no soporta `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, así que
    inspeccionamos `pragma_table_info` antes de cada ADD. Idempotente: re-correr
    no rompe ni duplica trabajo.
    """
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM pragma_table_info('conversations')").fetchall()
    }
    if "content_hash" not in existing:
        conn.execute("ALTER TABLE conversations ADD COLUMN content_hash TEXT")


def connect_and_init(
    db_path: Path | str | None = None,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Atajo: abre conexión y aplica schema. Devuelve la conexión lista para usar.

    Ver `get_connection` para el detalle de `check_same_thread`.
    """
    conn = get_connection(db_path, check_same_thread=check_same_thread)
    init_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> str | None:
    """Devuelve la versión del schema registrada en `schema_meta`."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return row[0] if row else None

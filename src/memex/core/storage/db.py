"""SQLite + sqlite-vec connection and bootstrap.

Key functions:
- `get_connection(path)`: open a connection with the sqlite-vec extension
  loaded, foreign keys enabled, and journal in WAL.
- `init_schema(conn)`: apply `schema.sql` (idempotent, uses IF NOT EXISTS).
- `connect_and_init(path)`: common-use helper.

`PRAGMA foreign_keys` is per-connection, so it is set here on every new
connection, not in the SQL.
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
    """Open a SQLite connection with extensions and PRAGMAs ready.

    If `db_path` is None, uses the value of `settings.db_path`.
    Creates the parent directory if it does not exist.
    Pass `:memory:` for an in-memory DB (useful in tests).

    `check_same_thread=False` disables Python's thread-safety check for
    cases like the HTTP server (Starlette runs handlers in a thread
    pool, which would break a conn created in another thread). The user
    is responsible for not running concurrent queries on the same conn.
    SQLite itself is thread-safe at the C level; only the Python
    client's check is relaxed.
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

    # PRAGMAs are per-connection.
    conn.execute("PRAGMA foreign_keys = ON")
    if connect_target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply `schema.sql` and idempotent additive migrations.

    `schema.sql` covers fresh installs (all DDL uses IF NOT EXISTS). For
    pre-existing DBs being upgraded to a newer schema, additive
    migrations live in `_apply_additive_migrations` (runs after the
    script and adds columns that the CREATE TABLE did not apply because
    the table already existed).
    """
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _apply_additive_migrations(conn)


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN for optional columns added post-v1.

    SQLite does not support `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
    so we inspect `pragma_table_info` before each ADD. Idempotent:
    re-running does not break or duplicate work.
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
    """Shortcut: open a connection and apply the schema. Returns a ready connection.

    See `get_connection` for `check_same_thread` details.
    """
    conn = get_connection(db_path, check_same_thread=check_same_thread)
    init_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> str | None:
    """Return the schema version registered in `schema_meta`."""
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return row[0] if row else None

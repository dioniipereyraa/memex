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

import contextlib
import os
import sqlite3
from pathlib import Path

import sqlite_vec

from memex.config import settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# The DB (and its -wal/-shm sidecars) hold the user's full chat history in
# plaintext. Create it user-only so other local accounts cannot read it on a
# shared host. SQLite gives the sidecars the same mode as the main file, so
# restricting the main file on creation is enough.
_DB_FILE_MODE = 0o600
_DB_DIR_MODE = 0o700


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

    file_is_new = False
    if isinstance(target, Path) or (isinstance(target, str) and target != ":memory:"):
        path = Path(target)
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock down the directory only if WE just created it, so we do not
        # fight an admin who deliberately widened a pre-existing data dir.
        if not parent_existed:
            with contextlib.suppress(OSError):
                os.chmod(path.parent, _DB_DIR_MODE)
        file_is_new = not path.exists()
        connect_target: str = str(path)
    else:
        connect_target = ":memory:"

    conn = sqlite3.connect(
        connect_target,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row

    # Restrict the freshly-created DB file before any data is written (so the
    # WAL/SHM sidecars inherit 0600 too). No-op on Windows where chmod is
    # advisory. Guarded so a read-only FS or odd ownership never breaks open.
    if connect_target != ":memory:" and file_is_new:
        with contextlib.suppress(OSError):
            os.chmod(connect_target, _DB_FILE_MODE)

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
    # Explicit busy timeout: under WAL only one writer holds the DB at a time.
    # `memex serve` (ingest) and `memex-mcp` (lazy-summary write) are separate
    # processes, so a write can briefly contend. Wait instead of failing fast.
    # CPython's sqlite3 default happens to be 5000ms; set it explicitly so it
    # does not silently change and so the value is intentional and documented.
    # 30s: serve + serve-remote + the 15-min scheduled ingest + the SessionEnd
    # hook can all write to the same DB; a bulk ingest can hold the write lock
    # for several seconds while embedding, so give contenders room to wait
    # rather than fail with SQLITE_BUSY.
    conn.execute("PRAGMA busy_timeout = 30000")

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

    # content_hash must exist before the table is recreated below (the migration
    # copies it column-by-column), so run the source-CHECK migration last.
    _migrate_conversations_source_check(conn)


def _migrate_conversations_source_check(conn: sqlite3.Connection) -> None:
    """Widen the `conversations.source` CHECK to allow new sources.

    SQLite cannot ALTER a CHECK constraint in place, so for a pre-existing DB
    whose `conversations` table predates a new source value we recreate the
    table (data + indexes preserved) following SQLite's documented
    table-redefinition procedure. Idempotent: a DB already carrying the widened
    CHECK (or a fresh one created from `schema.sql`) is left untouched.

    FKs that reference `conversations` (messages, chunks, chat_repos) use
    ON DELETE CASCADE, so `foreign_keys` is toggled OFF for the swap to keep
    the DROP from cascading. Integrity is preserved structurally: rows are
    copied wholesale with their original keys and the child tables are never
    touched, so no FK target changes.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversations'"
    ).fetchone()
    if row is None:
        return  # fresh DB: schema.sql already created it with the current CHECK
    ddl = row["sql"] or ""
    if "claude_code" in ddl:
        return  # already migrated

    # Columns of the new table, in order. Copy only those the old table also has
    # so very old DBs (predating `ingested_at` / `content_hash`) still migrate;
    # the omitted ones fall back to their DEFAULT / NULL.
    new_cols = [
        "uuid",
        "title",
        "summary",
        "source",
        "project_uuid",
        "account_uuid",
        "created_at",
        "updated_at",
        "ingested_at",
        "content_hash",
    ]
    old_cols = {
        r["name"] for r in conn.execute("SELECT name FROM pragma_table_info('conversations')")
    }
    copy = [c for c in new_cols if c in old_cols]
    col_csv = ", ".join(copy)

    # `PRAGMA foreign_keys` is a no-op inside a transaction, so commit any
    # pending work first to guarantee the OFF takes effect during the swap.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            f"""
            BEGIN;
            DROP TABLE IF EXISTS conversations_new;
            CREATE TABLE IF NOT EXISTS conversations_new (
                uuid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT NOT NULL CHECK (
                    source IN ('conversations', 'design_chat', 'memory', 'claude_code')
                ),
                project_uuid TEXT REFERENCES projects(uuid) ON DELETE SET NULL,
                account_uuid TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                content_hash TEXT
            ) STRICT;
            INSERT INTO conversations_new ({col_csv})
                SELECT {col_csv} FROM conversations;
            DROP TABLE conversations;
            ALTER TABLE conversations_new RENAME TO conversations;
            CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                ON conversations(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_project
                ON conversations(project_uuid);
            CREATE INDEX IF NOT EXISTS idx_conversations_source
                ON conversations(source);
            COMMIT;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


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

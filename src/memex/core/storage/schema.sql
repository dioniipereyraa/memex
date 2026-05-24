-- Memex schema v1
-- Tablas STRICT para projects, conversations, messages y chunks.
-- Virtual table vec_chunks (sqlite-vec) para vector search.
--
-- Convenciones:
--   - timestamps en TEXT con formato ISO 8601, hora UTC con sufijo Z.
--   - booleans como INTEGER (0/1) por compatibilidad con SQLite.
--   - UUIDs como TEXT.
--
-- La carga de la extensión sqlite-vec se hace en `db.py` antes de aplicar esto.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '1');

-- Projects de Claude.ai. Pueden tener prompt_template (system prompt persistente).
CREATE TABLE IF NOT EXISTS projects (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    prompt_template TEXT,
    is_private INTEGER NOT NULL DEFAULT 1,
    is_starter_project INTEGER NOT NULL DEFAULT 0,
    creator_uuid TEXT,
    creator_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- Conversaciones: las tres fuentes (conversations.json, design_chats, memories) se
-- unifican aquí. `project_uuid` solo aplica para design_chats.
CREATE TABLE IF NOT EXISTS conversations (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT NOT NULL CHECK (source IN ('conversations', 'design_chat', 'memory')),
    project_uuid TEXT REFERENCES projects(uuid) ON DELETE SET NULL,
    account_uuid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- SHA-256 (hex) del texto canónico al momento del último ingest. Permite
    -- detectar si el contenido cambió y decidir regen de derivados (summary).
    -- Nullable porque (1) bases pre-existentes no lo tenían, (2) ingests sin
    -- summary feature lo dejan vacío.
    content_hash TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_uuid);
CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);

-- Mensajes. `raw_content` es JSON serializado del content[] original.
-- `text` es la versión renderizada (incluye markers de tool blocks cuando aplica).
CREATE TABLE IF NOT EXISTS messages (
    uuid TEXT PRIMARY KEY,
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid) ON DELETE CASCADE,
    parent_uuid TEXT,
    sender TEXT NOT NULL CHECK (sender IN ('human', 'assistant', 'system')),
    text TEXT NOT NULL DEFAULT '',
    raw_content TEXT,
    has_tool_use INTEGER NOT NULL DEFAULT 0,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_uuid, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_uuid);

-- Chunks: unidades de retrieval. char_start/end indexan el texto concatenado de la
-- conversación, para poder remapear a mensajes después si hace falta.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid) ON DELETE CASCADE,
    message_uuid TEXT REFERENCES messages(uuid) ON DELETE SET NULL,
    sender TEXT,
    text TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_chunks_conversation ON chunks(conversation_uuid);
CREATE INDEX IF NOT EXISTS idx_chunks_created_at ON chunks(created_at);

-- Tabla virtual sqlite-vec. rowid se sincroniza con chunks.id en el repo.
-- Dimensión 768 = nomic-embed-text. Si se cambia el modelo, hay que recrear esto.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding FLOAT[768]
);

-- Tabla virtual FTS5 para búsqueda lexical sobre el texto de los chunks.
-- Sirve a `repo.text_search` (BM25) y como mitad lexical de `repo.hybrid_search`
-- (RRF de vector + texto). Resuelve el caso "Amarok" (proper nouns raros que
-- la búsqueda semántica pura no atrapa).
--
-- rowid se sincroniza con chunks.id, igual que vec_chunks. El repo mantiene
-- las tres tablas en sync (add_chunk inserta en las tres,
-- delete_chunks_for_conversation borra de las tres).
--
-- Tokenizer: unicode61 con remove_diacritics=2 para que "amarok" matchee
-- "Amarók", "AMAROK", etc. Importante para mezcla ES/EN del corpus.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Repos conocidos por Memex. Cada uno con una clave canónica estable que el
-- matcher genera al registrar (`memex repos add <path>`). El path absoluto
-- canonicalizado (forward slashes, lowercase en Windows) es la clave por
-- default; si hay remote URL del git, se guarda como columna separada.
--
-- name = nombre amigable (último segmento del path o manifest.name).
-- manifest_name = `name` del pyproject.toml / package.json / Cargo.toml si
-- existe; nullable. Lo usa el matcher como una de las señales para asociar.
CREATE TABLE IF NOT EXISTS repos (
    key TEXT PRIMARY KEY,
    path TEXT,
    remote_url TEXT,
    name TEXT NOT NULL,
    manifest_name TEXT,
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- Many-to-many entre conversaciones y repos. Una conv puede asociarse a más
-- de un repo (el típico caso: chat que tocó dos repos relacionados). Cada
-- asociación lleva un `source` (auto = detectada por el matcher al ingest;
-- manual = el user lo etiquetó con `memex tag`) y un `confidence` 0.0-1.0
-- que se usa para boost en `search_chats(repo=...)`.
CREATE TABLE IF NOT EXISTS chat_repos (
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid) ON DELETE CASCADE,
    repo_key TEXT NOT NULL REFERENCES repos(key) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    confidence REAL,
    associated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (conversation_uuid, repo_key)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_chat_repos_repo ON chat_repos(repo_key);
CREATE INDEX IF NOT EXISTS idx_chat_repos_conv ON chat_repos(conversation_uuid);

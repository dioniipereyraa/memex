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
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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

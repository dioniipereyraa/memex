# Devlog

Bitácora corta, cronología inversa. Una entrada por sesión sustantiva.

Formato: fecha, qué se hizo, decisiones, bloqueos, próximo paso.

---

## 2026-05-15 — Inspección del export oficial

**Qué se hizo:**
- Script `scripts/inspect_export.py` que abre el zip sin extraerlo, recorre los JSON y reporta esquema y estadísticas. Read-only, no leakea contenido (texto se redacta como `<str:N chars>`).
- Inspección completa del export real (1.71 MB).

**Hallazgos (sobre el export real, contenido redactado):**
- 12 archivos en el zip: `users.json`, `memories.json`, 2 `projects/*.json`, 7 `design_chats/*.json`, `conversations.json` (5.9 MB).
- Total indexable: 73 chats (66 sueltos + 7 dentro de projects), 900 mensajes.
- Schema de mensaje: `uuid`, `text` (legacy), `content[]` (nuevo), `sender` (human/assistant), `created_at`, `updated_at`, `attachments`, `files`, `parent_message_uuid`.
- `content[].type`: `text` (1015), `tool_use` (246), `tool_result` (245). Los tool blocks vienen con metadata de integraciones (Slack, GitHub, MCP servers).
- `text` y `content[].text` conviven en 876 de 900 mensajes. Diferencia media 19 chars (probablemente separadores entre blocks). Tomamos `content` como canónico.
- No hay forks/branches: cada mensaje tiene exactamente un parent. `parent_message_uuid` queda en el modelo por si exports futuros traen tree.
- `summary` viene poblado en cada conversación (mean 1067 chars). Resúmenes auto-generados por Claude.ai, gratis. Anticipa Fase 3.
- Mediana de mensaje: 223 chars (~55 tokens). Mediana de chat: 3138 chars (~785 tokens). Max chat: 132k chars.
- `memories.json` trae la memoria curada de Anthropic (3634 chars en `conversations_memory`). El handoff decía que estaba aislada en Claude.ai; con el export oficial la tenemos en disco.

**Decisiones (tomadas con el usuario):**
- Indexar `memories.json` como conversación sintética con `source='memory'`. Entra al mismo pipeline.
- Tool blocks se renderizan como texto plano con markers (`[tool_use: <name>] <input>`, `[result] <texto>`). Conserva contexto sin parsing complejo.
- Tabla `projects` separada con FK desde `conversations`. Permite recuperar `prompt_template` y futuras tools tipo `list_projects()`.
- Chunking: ~500 tokens con overlap, por conversación (lo del plan original confirmado tras ver el tamaño real de los chats).

**Schema base (cuatro tablas + virtual vec):**
- `projects` (uuid, name, prompt_template, creator, timestamps)
- `conversations` (uuid, title, summary, source, project_uuid FK, account_uuid, timestamps)
- `messages` (uuid, conversation_uuid, parent_uuid, sender, text, raw_content JSON, has_tool_use, has_attachments, timestamps)
- `chunks` (id, conversation_uuid, message_uuid, sender, text, char_start, char_end, created_at)
- `vec_chunks` (chunk_id, embedding FLOAT[768]) virtual table de sqlite-vec

**Próximo paso:**
- Implementar `core/models.py` con pydantic (Project, Conversation, Message, Chunk, SearchResult) y `core/storage/schema.sql` con las tablas y los índices. Ese es el bottleneck del que dependen ingest, embeddings y retrieval; queda en `main`. Después, abrir los tres worktrees paralelos.

---

## 2026-05-15 — Arranque del repo

**Qué se hizo:**
- Spin-off de SyncChat. Repo nuevo en `d:\Dionisio\Memex`.
- Lectura del handoff doc (excluido del repo público vía `.gitignore`).
- Plan completo aprobado: estructura, stack, fases, criterios de cierre.
- Setup base: `.gitignore`, `pyproject.toml`, `.env.example`, `.python-version`, estructura de carpetas (`src/memex/{core,transports,cli}`, `tests/{unit,integration}`, `data/exports`, `scripts`).
- README, ROADMAP y este DEVLOG escritos con tono práctico y conciso.
- Movimiento del export oficial de Claude.ai a `data/exports/` (gitignored).
- Inicialización de git e historia inicial limpia.

**Decisiones de esta sesión:**
- Stack: Python 3.13 (3.12+ soportado), uv como package manager, FastMCP, sqlite-vec, Ollama local con `nomic-embed-text`.
- Repo público desde el día 1, nombre `memex`.
- Multi-Claude vía git worktrees (no solo branches). División de Fase 0: schema en main primero, después tres worktrees paralelos (ingest, embeddings, retrieval+cli).
- Auditoría completa de bugs, código obsoleto y vulnerabilidades al cierre de cada fase.

**Bloqueos / notas:**
- Ollama instalado pero todavía no verificado vía CLI (PATH no refrescado, no urge hasta empezar a escribir el embedder).

**Próximo paso:**
- Fase 0, primera tarea: inspeccionar el JSON export de Claude.ai para definir el schema y el modelo de datos sobre datos reales.

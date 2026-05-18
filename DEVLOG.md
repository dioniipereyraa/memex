# Devlog

Bitácora corta, cronología inversa. Una entrada por sesión sustantiva.

Formato: fecha, qué se hizo, decisiones, bloqueos, próximo paso.

---

## 2026-05-18 — Módulo de ingest: renderer, chunker, parsers

**Qué se hizo:**
- `core/ingest/content_renderer.py`: convierte `content[]` (con bloques `text`, `tool_use`, `tool_result`) a texto plano. Tool blocks van como markers (`[tool_use: <name>] <input>`, `[result] <texto>`, `[result error] ...`). Truncado a `MAX_TOOL_INPUT_CHARS=500` y `MAX_TOOL_RESULT_CHARS=1000`. Bloques desconocidos se ignoran (deja la puerta abierta a tipos nuevos).
- `core/ingest/chunker.py`: char-based con factor `chars_per_token` configurable (default 4). Devuelve `list[ChunkSpan]` con `(text, char_start, char_end)`. `text[char_start:char_end] == text` siempre. Validación de parámetros con `ValueError`.
- `core/ingest/claude_export.py`: 4 parsers (`parse_project`, `parse_conversations_list`, `parse_design_chat`, `parse_memories`). Helpers privados unifican `conversations.json` y `design_chats/*.json`. La memoria curada se sintetiza como conversación con `uuid='memory-<account_uuid>'` (idempotente entre re-ingests).
- 53 tests unitarios nuevos (21 renderer, 13 chunker, 19 export), 82 totales verdes.

**Bug cazado por smoke test sobre el export real:**
- Algunos mensajes en `design_chats/*.json` no traen `updated_at`. Era `KeyError`. Fallback a `created_at`. Test agregado para no regresionar.

**Smoke test sobre el corpus real (sin imprimir contenido):**
- 2 projects parseados (1 starter vacío, 1 con `prompt_template` de 819 chars).
- 66 conversaciones sueltas con 900 mensajes, 58 con tool_use rendereados con markers.
- 7 design_chats con 123 mensajes, todos correctamente linkeados a su project (project_uuid presente).
- Memoria curada parseada (3634 chars) con uuid sintético estable.
- Total ingestable: 74 conversaciones, 1024 mensajes.

**Decisiones de implementación:**
- Char-based chunking, no token-based. Más simple, sin dependencia tokenizer, configurable via `chars_per_token`. Si los resultados de retrieval son pobres en Fase 0 se cambia.
- Renderer ignora bloques con `type` desconocido en vez de fallar. Robustez frente a cambios futuros del export.
- Sender desconocido cae a HUMAN (defensivo).
- `parse_memories` recibe `now` opcional para tests deterministas; en prod usa `datetime.now(UTC)`.

**Estado:**
- `uv run pytest tests/unit`: 82 passed.
- `uv run ruff check src tests scripts`: clean.
- `uv run mypy src/memex/core src/memex/config.py`: clean.
- Smoke test sobre el export real: todo parseado sin errores.

**Próximo paso:**
- Módulo de embeddings: cliente Ollama + interfaz `Embedder`. Tras eso, el orquestador end-to-end (parse → chunk → embed → store) + CLI.

---

## 2026-05-18 — Storage layer: models, schema, db, repo

**Qué se hizo:**
- `src/memex/config.py` con `pydantic-settings`. Lee `.env` y env vars. Alias por env var (OLLAMA_HOST, MEMEX_EMBED_MODEL, MEMEX_DB_PATH, MEMEX_CHUNK_SIZE, etc.). Validación de rangos en chunk_size y chunk_overlap.
- `src/memex/core/models.py` con pydantic v2: `Project`, `Conversation` (con `source` enum), `Message` (con `raw_content` y flags), `Chunk`, `SearchHit`. Enums `Source` y `Sender` como `StrEnum`. `extra="forbid"` para que un campo inesperado falle temprano.
- `src/memex/core/storage/schema.sql`: 4 tablas STRICT (`projects`, `conversations`, `messages`, `chunks`) + virtual table `vec_chunks` (sqlite-vec) + `schema_meta` para versionado. FKs con `ON DELETE CASCADE` en mensajes/chunks por conversation, `ON DELETE SET NULL` en project_uuid y message_uuid. CHECK constraints en `source` y `sender`. Índices en updated_at, project_uuid, conversation_uuid y created_at.
- `src/memex/core/storage/db.py`: `get_connection()` carga sqlite-vec, setea `foreign_keys=ON`, `journal_mode=WAL` y `synchronous=NORMAL`. `init_schema()` idempotente. `connect_and_init()` como atajo.
- `src/memex/core/storage/repo.py`: CRUD funcional (sin clases) con upserts (`ON CONFLICT DO UPDATE`). `add_chunk()` inserta el chunk y su embedding atómicamente (mismo rowid en chunks.id y vec_chunks.rowid). `vector_search()` hace KNN join con `MATCH ? AND k = ?`.
- Tests: `tests/conftest.py` con fixtures (db in-memory, project, conversation, messages, chunks). `tests/unit/test_models.py` (11 tests) y `tests/unit/test_storage.py` (17 tests). 28 tests pasan.

**Bugs encontrados y corregidos en el camino:**
- vec0 KNN no acepta `LIMIT ?` parametrizado cuando hay JOINs. Hay que usar `k = ?` en el WHERE. La query de `vector_search` ya lo refleja.
- El fixture `chunk` tenía `message_uuid` apuntando a un mensaje que el test no insertaba. Se separó en dos fixtures (`chunk` sin mensaje, `chunk_with_message` con). Agregado test que prueba el FK rechaza orphans.
- Ruff señaló `class X(str, Enum)` (legacy) y `timezone.utc` (legacy en 3.11+). Migrado a `StrEnum` y `datetime.UTC`.

**Decisiones de implementación:**
- Repo como funciones, no clases. Más simple, sin estado que mantener, sin DI complicada.
- Datetime serializado con sufijo `Z` (no `+00:00`) para mantener compatibilidad con el formato del export oficial de Claude.ai.
- `raw_content` se guarda como JSON en TEXT. Lo deserializa el repo al leer. Permite analizar tool blocks después sin re-parsear el export.
- L2 como métrica de distancia. nomic-embed-text devuelve embeddings normalizables, así que L2 ranking coincide con cosine ranking.

**Estado:**
- `uv run pytest tests/unit`: 28 passed.
- `uv run ruff check src tests scripts`: clean.
- `uv run mypy src/memex/core src/memex/config.py`: clean.

**Próximo paso:**
- Schema y modelos cerrados, el bottleneck de `main` se levanta. Ahora se pueden abrir los tres worktrees paralelos:
  - `feature/ingest`: parser de `conversations.json`, `design_chats/*.json`, `memories.json`, `projects/*.json` + chunker + content renderer (tool markers).
  - `feature/embeddings`: cliente Ollama + interfaz `Embedder`.
  - `feature/retrieval-cli`: tool de búsqueda (envuelve `repo.vector_search`) + CLI con `typer`.

---

## 2026-05-18 — Inspección del export oficial

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

## 2026-05-18 — Arranque del repo

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

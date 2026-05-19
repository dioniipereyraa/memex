# Devlog

Bitácora corta, cronología inversa. Una entrada por sesión sustantiva.

Formato: fecha, qué se hizo, decisiones, bloqueos, próximo paso.

---

## 2026-05-19 — Fase 2 sub-task: búsqueda híbrida FTS5 + RRF

**Contexto:** primera tarea de Fase 2 es resolver el caso "Amarok" antes de captura en vivo. El usuario eligió priorizar calidad de retrieval sobre volumen de datos.

**Qué se hizo:**
- `schema.sql`: nueva virtual table `fts_chunks` con FTS5, tokenizer `unicode61 remove_diacritics 2` (matchea "amarok" con "Amarók", "AMAROK", etc.). Comentarios explicando cómo se sincroniza con `chunks` y `vec_chunks`.
- `repo.add_chunk` y `repo.delete_chunks_for_conversation`: sincronizan ahora las TRES tablas (chunks + vec_chunks + fts_chunks). Igual patrón de DELETE + INSERT que ya tenía vec_chunks.
- `repo.text_search(conn, query, limit, dedupe_by_conversation)`: BM25 sobre fts_chunks. Sanitiza la query con `_sanitize_fts_query` (extrae palabras `\w+` y las quotea para evitar operadores FTS5 sueltos). Si la query es malformada devuelve lista vacía en lugar de propagar `OperationalError`.
- `repo.hybrid_search(conn, query, query_embedding, limit, ..., rrf_k=60)`: combina `vector_search` + `text_search` con Reciprocal Rank Fusion. Score = Σ 1/(rrf_k+rank). Default k=60 (Cormack 2009). Resultado: `SearchHit.distance = -rrf_score` para mantener "menor = mejor".
- `repo.rebuild_fts_index(conn)`: helper de mantenimiento. Borra y repuebla `fts_chunks` desde `chunks`. **Commitea al final** (es operación auto-contenida, no parte de transacción larga).
- `tools.search_chats`: nuevo parámetro `mode: "hybrid" | "semantic" | "lexical"`, default `"hybrid"`. Modo lexical no llama al embedder (skip Ollama).
- `stdio.search_chats` (wrapper MCP): expone `mode` con docstring que aclara cuándo conviene cada uno (Claude lo va a usar para decidir).
- CLI: `memex search --mode {hybrid|semantic|lexical}` + nuevo comando `memex reindex-fts` para poblar el índice sobre bases pre-existentes sin re-embedear.
- Tests nuevos (12): cobertura de `text_search` (incluyendo dedup, sanitización de queries con caracteres especiales, case-insensitive, rebuild), `hybrid_search` (rescate cuando solo matchea text, dedup), y `tools.search_chats` (mode inválido, default hybrid, lexical skip embedder).

**Bug real cazado durante validación en vivo:**
- `rebuild_fts_index` no commiteaba. La CLI ejecutaba el INSERT, reportaba "614 chunks indexados", pero `conn.close()` hacía rollback y el índice quedaba vacío. Fix: commit explícito al final de la función. Documenté el por qué (operación auto-contenida, distinta del patrón "caller commits" del resto del repo).

**Validación end-to-end sobre el corpus real (614 chunks):**

| Modo | Top-3 para "Amarok" |
|---|---|
| `semantic` (antes el único disponible) | Exportal (0.84), Probadno random (0.88), Matemática (0.88) — **FALLA** |
| `lexical` (FTS5 puro) | **"Desbloquear radio Amarok 2012 con VCDS" (-8.6)** — ÚNICO match |
| `hybrid` (RRF combinado) | Exportal (-0.0164), **Amarok (-0.0164)**, Probadno (-0.0159) — **ARREGLADO** |

Sin regresión en búsquedas semánticas previas: "Chrome extension para exportar chats" devuelve el mismo top-3 que antes (en híbrido, el #1 tiene casi el doble de score que los siguientes por sumar señal de FTS).

**Estado:**
- `uv run pytest tests/unit`: 165 passed (era 153, +12).
- `uv run ruff check`, `uv run mypy`: clean.
- `memex reindex-fts` funcional.
- `memex search "Amarok" --mode hybrid`: devuelve el chat correcto en top-2.

**Pendiente para cerrar Fase 2:**
- Captura en vivo: adaptar Chrome ext de SyncChat para escribir al mismo SQLite. Endpoint local de ingest. Idempotencia.

---

## 2026-05-18 — Cierre de Fase 1: auditoría + sync de docs

**Auditoría de cierre (sub-agent):**

Sin bloqueantes. Veredicto: cierra como está. Items accionables encontrados:

1. **Dead code**: `except EmbedderError` en `stdio.search_chats` nunca se ejecuta porque `tools.search_chats` ya atrapa la excepción y devuelve `{"error": ...}`. Fixed: removido del wrapper, dejado el `except Exception` general. También removido import inútil de `EmbedderError` en `stdio.py`.
2. **Docs desincronizadas (varios)**: arregladas.
   - `CLAUDE.md`: `transports/` decía `(PENDIENTE, Fase 1)`, ahora marca `tools.py` y `stdio.py` como DONE. Comentario obsoleto sobre `memex-mcp` actualizado.
   - `README.md`: descripción de `search_chats` mencionaba un parámetro fantasma `date_range?` (no existe; el real es `source`). Sacados párrafos "en construcción en Fase 1". Agregado `messages_limit`/`messages_offset` a la descripción de `get_chat`.
   - `ROADMAP.md`: test count desactualizado.
3. **Gaps de tests**: agregados 2 nuevos.
   - `test_embedder_error_becomes_json_error`: valida que `tools.search_chats` atrapa `EmbedderError` y lo convierte a `{"error": ...}`. Documenta el contrato que justifica haber sacado el `except` del stdio wrapper.
   - `test_offset_beyond_total_returns_empty`: valida que `get_chat` con `messages_offset >= total_messages` devuelve ventana vacía sin crashear, `truncated=False`.

**Follow-ups diferidos a Fase 4 (cuando arme remote MCP):**
- `stdio.py` devuelve `f"Error interno: {e}"` al cliente. Hoy es local single-user, riesgo bajo. En remote MCP conviene devolver mensaje genérico al cliente y dejar el detalle solo en el log para no leakear paths/queries.
- `OllamaEmbedder` detecta errores de conexión con un substring check (`"connect"`, `"refused"`, etc.). Frágil si `ollama` o `httpx` cambian el wording (por ejemplo en otro idioma). Mejor: catch explícito de `httpx.ConnectError` / `httpx.TimeoutException` antes del fallback por substring.

**Estado final de Fase 1:**
- 3 tools MCP funcionando en Claude Code real (validado por uso, no solo por tests).
- 153 unit + 7 integration tests verdes.
- `uv run ruff check`, `uv run mypy`: clean.
- Auditoría hecha, sin bloqueantes.
- Docs sincronizadas con el estado real del código.

**Fase 1 CERRADA.** Próximo: Fase 2 (captura en vivo via Chrome ext de SyncChat + búsqueda híbrida FTS5 + vectores para resolver el caso "Amarok").

---

## 2026-05-18 — Fix: get_chat excedía max-tokens de Claude Code

**Qué pasó:**
Después del primer commit de Fase 1, primera sesión real en Claude Code llamando `get_chat` sobre un chat de 32 mensajes (uuid `00ef7e7b-…`, "Exportal Companion extension") falló con `result (107.581 characters) exceeds maximum allowed tokens`. Claude Code derivó el resultado a un archivo aparte y tuvo que leerlo en chunks manualmente. UX rota para chats no triviales.

Esto era exactamente el riesgo que la auditoría de Fase 0 había anticipado y que dejé como "agrego pagination si pasa". Pasó en el primer chat real, no en uno extremo de 264 mensajes.

**Qué se hizo:**
1. `get_chat` ahora acepta `messages_limit` (default 20, max 100) y `messages_offset` (default 0). Permite a Claude paginar chats largos.
2. `get_chat` strippea `raw_content` (JSON de tool_use/tool_result blocks) de la respuesta siempre. Es ~10-30% del peso y rara vez se usa por Claude.
3. Cada `text` de mensaje se trunca a `GET_CHAT_MESSAGE_TEXT_MAX_CHARS=3000` con marker `…[truncated]`. Code dumps de Claude saltaban solos el límite.
4. La respuesta incluye `total_messages`, `messages_returned`, `truncated: bool`, `messages_offset` para que Claude sepa si hay más y cómo pedirlos.
5. `search_chats` ahora trunca el `summary` de cada resultado a `SEARCH_SUMMARY_MAX_CHARS=500`. Algunos summaries del export pesaban 2-3k chars y se acumulaban en respuestas de 5 resultados.
6. Helper `_truncate(s, max_chars)` agrega marker `…[truncated]` si recortó.
7. 8 tests nuevos: 6 de pagination en `get_chat`, 1 de raw_content stripped, 1 de summary truncation en `search_chats`.

**Validación post-fix:**
Llamando `get_chat` al mismo uuid de 32 mensajes que rompió antes:
- Tamaño: 31.6k chars (era 107.5k, **70% reducción**).
- `total_messages: 32`, `messages_returned: 20`, `truncated: true`. Claude puede pedir los otros 12 con `messages_offset=20`.
- `raw_content` ausente en cada mensaje.
- Texts intactos (ninguno excedía los 3000 chars individuales en este chat).

**Tests:**
- 151 unit tests verdes (era 143, +8).
- Ruff y mypy clean.

---

## 2026-05-18 — Fase 1 MVP: MCP server stdio

**Qué se hizo:**
- `src/memex/transports/tools.py`: implementaciones puras de las 3 tools (`search_chats`, `get_chat`, `list_recent_chats`). Toman `conn` y `embedder` por parámetro, devuelven dicts serializables. Sin dependencia de FastMCP, totalmente testables.
- `src/memex/transports/stdio.py`: FastMCP server con las 3 tools registradas via `@server.tool`. Conexión SQLite y `OllamaEmbedder` lazy singletons. `EmbedderError` se atrapa y se devuelve como `{"error": ...}` en JSON. Logging configurado a stderr (stdout reservado para JSON-RPC).
- `pyproject.toml`: re-agregado el script `memex-mcp = "memex.transports.stdio:main"` (estaba comentado desde la auditoría de Fase 0).
- `tests/unit/test_tools.py`: 17 tests de las funciones puras (queries vacías, source filter, ordering, errores).
- `tests/unit/test_stdio_server.py`: 6 tests del server MCP (3 tools registradas, call_tool funciona end-to-end, errores envueltos en JSON).
- `README.md`: agregado el snippet de configuración para Claude Code (`.mcp.json` con cwd absoluto).

**Bug real cazado por el smoke test:**
- `sqlite3.ProgrammingError`: SQLite objects son thread-bound. FastMCP por default corre las tools sync en un thread pool, así que nuestra conexión singleton fallaba al ser usada desde otro thread. Fix: `@server.tool(run_in_thread=False)` en cada tool. Las tools quedan corriendo en el event loop, lo cual es razonable porque son I/O cortas. Documentado el motivo en el docstring del módulo.

**Smoke test del MCP server (en proceso, no via JSON-RPC):**
- Las 3 tools quedan registradas con sus descripciones.
- `server.call_tool("list_recent_chats", {"limit": 3})` devuelve `ToolResult` con `TextContent` que contiene JSON válido y los chats reales de la base.
- `server.call_tool("get_chat", {"uuid": "no-existe"})` devuelve `{"error": ...}` sin crashear.
- `server.call_tool("search_chats", {"query": "  "})` devuelve error sin consultar Ollama.

**Decisiones:**
- Tools devuelven `str` (JSON pretty-printed) en lugar de dicts. Da control explícito del formato y evita serializaciones automáticas de FastMCP que podrían cambiar.
- Límites duros: `search` max 50 resultados, `list_recent_chats` max 100. Evita payloads enormes que sobrecarguen el contexto de Claude.
- `get_chat` no pagina; devuelve todos los mensajes. Si en uso real vemos chats de cientos de mensajes saturando, agregamos paginación. Por ahora over-engineering.
- `source` filter en `search_chats` se aplica en Python tras pedir 3x más candidatos a la DB. Bajo costo, evita complicar el SQL.

**Estado:**
- `uv run pytest tests/unit`: 143 passed (era 137, +6 nuevos del server).
- `uv run ruff check`, `uv run mypy`: clean.
- `uv run memex-mcp`: arranca limpio, registra las 3 tools.

**Próximo paso (criterio de cierre real de Fase 1):**
- Conectarlo a Claude Code via `.mcp.json` y usarlo en sesiones reales.
- 5 sesiones reales con al menos una tool invocada, sin crashes.
- Auditoría de cierre cuando se cumpla.

---

## 2026-05-18 — Cierre de Fase 0: dedup + auditoría

**Qué se hizo:**
- `repo.vector_search` ahora acepta `dedupe_by_conversation: bool = True` (default ON). Devuelve a lo sumo un chunk por conversación, el más cercano. Para conseguir N únicas pide `k = N * 5` a `vec_chunks` y dedupea en Python. Resuelve el problema UX visible en la validación: las búsquedas anteriores tenían 2-3 chunks del mismo chat ocupando puestos del top-5.
- 2 tests nuevos del dedup, más 2 directos para `delete_chunks_for_conversation`, más 6 tests del CLI con `typer.testing.CliRunner` (paths inválidos, DB vacía, help). Total: 112 unit tests verdes.
- Auditoría completa del proyecto pre-cierre (con sub-agent, en `tools/audit-fase0.md` mentalmente). Veredicto: cierra sin bloqueantes mayores, una sola cosa accionable inmediata.

**Bug crítico cazado por la auditoría:**
- `pyproject.toml` declaraba el script `memex-mcp = "memex.transports.stdio:main"` pero `transports/` solo tiene `__init__.py` vacío. Cualquier `uv run memex-mcp` reventaría con `ModuleNotFoundError`. Removido (comentado) hasta que Fase 1 implemente el transport stdio.

**Doc sync hecho:**
- `CLAUDE.md` describía `transports/{tools,stdio,http}.py` y `core/retrieval/` como módulos existentes; agregada anotación `(DONE)` / `(PENDIENTE, Fase 1)` por módulo + nota explicando que `vector_search` vive en `storage/repo.py` por simplicidad inicial.

**Validación final de retrieval (7 búsquedas reales sobre el corpus del usuario):**
| Query | Top-3 relevante | Distancia top-1 |
|---|---|---|
| "Chrome extension para exportar chats" | 3/3 | 0.62 |
| "decisión sobre arquitectura del proyecto" | 1/3 | 0.67 |
| "exportal" | 3/3 | 0.86 |
| "Amarok" | 0/3 (semántica falla con un proper noun raro) | 0.84 |
| "extension" | 3/3 | 0.81 |
| "conjunto de nivel 0" | 3/3 (matemática perfecto) | 0.72 |
| "clonar el proyecto en linux" | 2/3 | 0.75 |

**Pasa 6 de 7 (85%)**. Criterio de cierre era 7/10. Cierra con holgura.

**Limitación conocida:** búsqueda puramente semántica falla en proper nouns raros mencionados una sola vez (caso Amarok). Se va a resolver en Fase 2 con búsqueda híbrida (FTS5 + vectores + RRF).

**Follow-ups anotados (de la auditoría, no urgentes):**
- `settings = get_settings()` al importar `config.py` quedaría stale si tests cambian env vars post-import. Refactor menor para Fase 1 si hace falta.
- `pipeline._lookup_msg` es O(M*C) por conversación. Trivial hoy, podría doler con corpus 50x más grande.
- Streaming de `conversations.json` para evitar load de 50+ MB en memoria con corpus históricos grandes. Optimización para Fase 3.
- `OllamaEmbedder` no testea el caso "modelo no instalado" o "404 del servicio". Importante para Fase 1 (manejo de errores en MCP).
- `vector_search` con dim != 768 fallaría con error oscuro. Vale validar al inicio del search.

**Estado final de Fase 0:**
- 112 unit tests verdes, 7 integration tests verdes (Ollama real).
- `uv run ruff check`, `uv run mypy`: clean.
- CLI funcional: `ingest`, `search`, `stats`.
- Corpus indexado: 74 conversaciones, 1024 mensajes, 614 chunks.
- Retrieval valida con datos reales con calidad razonable.

**Fase 0 CERRADA.** Próximo: Fase 1 (MCP server stdio para Claude Code).

---

## 2026-05-18 — Pipeline end-to-end + CLI funcional

**Qué se hizo:**
- `core/ingest/pipeline.py`: orquestador completo. Toma zip + DB + Embedder, hace parse → render → chunk → embed → store. Orden: projects → design_chats → conversations → memories. Transacción por conversación (un error no rompe el resto). Idempotente vía upserts + `delete_chunks_for_conversation` antes de re-chunkear.
- `cli/main.py` con `typer` + `rich`: comandos `memex ingest <zip>`, `memex search "<query>" [-n N]`, `memex stats`. Tablas y output con colores.
- `repo.delete_chunks_for_conversation()`: helper para limpiar chunks viejos + sus vectores antes de re-ingest.
- 6 tests unitarios nuevos (pipeline end-to-end, idempotencia, FK orphan handling, etc.). Total: 103 unit tests verdes.
- `tests/integration/test_full_flow.py`: integration test que parsea el export real con OllamaEmbedder.

**Bug real cazado por smoke test sobre el corpus completo:**
- Los 7 design_chats apuntan a `project_uuid`s que NO están en `projects/*.json` del export (el usuario tiene projects que no fueron exportados). Fallaban con FK violation y se ingestaban con `errores=7`. Fix: si el `project_uuid` referenciado no existe, se setea a `None` antes del insert (orfanidad benigna). Test agregado para no regresionar.

**Smoke test contra el export real (1.71 MB, generación de embeddings ~1-2 min):**
- 2 projects, 74 conversaciones (66 sueltas + 7 design_chats + 1 memoria curada), 1024 mensajes, 614 chunks indexados, 147 mensajes vacíos saltados, **0 errores**.
- `memex search "Chrome extension para exportar chats"` → top-3 con distancias 0.67-0.69, devuelve exactamente las tres conversaciones del usuario sobre Exportal (su otro proyecto Chrome ext). El retrieval anda.
- `memex search "decision sobre stack tecnologico python o rust"` → distancia más alta (0.88), resultados menos precisos (es una query más vaga).
- `memex stats`: muestra distribución por source (conversations=66, design_chat=7, memory=1).

**Estado:**
- `uv run pytest tests/unit`: 103 passed.
- `uv run ruff check`, `uv run mypy`: clean.
- CLI funcional end-to-end con datos reales.

**Fase 0 lista para cerrar.** Quedaría evaluación formal (10 búsquedas representativas) y, si los resultados son satisfactorios, auditoría de cierre de fase y luego Fase 1 (MCP server).

---

## 2026-05-18 — Módulo de embeddings: interfaz + Ollama

**Qué se hizo:**
- `core/embeddings/base.py`: ABC `Embedder` con `dim`, `model_name`, `embed(texts)` y helper `embed_one(text)`. Función pública `l2_normalize` para que toda implementación pueda devolver unit vectors (alinea L2 con coseno en sqlite-vec).
- `core/embeddings/fake.py`: `FakeEmbedder` determinístico para tests. Hashea texto con SHA-256, lo descompone en int32 normalizados a [-1, 1], aplica L2 normalize. Mismo texto → mismo vector. Útil para tests del pipeline sin tocar Ollama.
- `core/embeddings/ollama.py`: `OllamaEmbedder` usando el cliente oficial `ollama` 0.6.2. Lee `model` y `host` de settings. Detecta `dim` real al primer embed. L2 normaliza por default.
- `tests/unit/test_embeddings.py`: 15 tests (l2_normalize + FakeEmbedder).
- `tests/integration/test_ollama_embedder.py`: 7 tests que hablan con Ollama real. Skip automático si el servicio no responde en `OLLAMA_HOST`. Incluye sanity test semántico: "perro labrador marrón" debe estar más cerca de "labrador chocolate jugando" que de "fórmulas matemáticas avanzadas".

**Resultados:**
- 97 unit tests verdes (era 82, +15).
- 7 integration tests verdes con Ollama corriendo local.
- Sanity semántico passes: el ranking por similitud refleja afinidad real entre textos.
- `nomic-embed-text` confirma dim=768, embeddings normalizables, determinístico.

**Decisiones de implementación:**
- `FakeEmbedder` en `core/embeddings/fake.py` (no en `tests/`) para que esté disponible si alguien quiere usarlo sin Ollama en su propio código.
- Integration tests marcados con `pytestmark = [integration, skipif(not _ollama_available())]`. Hace `urllib.request.urlopen(f"{host}/api/tags")` al colectar; salta limpio si Ollama no responde.
- Default normalize=True. Si futuras implementaciones usan un modelo que ya devuelve unit vectors, pueden desactivar.
- Dim se lee al primer embed real, no se hardcodea (a parte del fallback en settings).

**Estado:**
- `uv run pytest tests/unit`: 97 passed.
- `uv run pytest tests/integration`: 7 passed.
- `uv run ruff check`: clean. `uv run mypy`: clean.

**Próximo paso:**
- Orquestador end-to-end: `core/ingest/pipeline.py` que toma el path al zip y hace parse → chunk → embed → store. CLI con typer: `memex ingest <zip>`, `memex search "<query>"`, `memex stats`. Después: 10 búsquedas reales sobre el corpus → criterio de cierre de Fase 0.

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

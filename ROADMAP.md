# Roadmap

> Última actualización: 2026-05-19

**Estado actual:** Fases 0 y 1 cerradas. **Fase 2 en progreso (2026-05-19):** búsqueda híbrida FTS5 + RRF cerrada; captura en vivo (HTTP server + Chrome ext) implementada y validada; embedder zero-config con fastembed default (Ollama opcional). Limpieza completa post-Discord aplicada: 4 críticos del audit, 5 importantes, código muerto y deps reorganizadas. **190 unit tests verdes.** Pendiente para cerrar Fase 2: uso real de la Chrome ext en sesiones reales + auditoría de cierre.

## Principio rector

Que el contexto que tenga Claude.ai lo tenga también Claude Code. Cada fase tiene que acercarse a ese objetivo. Si una tarea no aporta a eso, sobra.

---

## Fase 0: validar retrieval

**Objetivo:** descartar el riesgo más grande antes de invertir tiempo. Probar que con embeddings locales sobre el corpus real de chats, la búsqueda semántica devuelve resultados razonables.

**Tareas:**
- [x] Inspeccionar el JSON export oficial de Claude.ai (esquema, cantidad de chats, edge cases). Ver entrada del DEVLOG del 2026-05-18.
- [x] Implementar `core/models.py` con pydantic: `Project`, `Conversation` (con campo `source`: 'conversations' / 'design_chat' / 'memory'), `Message` (con `parent_uuid`, `raw_content`, flags `has_tool_use`/`has_attachments`), `Chunk`, `SearchHit`.
- [x] Implementar `core/storage/` (schema con 4 tablas + virtual `vec_chunks`, conexión, migración inicial, repo CRUD). 28 tests unitarios verdes.
- [x] Implementar `core/ingest/claude_export.py` con cuatro parsers: `parse_project`, `parse_conversations_list`, `parse_design_chat`, `parse_memories`. La memoria curada se modela como conversación sintética con uuid estable.
- [x] Implementar `core/ingest/content_renderer.py` que convierte `content[]` de Claude.ai a texto plano. Tool blocks se renderizan con markers: `[tool_use: <name>] <input>`, `[result] <texto>`.
- [x] Implementar `core/ingest/chunker.py` (~500 tokens con overlap 50, char-based con factor `chars_per_token` configurable).
- [x] Implementar `core/embeddings/` (interfaz `Embedder` + cliente Ollama con `nomic-embed-text`, más `FakeEmbedder` determinístico para tests). 7 integration tests verdes contra Ollama real.
- [x] Implementar `core/retrieval/search.py` (búsqueda semántica con sqlite-vec, joins con `messages` y `conversations` para hidratar resultados). Vive en `core/storage/repo.py::vector_search`.
- [x] CLI mínima: `memex ingest <path>`, `memex search "<query>"`, `memex stats`.
- [x] Tests unitarios de chunker, content_renderer, parser y pipeline. Un integration test del flujo completo contra el export real.
- [x] Ejecutar búsquedas reales sobre el corpus completo (74 chats, 1024 mensajes, 614 chunks). 6 de 7 con top-3 relevante. Limitación conocida: queries de proper nouns raros (caso "Amarok") fallan; se resuelve con búsqueda híbrida en Fase 2.
- [x] Auditoría de cierre de fase. Un bloqueante encontrado y arreglado (entrypoint `memex-mcp` apuntando a módulo inexistente). Follow-ups menores anotados en DEVLOG para Fase 1+.

**Criterio de cierre:** al menos 7 de 10 búsquedas devuelven en top-3 un chat efectivamente relevante. Si falla, decisión consciente sobre cambiar de modelo (bge-base), ajustar chunking, o reconsiderar el approach.

**Duración estimada:** 1 a 2 días.

---

## Fase 1: MCP MVP

**Objetivo:** que Claude Code pueda usar Memex vía stdio en sesiones reales.

**Tareas:**
- [x] FastMCP server con las 3 tools: `search_chats`, `get_chat`, `list_recent_chats`. Implementadas en `src/memex/transports/tools.py` (lógica pura) + `src/memex/transports/stdio.py` (capa MCP).
- [x] Transport stdio (`memex-mcp` como entrypoint). Re-registrado en `pyproject.toml`.
- [x] Configuración documentada para Claude Code (`.mcp.json`). En README sección "Conectarlo a Claude Code".
- [x] Manejo de errores claros: `EmbedderError` se envuelve en `{"error": ...}` JSON. Queries vacías, uuids inexistentes, sources inválidos devuelven errores accionables sin crashear.
- [x] Usar Memex en sesiones reales. Validado en 2 sesiones de Claude Code que ejercieron las 3 tools (`search_chats`, `get_chat` con pagination, `list_recent_chats`). En la primera invocación de `get_chat` se detectó un bug (respuesta excedía el límite max-tokens del cliente) que se fixeó con pagination + truncation. En la segunda iteración Claude Code descubrió `messages_offset` solo, lo que valida la calidad de las docstrings.
- [x] Auditoría de cierre de fase. Sin bloqueantes. Follow-ups menores cerrados: dead code en `stdio.search_chats`, docs sincronizadas (CLAUDE.md, README, ROADMAP). Follow-ups diferidos a Fase 4: mensaje de error genérico al cliente MCP remoto (evitar leak de paths/queries), catch explícito de excepciones de conexión en `OllamaEmbedder` (hoy con substring frágil).

**Criterio de cierre:** Memex levantado en Claude Code, 5 sesiones reales con al menos una tool invocada, sin crashes.

**Duración estimada:** 1 a 2 semanas.

---

## Fase 2: captura en vivo + retrieval híbrido

**Objetivo doble:**
1. Mejorar calidad de retrieval con búsqueda híbrida (FTS5 + vectores). Resuelve el caso "proper nouns raros" (ej. "Amarok") donde la búsqueda semántica pura falla.
2. Que los chats nuevos aparezcan en Memex sin pedir export manual.

**Tareas:**
- [x] **Búsqueda híbrida FTS5 + RRF.** Schema `fts_chunks` (unicode61 remove_diacritics). `repo.text_search`, `repo.hybrid_search` (RRF k=60), `repo.rebuild_fts_index`. `tools.search_chats` con `mode: hybrid|semantic|lexical` (default hybrid). CLI `memex search --mode` y `memex reindex-fts`. Validado contra el corpus real: caso Amarok resuelto en top-2 del modo hybrid; sin regresión en queries semánticas. (2026-05-19)
- [x] **Endpoint HTTP local** (`transports/http_ingest.py` con Starlette). `POST /ingest/conversation` con origin check (`chrome-extension://` / `moz-extension://`) + validación de shape + manejo de errores. `pipeline.ingest_single_conversation()` reusable. CLI `memex serve --host --port --db`. 14 tests con TestClient y smoke test live con uvicorn real. (2026-05-19)
- [x] **Chrome extension propia de Memex** (`chrome-extension/`). MV3, host_permissions a `claude.ai/*` + `127.0.0.1:5777/*`. inject.js basado en SyncChat (rename), content.js de puente, background.js POSTea al endpoint con stats en `chrome.storage`, popup HTML/JS con indicador de status. README con instrucciones de carga unpacked. (2026-05-19)
- [x] **Idempotencia**: ya cubierta por la arquitectura existente. `repo.add_chunk` + `delete_chunks_for_conversation` sincronizan chunks + vec_chunks + fts_chunks; re-ingestar el mismo chat reemplaza sin duplicar.
- [ ] Uso real de la Chrome ext (5+ chats nuevos en claude.ai, apareciendo en `memex search` sin acción manual).
- [ ] Auditoría de cierre de fase.

**Criterio de cierre:** abrir un chat nuevo en Claude.ai lo deja consultable desde Claude Code en menos de 1 minuto.

**Duración estimada:** ~1 semana.

---

## Fase 3: quality pass

**Objetivo:** subir la calidad de retrieval y la relevancia del contexto inyectado.

**Tareas:**
- [ ] Resúmenes auto-generados al ingestar (Claude Haiku barato).
- [ ] Asociación chat ↔ proyecto/repo (que Claude Code matchee con el repo actual).
- [ ] Hook `SessionStart` opcional para inyectar contexto proactivo.
- [ ] Tool `find_related(current_context)`.
- [ ] Auditoría de cierre de fase.

**Duración estimada:** 1 a 2 semanas.

---

## Fase 4: transport remoto

**Objetivo:** Claude.ai consume Memex como remote MCP.

**Tareas:**
- [ ] Transport SSE/HTTP en FastMCP.
- [ ] Auth (token local, port forwarding o túnel).
- [ ] Documentar cómo conectarlo desde Claude.ai.
- [ ] Auditoría de cierre de fase.

**Duración estimada:** ~1 semana.

---

## Fase 5: release

**Objetivo:** que otra gente lo use.

**Tareas:**
- [ ] Pulir README, screencast, post en Reddit/Discord (playbook SyncChat).
- [ ] Empaquetar para `uvx memex` o instalador.
- [ ] Atender feedback.

---

## Fuera de scope

- Multi-usuario o sharing entre cuentas.
- Cloud o hosted.
- Indexar chats de Claude Code (eso ya lo hace [Claude Historian](https://mcpmarket.com/server/claude-historian)).
- UI fancy de browse (CLI y MCP bastan).
- Attachments, tool_use, files (solo texto en v1 y v2).
- Memoria compartida a nivel team.

## Riesgos abiertos

- **Anthropic shipea oficial:** alta probabilidad en 6 a 12 meses. Mitigación: posicionar local-first y corpus completo.
- **Retrieval pobre con embeddings locales:** mitigado por Fase 0.
- **ToS de Anthropic con la captura:** mismo riesgo que SyncChat. Decisión: publicar abierto con disclaimer, mismo nivel que ShareGPT.

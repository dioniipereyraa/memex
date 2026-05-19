# CLAUDE.md

Contexto y reglas para cualquier instancia de Claude Code trabajando en este repo (incluyendo worktrees paralelos).

## Idea del proyecto en una línea

Que el contexto que tenga Claude.ai lo tenga también Claude Code. Todo lo demás (storage, embeddings, MCP, captura) es plomería para lograr eso.

Detalle completo en [README.md](README.md) y [ROADMAP.md](ROADMAP.md).

## Reglas de trabajo (aplicar SIEMPRE)

1. **Leer el código antes y después de editarlo.** Antes para no romper nada, después para verificar lo que quedó.
2. **Mantener README, ROADMAP y DEVLOG al día con cada cambio relevante.** Actualizarlos en la misma iteración que el código.
3. **Revisar el código recién escrito en busca de bugs** antes de cerrar la tarea.
4. **Al cerrar cada fase del ROADMAP, auditar el proyecto entero** en busca de bugs, código obsoleto y vulnerabilidades. Entregar informe escrito.
5. **Planear antes de codear.** Nada de escribir código sin un plan claro.
6. **Si hay dudas reales, preguntar.** No asumir.
7. **Código y planes pensados para escalar.** Separación clara de responsabilidades (core puro, transport intercambiable, embedder y storage detrás de interfaces).
8. **Sin guiones largos como conector.** Usar comas, puntos, paréntesis. Aplica a docs, commits, código y respuestas al usuario.
9. **Sin shoutout a Claude en commits.** Nada de `Co-Authored-By` ni footers de IA. Commits firmados solo por el autor humano.
10. **Aplicar estas reglas en cada iteración.**

## Stack

- Python 3.12+, gestor de paquetes [uv](https://docs.astral.sh/uv/).
- [FastMCP](https://github.com/jlowin/fastmcp) para el server MCP (soporta stdio y SSE/HTTP).
- SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) para storage y vector search.
- [fastembed](https://github.com/qdrant/fastembed) por default (zero-config, ONNX embebido) u [Ollama](https://ollama.com) opcional con `nomic-embed-text`. Backend configurable via `MEMEX_EMBED_BACKEND`.
- `pydantic` + `pydantic-settings` para config y modelos.
- `typer` + `rich` para CLI.
- `pytest`, `ruff`, `mypy` para test/lint/typecheck.

## Arquitectura

```
src/memex/
├── config.py            ← settings con pydantic-settings (DONE)
├── core/                ← librería pura, sin transport
│   ├── models.py        ← Project, Conversation, Message, Chunk, SearchHit
│   ├── storage/         ← SQLite + sqlite-vec + FTS5 (schema, db, repo)
│   └── ingest/          ← parsers + chunker + pipeline (content_renderer, chunker, claude_export, pipeline)
├── core/embeddings/     ← factory + interfaces
│   ├── base.py          ← Embedder ABC + EmbedderError + l2_normalize
│   ├── fastembed_embedder.py  ← default (ONNX, zero-config)
│   ├── ollama.py        ← opcional (extra `ollama`)
│   ├── fake.py          ← FakeEmbedder determinístico para tests
│   └── __init__.py      ← get_default_embedder() — factory según MEMEX_EMBED_BACKEND
├── transports/          ← bindings MCP + HTTP local
│   ├── tools.py         ← lógica pura de las 3 tools MCP
│   ├── stdio.py         ← entrypoint MCP stdio con FastMCP (memex-mcp)
│   ├── http_ingest.py   ← server HTTP local para captura en vivo (Starlette)
│   └── http.py          ← SSE/HTTP remote MCP (TBD, Fase 4)  ← no existe todavía
└── cli/                 ← CLI con typer (ingest, search, stats, serve, reindex-fts)
```

**Regla de dependencias:** `core/` no importa de `transports/` ni de `cli/`. Las flechas apuntan para adentro.

**Estado al 2026-05-19 (Fase 2 en progreso):**
- Fases 0 y 1 cerradas con audit. Fase 2 con sub-tasks cerrados: búsqueda híbrida FTS5 + RRF, captura en vivo (Chrome ext + HTTP server local), embedder zero-config con fastembed default.
- `vector_search`, `text_search` y `hybrid_search` viven en `core/storage/repo.py`. El directorio `core/retrieval/` se eliminó (estaba vacío); si la lógica de retrieval crece (re-ranking, filtros complejos), se vuelve a crear con contenido real.
- `transports/http.py` no existe aún; lo agrega Fase 4 cuando se arme el remote MCP. La captura en vivo usa `transports/http_ingest.py` (otro server local, distinto del MCP).

## Comandos habituales

```bash
uv sync                       # instala deps + crea .venv
uv run pytest                 # tests (-m 'not integration' para saltar integration)
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src/memex/core    # type check (estricto en core)
uv run memex --help           # CLI (ingest, search, stats, serve, reindex-fts)
uv run memex-mcp              # MCP server stdio (para Claude Code / Desktop)
uv run memex serve            # HTTP server local para captura en vivo desde Chrome ext
```

## Multi-Claude con git worktrees

Para trabajar varios Claudes en paralelo sobre tareas independientes:

```bash
git worktree add ../Memex-ingest feature/ingest
git worktree add ../Memex-embed  feature/embeddings
git worktree add ../Memex-store  feature/storage
```

Cada worktree es una carpeta separada con su propio branch y su propio `.venv` (uv aísla solo). Convergen al mismo `.git`. Cada Claude trabaja sin pisarse archivos, y al mergear todos los cambios entran al mismo repo.

**Limitaciones:** los worktrees no se ven entre sí hasta el merge. Conviene dividir por módulo independiente, no por feature transversal. El coordinador es el humano (o un Claude "lead" en `main`).

## Datos sensibles

Todo lo que está en `data/` es personal y NUNCA va al repo (ya excluido por `.gitignore`):
- `data/exports/*.zip`: exports de Claude.ai con conversaciones reales.
- `data/memex.db`: base SQLite con chats indexados.

El archivo `MEMEX.md` también está en `.gitignore` porque es un documento de contexto interno (handoff de SyncChat), no para usuarios.

## Convenciones de commit

- Mensajes claros, modo imperativo, en español o inglés (cualquiera consistente dentro del mensaje).
- Sin `Co-Authored-By: Claude...`. Sin footers de IA. Sin `Generated with Claude Code`.
- Un commit por unidad lógica de cambio.

## Memoria persistente

Hay memoria del proyecto en `C:\Users\dioni\.claude\projects\d--Dionisio-Memex\memory\`. Contiene reglas de workflow, contexto del usuario, decisiones de setup. Leer al iniciar cada sesión.

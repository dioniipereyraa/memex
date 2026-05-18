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
- [Ollama](https://ollama.com) local con `nomic-embed-text` para embeddings.
- `pydantic` + `pydantic-settings` para config y modelos.
- `typer` + `rich` para CLI.
- `pytest`, `ruff`, `mypy` para test/lint/typecheck.

## Arquitectura

```
src/memex/
├── config.py            ← settings con pydantic-settings (DONE)
├── core/                ← librería pura, sin transport
│   ├── models.py        ← Project, Conversation, Message, Chunk, SearchHit (DONE)
│   ├── storage/         ← SQLite + sqlite-vec (DONE: schema, db, repo)
│   ├── ingest/          ← parsers + chunker + pipeline (DONE: content_renderer, chunker, claude_export, pipeline)
│   ├── embeddings/      ← interfaz Embedder + Ollama + Fake (DONE)
│   └── retrieval/       ← (vacío; vector_search vive en storage/repo.py)
├── transports/          ← bindings MCP (PENDIENTE, Fase 1)
│   ├── tools.py         ← definiciones de tools compartidas (TBD)
│   ├── stdio.py         ← entrypoint stdio (TBD)
│   └── http.py          ← SSE/HTTP (TBD, Fase 4)
└── cli/                 ← CLI con typer (DONE: ingest, search, stats)
```

**Regla de dependencias:** `core/` no importa de `transports/` ni de `cli/`. Las flechas apuntan para adentro.

**Estado al cierre de Fase 0 (2026-05-18):** todo lo marcado `(DONE)` está implementado y testeado. El `vector_search` está en `core/storage/repo.py` (no en `core/retrieval/`) por simplicidad inicial; si `retrieval/` necesita crecer (filtros complejos, híbrido FTS+vector, re-ranking) se va a mover ahí.

## Comandos habituales

```bash
uv sync                       # instala deps + crea .venv
uv run pytest                 # tests (-m 'not integration' para saltar integration)
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src/memex/core    # type check (estricto en core)
uv run memex --help           # CLI (ingest, search, stats)
# uv run memex-mcp            # MCP server stdio — se registra en Fase 1
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

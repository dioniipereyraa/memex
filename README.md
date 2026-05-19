# Memex

> *Quick read in English — full docs below in Spanish.*
>
> **Memex** is a local-first MCP server that indexes your entire Claude.ai history (via official export or live Chrome capture) and exposes `search_chats`, `get_chat`, `list_recent_chats` to Claude Code, Claude Desktop, or any MCP client. Everything stays on your machine: SQLite + sqlite-vec for embeddings, FTS5 for lexical, hybrid retrieval via Reciprocal Rank Fusion. Zero-config embeddings out of the box (fastembed/ONNX), Ollama optional. **Status: pre-alpha**, runs from source. See `ROADMAP.md` for phases, `DEVLOG.md` for the project journal.
>
> Servidor MCP local que indexa tus chats de Claude.ai y los expone a Claude Code (y, próximamente, a Claude.ai vía remote MCP). Que el contexto que tenga Claude.ai lo tenga también Claude Code.

**Estado:** pre-alpha. Fases 0 y 1 cerradas. **Fase 2 en progreso:** búsqueda híbrida FTS5 + RRF cerrada (resuelve caso "Amarok"); captura en vivo via Chrome extension + HTTP server local funcionando, falta uso real durante una semana y auditoría de cierre.

## El problema

Brainstorming y planning pasan en Claude.ai. Ejecución pasa en Claude Code. Los dos mundos no se hablan: Claude Code no puede leer un chat tuyo de Claude.ai, ni siquiera el que originó la tarea que está haciendo. La memoria que Anthropic lanzó en Claude.ai (marzo 2026) es curada, no historial completo, y vive aislada dentro de Claude.ai.

Memex llena ese hueco: corre local, indexa el corpus entero de tus chats, y los expone como tools MCP para que Claude pueda buscar y traer contexto pasado cuando lo necesite.

## Cómo funciona

```
[Claude.ai]
    ↓  (export oficial JSON / Chrome ext)
[Ingestor]  →  [SQLite + sqlite-vec]  →  [embeddings locales con Ollama]
                                    ↓
                          [core: storage + retrieval]
                                    ↓
                  [MCP stdio]  ───→  Claude Code, Claude Desktop
                  [MCP SSE/HTTP] ──→ Claude.ai (próximamente)
```

Diseño: core puro (storage, ingest, embeddings, retrieval) separado del transport. El mismo motor sirve a stdio y a remote MCP sin rewrite.

## Requisitos

- Python 3.12 o superior
- [uv](https://docs.astral.sh/uv/) (gestor de paquetes)

Embeddings: **zero-config por default** (usa [fastembed](https://github.com/qdrant/fastembed) con un modelo cuantizado de 130 MB que se baja la primera vez automáticamente).

Si preferís coordinar con tu instancia local de Ollama (porque ya la tenés corriendo para otros modelos), configurá:
```bash
export MEMEX_EMBED_BACKEND=ollama
# y opcionalmente:
ollama pull nomic-embed-text
```

## Quickstart

1. Cloná el repo e instalá deps:
   ```bash
   git clone https://github.com/dioniipereyraa/memex
   cd memex
   uv sync
   ```
2. Pedí tu export oficial de Claude.ai (Settings → Privacy → Export data), descomprimilo, y dejá el zip en `data/exports/`.
3. Indexá:
   ```bash
   uv run memex ingest data/exports/<tu-export>.zip
   ```
   La primera vez tarda un par de minutos generando embeddings con Ollama.
4. Buscá:
   ```bash
   uv run memex search "tu query" -n 5
   uv run memex stats
   ```

## Tools del MCP server (v1)

- `search_chats(query, limit=5, source?, mode="hybrid")` busca sobre el corpus. Modos: `hybrid` (default, combina vector search + FTS5 BM25 vía Reciprocal Rank Fusion), `semantic` (solo vectores), `lexical` (solo FTS5, ideal para nombres propios o términos exactos). `source` filtra por origen (`conversations`, `design_chat`, `memory`). Dedup por conversación.
- `get_chat(uuid, messages_limit=20, messages_offset=0)` trae una conversación con sus mensajes, paginados. `raw_content` se omite; cada mensaje se trunca a 3000 chars para no exceder el límite de tokens del cliente.
- `list_recent_chats(limit=10, source?)` lista los últimos chats ordenados por última actualización.

La búsqueda también está accesible vía CLI con `memex search "query" --mode {hybrid|semantic|lexical}`. Para bases creadas antes del FTS5 híbrido, correr `memex reindex-fts` una vez para poblar el índice lexical.

## Conectarlo a Claude Code

Una vez que tu base local está poblada (`memex ingest`), levantás el MCP server con `uv run memex-mcp`. Para que Claude Code lo descubra automáticamente, agregá un archivo `.mcp.json` en la raíz de tu proyecto (o un servidor user-level en `~/.claude.json`):

```json
{
  "mcpServers": {
    "memex": {
      "command": "uv",
      "args": ["run", "memex-mcp"],
      "cwd": "/ruta/absoluta/al/repo/de/memex"
    }
  }
}
```

Ajustá `cwd` al path absoluto donde clonaste Memex (donde está el `pyproject.toml`). Reiniciá Claude Code y las tools `search_chats`, `get_chat`, `list_recent_chats` aparecen en la sesión.

Las mismas búsquedas también están disponibles desde CLI con `uv run memex search "..."` si preferís usarlas fuera de Claude Code.

## Captura en vivo (Fase 2)

Para que los chats nuevos de Claude.ai aparezcan en Memex sin pedir export manual:

1. **Arrancá el servidor HTTP local** en una terminal:
   ```powershell
   uv run memex serve
   ```
   Por default escucha en `127.0.0.1:5777`. Lo dejás corriendo mientras navegues claude.ai.

2. **Cargá la Chrome extension** desde la carpeta `chrome-extension/`:
   - Abrí `chrome://extensions/`
   - Activá **Modo desarrollador**
   - **Cargar descomprimida** → seleccioná `chrome-extension/`
   - Click en el ícono de Memex y verificá que el chip "Servidor" diga **responde** (verde).

3. **Usá claude.ai normalmente.** Cada chat que abras o crees se ingesta automáticamente. Verificalo con `memex stats` o llamando `search_chats` desde Claude Code.

Detalles en [chrome-extension/README.md](chrome-extension/README.md).

**Para uso público sin tocar terminales** (Fase 5): va a haber un comando `memex install-service` que registre autostart en Windows / macOS / Linux. Por ahora, terminal manual.

### Hacer que Claude use Memex proactivamente

Por default, los LLMs son conservadores con las tools: prefieren preguntar antes que invocar algo. Si decís *"viste que te hablé de X?"*, Claude tiende a responder *"no recuerdo"* en lugar de buscar.

Las docstrings de las 3 tools ya tienen instrucciones de "USAR PROACTIVAMENTE", pero podés reforzarlo agregando este snippet a tu `CLAUDE.md` (global en `~/.claude/CLAUDE.md` para todas las sesiones, o local en `<proyecto>/CLAUDE.md` para uno específico):

```markdown
## Memex — memoria persistente de chats de Claude.ai

Hay un MCP server `memex` con 3 tools: `search_chats`, `get_chat`, `list_recent_chats`.
Indexan TODO el historial de Claude.ai del usuario, accesible vía búsqueda híbrida
(semántica + lexical FTS5).

**Regla operativa:** antes de responder "no tengo registro", "no recuerdo", "es la
primera vez que oigo de esto", o algo equivalente, invocá `mcp__memex__search_chats`
con la query relevante. La memoria nativa de Claude Code arranca limpia cada sesión;
Memex es el único acceso al historial real del usuario.

Disparadores típicos: "te acordás de...", "viste que...", "ya hablamos de...", "el
otro día charlamos sobre...", o cualquier referencia a un proyecto/persona/decisión
que podría estar en historial.
```

## Roadmap

Ver [ROADMAP.md](ROADMAP.md) para fases, criterios de cierre y estado actual.

## Devlog

Ver [DEVLOG.md](DEVLOG.md) para la bitácora de decisiones, bloqueos y progreso.

## Inspiración y referencias

- Feature request oficial: [anthropics/claude-code#12858](https://github.com/anthropics/claude-code/issues/12858)
- [Claude Historian](https://mcpmarket.com/server/claude-historian), [claude-conversation-extractor](https://github.com/ZeroSumQuant/claude-conversation-extractor): MCP para historial de Claude Code/Desktop. Referencia de estructura de tools.
- [claude-conversation-export](https://github.com/Emnolope/claude-conversation-export): exporter de Claude.ai con la misma estrategia de captura. Útil como backfill.
- Spin-off del proyecto [SyncChat](https://github.com/dionipereyrab/SyncChat).

## Licencia

MIT.

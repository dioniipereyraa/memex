# Memex

> Servidor MCP local que indexa tus chats de Claude.ai y los expone a Claude Code (y, próximamente, a Claude.ai vía remote MCP). Que el contexto que tenga Claude.ai lo tenga también Claude Code.

**Estado:** pre-alpha. Fase 0 cerrada (retrieval validado sobre corpus real). **Fase 1 cerrada**: MCP server stdio funcional, validado en uso real con Claude Code.

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
- [Ollama](https://ollama.com) corriendo local, con el modelo `nomic-embed-text` descargado:
  ```bash
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

- `search_chats(query, limit=5, source?)` busca semánticamente. Devuelve top-N por relevancia, dedup por conversación. `source` filtra por origen (`conversations`, `design_chat`, `memory`).
- `get_chat(uuid, messages_limit=20, messages_offset=0)` trae una conversación con sus mensajes, paginados. `raw_content` se omite; cada mensaje se trunca a 3000 chars para no exceder el límite de tokens del cliente.
- `list_recent_chats(limit=10, source?)` lista los últimos chats ordenados por última actualización.

La búsqueda también está accesible vía CLI con `memex search`.

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

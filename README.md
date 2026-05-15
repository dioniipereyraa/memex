# Memex

> Servidor MCP local que indexa tus chats de Claude.ai y los expone a Claude Code (y, próximamente, a Claude.ai vía remote MCP). Que el contexto que tenga Claude.ai lo tenga también Claude Code.

**Estado:** pre-alpha. Fase 0 (validar retrieval).

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

> Pendiente hasta Fase 0. Cuando esté, va el flujo de `uv sync`, `memex ingest <export.zip>` y `memex search "tu query"`.

## Tools que expone (v1, en construcción)

- `search_chats(query, limit, date_range?)` busca semánticamente sobre todos tus chats.
- `get_chat(id)` trae una conversación completa por id.
- `list_recent_chats(n)` lista los últimos n chats en orden cronológico.

## Conectarlo a Claude Code

> Pendiente hasta Fase 1. Va a ser un snippet de `.mcp.json` o `claude_desktop_config.json`.

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

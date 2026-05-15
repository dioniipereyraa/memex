# Devlog

Bitácora corta, cronología inversa. Una entrada por sesión sustantiva.

Formato: fecha, qué se hizo, decisiones, bloqueos, próximo paso.

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

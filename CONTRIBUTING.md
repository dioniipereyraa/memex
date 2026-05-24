# Contributing to Memex

Thanks for taking a look. Memex is pre-alpha, runs from source, and the roadmap is driven by a single user-facing goal: give Claude Code the same context Claude.ai already has. Phases and close criteria live in [ROADMAP.md](ROADMAP.md); the project journal in [DEVLOG.md](DEVLOG.md).

## Scope of contributions

Welcome:

- Bug reports with reproducible steps.
- Discussion on tool API shape (`search_chats`, `get_chat`, `list_recent_chats`) and how Claude actually uses them in practice.
- Improvements to the live capture path (Chrome extension + HTTP ingest server) for sites or flows that break.
- Better embedders, retrievers, or chunking, with benchmarks.

Out of scope for now:

- Packaging for distribution (Phase 5 work, owned).
- UI other than the Chrome extension.
- Provider integrations beyond Claude.ai (focus first).

If something is unclear, open an issue before writing code. Saves time on both sides.

## Local setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/dioniipereyraa/memex
cd memex
uv sync --extra dev
```

The `dev` extra brings pytest, ruff, mypy, and `ollama` (Python client, used by the optional Ollama backend).

## Running checks locally

The same checks CI runs:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/memex/core src/memex/config.py src/memex/transports
uv run pytest tests/unit -q
```

Integration tests live under `tests/integration/` and need external services (Ollama). Run with:

```bash
uv run pytest tests/integration -q
```

Skipped by default in CI.

## Code style

- **No em dashes (`—`) as connectors.** Use commas, periods, parentheses. Applies to docs, commits, code, and PR descriptions.
- **No AI footers in commits.** No `Co-Authored-By: Claude...`, no `Generated with ...`. Commits signed by the human author.
- **Imperative mood in commit messages.** Spanish or English, pick one per message and stay consistent.
- **Read before you edit, read after.** Verify what you wrote landed as intended.
- **Plan before you code.** No stream-of-consciousness implementations.
- **Architecture rule:** `core/` does not import from `transports/` or `cli/`. Dependencies point inward.

## Pull request workflow

1. Branch off `main`. Conventional names like `feat/...`, `fix/...`, `docs/...`, `chore/...`.
2. Make the change, with tests when it is testable. Match existing test style (see `tests/unit/` for examples).
3. Run all local checks (lint, format, mypy, tests). CI runs the same set; if it is green locally, it should be green there.
4. Open a PR with a description that explains the *why*, not just the *what*. The diff already shows the what.
5. Update `DEVLOG.md` if the change is non-trivial (new feature, behavior change, architecture decision). Skip for pure refactors or test additions.

## Reporting bugs

Open an issue with:

- What you expected.
- What happened instead.
- Steps to reproduce (the smaller the better).
- Output of `memex stats` and `uv run python -V` if relevant.

If it involves the Chrome extension, include the extension version and browser version.

## License

By contributing, you agree your contribution is licensed under the [MIT License](LICENSE) of the project.

#!/usr/bin/env bash
# Periodic Memex ingest of Claude Code / terminal sessions (the backstop to
# the SessionEnd hook). Run on a schedule (launchd on macOS, systemd timer or
# cron on Linux). Scans ~/.claude/projects incrementally: unchanged sessions
# are skipped and the embedding model is loaded only if something is new, so a
# quiet run is nearly free. Low priority so it never competes with real work.

set -uo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || exit 0

mkdir -p "$REPO/data"
exec nice -n 19 "$UV" run --directory "$REPO" memex ingest-claude-code

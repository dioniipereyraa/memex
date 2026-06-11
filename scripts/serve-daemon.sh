#!/usr/bin/env bash
# Wrapper to run the live-capture server (`memex serve`) as a long-lived
# daemon under launchd/systemd. Resolves the repo from its own location and
# runs from there so the relative DB path and the ingest token resolve.
# Idle cost is low: the embedding model is loaded lazily, only on first ingest.

set -uo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || exit 1

cd "$REPO"
exec "$UV" run memex serve

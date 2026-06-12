"""Set a friendly process name so Memex is identifiable in Activity Monitor.

Background daemons otherwise show up as bare `python3.13` / `uv`, which looks
opaque (and alarming when one is using CPU/RAM). `setproctitle` renames the
process so the user sees `Memex capture` / `Memex connector` / `Memex ingest`
and knows what it is and what to stop. macOS truncates the visible name to
~16 chars, so titles are kept short. Best-effort: never fails the program.
"""

from __future__ import annotations


def set_process_title(title: str) -> None:
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except Exception:
        pass

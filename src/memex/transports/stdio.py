"""MCP server stdio entrypoint for Memex.

Runs the shared server (see `memex.transports.mcp_server`) over stdio.
JSON-RPC communication over stdin/stdout. No auth: each MCP client spins
up its own local process, so the caller is trusted by definition.

To connect from Claude Code, add to your `.mcp.json`:

    {
      "mcpServers": {
        "memex": {
          "command": "uv",
          "args": ["run", "memex-mcp"],
          "cwd": "/path/to/the/memex/repo"
        }
      }
    }
"""

from __future__ import annotations

import logging
import sys

from memex.transports.mcp_server import build_server

# Logging to stderr (stdout is reserved for JSON-RPC).
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("memex.mcp")

server = build_server()


def main() -> None:
    """Entrypoint that starts the MCP server over stdio.

    Configured in `pyproject.toml` as the `memex-mcp` script.
    """
    logger.info("Memex MCP server starting (stdio).")
    server.run(show_banner=False)


if __name__ == "__main__":
    main()

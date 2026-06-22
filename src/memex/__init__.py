"""Memex: local MCP server that indexes your Claude.ai chats."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("memex-chats")
except PackageNotFoundError:  # running from a source tree without installed metadata
    __version__ = "0+unknown"

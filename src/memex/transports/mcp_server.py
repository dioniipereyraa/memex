"""Shared MCP server definition for all transports.

Holds the 4 tool wrappers (`search_chats`, `get_chat`, `list_recent_chats`,
`find_related`) and the `build_server()` factory that registers them on a
FastMCP instance. Transports stay thin:

- `transports/stdio.py` builds the server without auth and runs it over
  stdio (local, single user, one process per MCP client).
- `transports/http.py` builds the server with an OAuth provider and mounts
  it as a Streamable HTTP app for remote clients (claude.ai connectors).

The functions decorated here are thin wrappers around the pure
implementations in `memex.transports.tools`. The split lets us test the
logic without spinning up a server.

The SQLite connection and embedder are created lazily (on the first call)
and reused for the lifetime of the process. stdio and HTTP run in separate
processes, so the singletons are never shared across transports; WAL mode
makes concurrent access from both processes safe.

`run_in_thread=False` on each tool: by default FastMCP runs sync tools in a
thread pool, but the `sqlite3` module binds each connection to the thread
that created it. For fast I/O tools it is preferable to run them on the
event loop directly and keep a single connection per process.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from memex.core.embeddings import Embedder, get_default_embedder
from memex.core.storage.db import connect_and_init
from memex.core.summaries import Summarizer, get_default_summarizer
from memex.transports import tools

logger = logging.getLogger("memex.mcp")

_conn: sqlite3.Connection | None = None
_embedder: Embedder | None = None
_summarizer: Summarizer | None = None
_summarizer_resolved: bool = False


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect_and_init()
        logger.info("DB opened: %s", _conn)
    return _conn


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = get_default_embedder()
        logger.info("Embedder initialized: %s", _embedder.model_name)
    return _embedder


def _get_summarizer() -> Summarizer | None:
    """Resolve the summarizer once per process (cached).

    Returns `None` if `MEMEX_SUMMARY_ENABLED` is false or there is no key.
    The `_summarizer_resolved` sentinel distinguishes "not yet resolved"
    from "resolved to None because the feature is OFF".
    """
    global _summarizer, _summarizer_resolved
    if not _summarizer_resolved:
        _summarizer = get_default_summarizer()
        _summarizer_resolved = True
        if _summarizer is not None:
            logger.info("Summarizer active: %s", _summarizer.model_name)
    return _summarizer


def _serialize(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def search_chats(
    query: str,
    limit: int = 5,
    source: str | None = None,
    mode: str = "hybrid",
    repo: str | None = None,
) -> str:
    """Access to the user's persistent memory: ALL their past Claude.ai chats.

    This is the only tool available to access the user's real history.
    Your native memory starts empty in every Claude Code session; anything
    the user has discussed on claude.ai lives only here.

    USE PROACTIVELY (without the user asking explicitly) when:
    - They mention prior conversations or decisions: "remember when...",
      "we talked about...", "the other day we discussed...", "in that
      chat...", "I mentioned earlier...", "as we said...".
    - They ask about a project, person, decision, or specific term that
      might be in their history but is not in your current context.
    - They request context that seems "lost" between sessions or make an
      implicit reference to continuity ("I kept working on X", "the
      approach we discussed").
    - You need background on the user to answer well (what they do, their
      projects, their technical preferences).

    BEFORE saying things like "I have no record", "I do not remember",
    "this is the first time I hear about that", "this is not in my
    memory" or similar, invoke this tool and check the results. The
    likelihood the data is indexed is high.

    Args:
        query: Text to look up (natural language). The `hybrid` mode
            (default) combines semantic search with FTS5 lexical search,
            so it works well both for descriptive phrases and for unusual
            proper nouns (e.g. "Amarok").
        limit: Number of results (default 5, max 50).
        source: Optional filter by chat origin. Valid values:
            'conversations' (standalone claude.ai chats), 'design_chat'
            (chats inside a Claude.ai Project), 'memory' (curated memory),
            'claude_code' (local Claude Code / terminal sessions).
        mode: Search strategy. 'hybrid' (default, recommended in most
            cases), 'semantic' (vectors only, for pure conceptual
            similarity), 'lexical' (FTS5 BM25 only, ideal for exact
            proper nouns or technical terms).
        repo: Optional. If you are running in a repo and want to
            prioritize chats related to it, pass the absolute path
            (e.g. "d:/dionisio/memex") or the git remote URL. Any form
            is accepted; Memex canonicalizes it. Chats associated with
            the repo get a ranking boost proportional to the match
            confidence; chats outside the repo still appear lower (not a
            filter). Requires registering the repo with `memex repos add`.

    Returns:
        JSON with `query`, `mode`, `count`, and `results`: list sorted by
        relevance (distance, lower = more relevant in all three modes).
        Each result includes uuid, title, summary, snippet, and timestamps.
    """
    # `tools.search_chats` already catches `EmbedderError` and returns
    # `{"error": ...}`. Here we only handle the unexpected.
    try:
        result = tools.search_chats(
            _get_conn(),
            _get_embedder(),
            query,
            limit,
            source,
            mode,
            summarizer=_get_summarizer(),
            repo_arg=repo,
        )
    except Exception as e:
        logger.exception("Error in search_chats")
        # Generic message to the client to avoid leaking paths/queries in
        # the error (the detail goes to the log via logger.exception above).
        result = {"error": f"Internal error ({type(e).__name__})."}
    return _serialize(result)


def get_chat(uuid: str, messages_limit: int = 20, messages_offset: int = 0) -> str:
    """Fetch a specific conversation from history by uuid (with pagination).

    USE when:
    - You have already identified a relevant chat (usually via
      `search_chats`) and need more context than the result snippet.
    - The user gave you a specific uuid and asked you to review it.
    - You are following a thread and need detail from a specific chat
      that appeared earlier in the conversation.

    DO NOT use to discover new chats without searching first. To find
    chats by topic or keyword, use `search_chats`.

    If the chat is long, call again with `messages_offset` to paginate
    (truncated response is indicated by `truncated: true` and
    `total_messages`).

    Args:
        uuid: Chat UUID (typically obtained via `search_chats` or
            `list_recent_chats`).
        messages_limit: How many messages to fetch starting from the
            offset (default 10, max 100). Individual messages are
            truncated to 1500 chars so the total response fits in the
            MCP client's token cap (~17k chars worst case with the
            default). If you need more detail per message, ask for fewer
            messages (e.g. messages_limit=5) and you can request a
            higher messages_limit in chats with short messages.
        messages_offset: How many messages to skip from the start of the
            chat (default 0). Use this to paginate long chats: first
            call with offset=0, second with offset=10, etc.

    Returns:
        JSON with chat metadata (title, summary, source, project if
        applicable), `total_messages`, `messages_returned`, `truncated`
        (bool indicating if there are more messages after offset+limit),
        and the `messages` list in ascending chronological order.
    """
    try:
        result = tools.get_chat(_get_conn(), uuid, messages_limit, messages_offset)
    except Exception as e:
        logger.exception("Error in get_chat")
        # Generic message to the client to avoid leaking paths/queries in
        # the error (the detail goes to the log via logger.exception above).
        result = {"error": f"Internal error ({type(e).__name__})."}
    return _serialize(result)


def list_recent_chats(limit: int = 10, source: str | None = None) -> str:
    """Chronological browse of the user's most recent Claude.ai chats.

    USE when:
    - The user asks about recent activity without a specific keyword
      ("what have I been up to", "which chats did I have this week",
      "catch up on what I have been thinking about").
    - You need general context on the topics the user has been working on
      before going deeper.
    - The user asks to explore without knowing exactly what to look for.

    For targeted topic/keyword searches use `search_chats`. This tool is
    for chronological scanning, not for searching.

    Args:
        limit: How many to return (default 10, max 100).
        source: Optional filter by origin. Same values as in
            `search_chats`.

    Returns:
        JSON with `count` and `chats`. Each chat includes uuid, title,
        summary (if any), source, project_uuid, and timestamps.
    """
    try:
        result = tools.list_recent_chats(_get_conn(), limit, source)
    except Exception as e:
        logger.exception("Error in list_recent_chats")
        # Generic message to the client to avoid leaking paths/queries in
        # the error (the detail goes to the log via logger.exception above).
        result = {"error": f"Internal error ({type(e).__name__})."}
    return _serialize(result)


def find_related(
    context: str,
    limit: int = 5,
    repo: str | None = None,
) -> str:
    """Find chats related to a free-form context (not to a short query).

    Different from `search_chats`: this tool accepts long input (a
    paragraph, file contents, what is being discussed right now) and
    returns semantically related chats. Internally uses pure vector
    search (no FTS) because for long inputs exact words weigh less than
    embedding similarity.

    USE when:
    - You want "more like this": you have text in hand and want to see
      which history chats covered similar topics.
    - The user is pasting a paragraph / code / log and you need prior
      context automatically, without having to craft a keyword query.
    - You want to proactively suggest relevant chats without the user
      asking for them.

    For short-keyword searches, use `search_chats`. For a recent
    chronological list, `list_recent_chats`.

    Args:
        context: Free-form text. Truncated to 4000 chars before embedding
            to bound latency.
        limit: Number of results (default 5, max 50).
        repo: Optional. Same repo boost as `search_chats`.

    Returns:
        JSON with `count`, `context_chars` (how many chars of the input
        were used), and `results` (same shape as `search_chats`).
    """
    try:
        result = tools.find_related(
            _get_conn(),
            _get_embedder(),
            context,
            limit,
            repo_arg=repo,
        )
    except Exception as e:
        logger.exception("Error in find_related")
        result = {"error": f"Internal error ({type(e).__name__})."}
    return _serialize(result)


def build_server(*, auth: AuthProvider | None = None) -> FastMCP:
    """Create a FastMCP server with the 4 Memex tools registered.

    Args:
        auth: Optional auth provider. `None` for stdio (the local client
            is trusted by definition); an OAuth provider for the remote
            HTTP transport, where every request carries a bearer token.
    """
    server: FastMCP = FastMCP("memex", auth=auth)
    for fn in (search_chats, get_chat, list_recent_chats, find_related):
        server.tool(fn, run_in_thread=False)
    return server

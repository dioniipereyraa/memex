"""Multi-device sync.

Keeps Claude one memory across the user's devices by syncing local memex
instances peer-to-peer, with no central always-on server. A paired peer exposes
its corpus over the existing `memex serve` HTTP server (`/sync/*` in
`transports/http_ingest`); each side diffs by uuid + content_hash and transfers
the new/changed conversations, vectors included, so the receiver never re-embeds.

The feature is OFF by default and gated by a master flag (`state`): nothing is
served until the user runs `memex sync enable` AND binds `memex serve` beyond
loopback.

- `peers`: the on-disk peer registry (address + shared token, 0600).
- `records`: the shared wire format (serialize + insert + diff), so the server
  and the client cannot drift.
- `client`: pull / push (one-directional overrides) and reconcile (two-way,
  last-writer-wins by `updated_at`).
- `state`: the master on/off gate + per-peer sync history for `status`.
"""

from memex.sync.peers import Peer

__all__ = ["Peer"]

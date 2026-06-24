"""Multi-device sync (experimental, Phase 1).

Keeps Claude one memory across the user's devices by syncing local memex
instances peer-to-peer, with no central always-on server. Phase 1 is a
one-directional, manually triggered pull: a paired peer exposes its corpus over
the existing `memex serve` HTTP server (`/sync/manifest` + `/sync/conversations`
in `transports/http_ingest`), and this side diffs by uuid + content_hash and
pulls the new/changed conversations, vectors included, so the receiver never
re-embeds.

- `peers`: the on-disk peer registry (address + shared token, 0600).
- `client`: the pull logic (manifest diff + insert through the repo).
"""

from memex.sync.peers import Peer

__all__ = ["Peer"]

# dechat Wire Protocol 2.0

This document describes dechat's network protocol as implemented by
`PROTOCOL_VERSION = "2.0"`. It covers discovery, transport, the mesh
relay/flood layer, session-based authentication, and every packet type.

Peers running different protocol versions are not guaranteed to
interoperate and should not be assumed compatible.

## 1. Overview

dechat has two network layers:

1. **Discovery** — UDP broadcast, used only to find other peers' IP
   addresses.
2. **Chat transport** — TCP, used for everything else (messages, private
   messages, pings, file transfer, presence).

On top of the TCP transport sits a **gossip mesh**: peers maintain a
bounded number of direct connections and packets that need to travel
further are wrapped in a `RELAY` envelope and flooded hop by hop.

All wire data is line-delimited UTF-8 text, fields separated by `|`.

## 2. Ports

| Purpose | Protocol | Port |
|---|---|---|
| Discovery | UDP | 5002 |
| Chat transport | TCP | 5001 |

## 3. Identity

- Each running instance generates `MY_ID`, an 8-character random hex-like
  ID (`uuid4()[:8]`), at startup. IDs are **not** persistent across
  restarts.
- Display names are user-chosen strings, persisted locally, and are not a
  security boundary — two peers may share the same display name.
- There is no persistent cryptographic identity (no accounts, no keys
  saved to disk). Identity proof is scoped to a single run.

## 4. Session authentication (anti-spoofing)

Because dechat has no persistent identity, protocol 2.0 introduces a
**per-session HMAC signature scheme** to stop packet forgery on the mesh,
particularly by relay nodes.

### 4.1 Session key

Each peer generates `MY_SESSION_KEY`, 32 random bytes, once at startup.
This key is never transmitted directly.

### 4.2 Signing

Every outbound packet's inner protocol line is signed:

```
tag = HMAC-SHA256(key=MY_SESSION_KEY, msg=inner_line)[:16 hex chars]
```

The tag travels alongside the packet (in the `RELAY` envelope, see §6) and
is verified by every recipient — not just the next hop.

### 4.3 Key exchange (`KEY` packet)

Immediately after a direct TCP connection is established, each side sends
its session key to the other in the clear as a `KEY` packet (§7.1). This
packet is **only** trusted when received directly off the wire (never via
relay) — a relayed `KEY` claim would let any relay hop plant its own key
for someone else's identity and then forge traffic under it.

### 4.4 Trust-on-first-use (TOFU) verification

For each `peer_id`, a peer remembers at most one session key, learned only
from that peer's own direct `KEY` packet:

- **No key on file yet:** only a *direct* (first-hop) packet can establish
  the binding. A relayed packet claiming an unknown `peer_id` is rejected
  outright — there is nothing yet to verify it against.
- **Key already on file:** *every* packet claiming that `peer_id`, direct
  or relayed, must produce a tag that verifies against the known key using
  `hmac.compare_digest`. Anything that doesn't verify is silently dropped.

This closes the main hole in a naive flood/relay design: without it, any
single relay node could forge messages, pings, or file-transfer control
packets "from" any peer_id on the network, since a relayed packet has no
direct socket to check identity against.

### 4.5 What this does and doesn't provide

- **Provides:** integrity and authenticity of packet origin within a
  single run, even across multiple relay hops.
- **Does not provide:** persistent identity across restarts (a fresh run
  = a fresh key, same as a fresh `MY_ID`), or confidentiality — message
  and file content are still sent in plaintext. Any relay hop can read
  traffic it forwards; it just can't alter or forge it without detection.

## 5. Discovery (UDP, port 5002)

Peers periodically broadcast:

```
CHAT:<peer_id>
```

to `255.255.255.255` and their local subnet broadcast address. The
announce interval adapts based on how many peers are already known
(`announce_interval_for_known_peers()`).

On receiving an announcement:

1. Ignore if `peer_id == MY_ID` (self).
2. Record `announced_peer_ids[ip] = peer_id` immediately (before any
   connection attempt), to detect the same identity broadcasting from
   multiple addresses (e.g. a machine with two NICs) before the handshake
   completes.
3. Skip if this `ip` was already attempted, or is in a post-disconnect
   cooldown (`DISCONNECT_RETRY_COOLDOWN`) or a deferred cooldown
   (`DEFER_RETRY_COOLDOWN`).
4. Skip if this `peer_id` is already connected via a different address.
5. If the same `peer_id` has been announced from more than one
   unconnected address, deterministically prefer the lexicographically
   smallest IP so the whole network converges on the same choice.
6. Skip (but stay eligible again soon) if this peer is already at its
   connection-degree cap (`MAX_PEER_DEGREE`) — the peer will still be
   reachable via relay.
7. Otherwise, dial the peer over TCP (`connect_to_peer`).

## 6. Mesh topology and relay envelope

### 6.1 Bounded-degree gossip mesh

Rather than a full mesh (O(n²) connections), each node holds at most
`MAX_PEER_DEGREE` (12) direct TCP connections. Below that peer count the
network is still a full mesh automatically; above it, discovery stops
opening new direct connections and relies on flooding through existing
connections to reach the rest of the network.

### 6.2 RELAY envelope

Any packet that may need to travel beyond a direct neighbor (broadcast
chat, and all point-to-point types, since the target may be multiple hops
away) is wrapped:

```
RELAY|<packet_id>|<ttl>|<tag>|<inner_line>
```

| Field | Description |
|---|---|
| `packet_id` | Random 12-hex-char id (`uuid4().hex[:12]`), used for duplicate suppression |
| `ttl` | Hop budget, starts at `RELAY_TTL` (24), decremented by 1 per hop |
| `tag` | HMAC-SHA256 tag (16 hex chars) over `inner_line`, signed by the packet's originator and never re-signed by relays |
| `inner_line` | The actual protocol packet (see §7) |

A packet is considered to be on its **first hop** (`is_direct = True`) iff
`ttl == RELAY_TTL`. This matters because identity binding (`claim_identity`)
is only safe when the sender at the socket is the packet's actual origin.

### 6.3 Relay behavior

On receipt of a `RELAY` packet:

1. Parse into exactly 5 `|`-delimited fields; malformed envelopes are
   dropped.
2. `mark_seen(packet_id)` — if already seen, drop (duplicate arriving via
   another path).
3. `relay_onward()` — flood the envelope (with `ttl - 1` and the
   **same, unmodified** tag) to every direct neighbor except the one it
   arrived from. No-ops once `ttl <= 0`.
4. Unwrap and process `inner_line` locally as well.

Seen packet IDs are retained up to `MAX_SEEN_IDS` (20000) or
`SEEN_ID_MAX_AGE` (300s), whichever comes first, evicting oldest-first.

### 6.4 Point-to-point vs. broadcast

Point-to-point packet types (private messages, pings, file transfer
control) are flooded the same way as broadcasts, but only **acted on** by
the node whose ID matches the packet's `target_id` field; all other nodes
simply relay it onward. This lets any peer reach any other peer regardless
of hop distance without requiring a direct connection.

### 6.5 Routing hints

`learn_route(peer_id, from_ip)` records, for each verified sender, which
neighbor IP their traffic is arriving through. `send_routed_to_peer_id()`
uses this to unicast a response (e.g. a file chunk ack) back along a known
path instead of flooding it, since some traffic (like per-chunk acks) is
frequent enough that flooding it would double the mesh's bandwidth cost.

## 7. Packet types

All fields except the final "rest" field (where applicable) are
plain `|`-delimited tokens. IDs (`peer_id`, `target_id`, `sender_id`) are
always 8 characters.

### 7.1 `KEY` — session key exchange

```
KEY|<peer_id>|<session_key_hex>
```

Sent immediately after a direct connection is established. `session_key_hex`
is 64 hex chars (32 bytes). Only trusted when received directly
(never relayed). Once bound, a peer's key is fixed for the life of the
connection; a later differing `KEY` claim for the same `peer_id` is
ignored.

### 7.2 `MSG` — broadcast chat message

```
MSG|<peer_id>|<color_idx>|<name>|<text>[|<msg_id>|<reply_id>]
```

Sent wrapped in a `RELAY` envelope (broadcast to the whole mesh).
`text`, `msg_id`, and `reply_id` may be present as a 3-part sub-field
(`text|msg_id|reply_id`) for messages that carry an id or are replies.
Field values are escaped with `escape_field`/`unescape_field` to survive
the `|` delimiter. Names and text are sanitized for display on receipt.

### 7.3 `PRIV` — private message

```
PRIV|<target_id>|<sender_id>|<color_idx>|<name>|<text>
```

Only displayed/acted on by the node whose ID equals `target_id`; other
nodes relay it onward without processing it.

### 7.4 `COLOR` — color change announcement

```
COLOR|<peer_id>|<color_idx>
```

Broadcast whenever a peer changes their display color.

### 7.5 `NAME` — display name announcement

```
NAME|<peer_id>|<name>
```

Broadcast whenever a peer sets/changes their display name. Triggers a
"has joined" notice locally the first time a given `peer_id` is seen.

### 7.6 `PING` / `PONG` — latency probe

```
PING|<target_id>|<peer_id>|<sent_time>
PONG|<target_id>|<peer_id>|<sent_time>
```

`PING` is sent to a specific peer; on receipt, that peer replies with
`PONG` carrying the same `sent_time` so the original sender can compute
round-trip time.

### 7.7 File transfer packets

```
FILE_OFFER|<target_id>|<peer_id>|<offer_id>|<filename>|<filesize>|<file_hash>
FILE_ACCEPT|<target_id>|<peer_id>|<offer_id>
FILE_DECLINE|<target_id>|<peer_id>|<offer_id>
FILE|<target_id>|<peer_id>|<offer_id>|<chunk_index>/<total_chunks>|<hex_data>
FILE_ACK|<target_id>|<peer_id>|<offer_id>|<chunk_index>
```

- `file_hash` is the sender's SHA-256 hex digest of the full file
  contents, used to verify the reassembled file on the receiving end.
- Files are sent in bounded-size chunks, hex-encoded in `hex_data`.
  `total_chunks` is pinned to the value declared by the first chunk of a
  transfer and re-validated on every subsequent chunk.
- `FILE` chunks are only accepted for an `offer_id` the receiver actually
  accepted (`FILE_ACCEPT` was sent for it) and only from the peer that
  made the offer.
- `FILE_ACK` is sent per-chunk, **routed** (unicast via the learned
  reverse path, §6.5) rather than flooded, so the sender's flow-control
  loop (`stream_file`) knows which chunks landed and can resend anything
  un-acked within a bounded retry window.
- On full reassembly, the receiver recomputes the SHA-256 of the
  reassembled bytes and compares it (constant-time) against `file_hash`.
  A mismatch discards the file rather than saving unverified data —
  this also protects against a relay hop tampering with chunk contents
  in transit (relays can read plaintext chunks but altering them is
  detectable).

Transfers are additionally bounded by:

- `MAX_FILE_CHUNKS` (65536) — refuses declared transfers larger than
  this outright.
- `MAX_CONCURRENT_TRANSFERS` (50) — caps the number of simultaneous
  incoming transfers.
- `MAX_TOTAL_INCOMING_BYTES` (512 MiB) — caps combined buffered bytes
  across all in-flight incoming transfers; a chunk that would exceed
  this is dropped (and later retried by the sender) rather than
  accepted.

## 8. Framing, limits, and rate limiting

- Packets are line-delimited (`recv_lines`), buffered per-connection.
- Any raw line longer than 10000 bytes is dropped outright
  (`handle_incoming`).
- Chat-class and file-class traffic are rate-limited separately per
  source IP (`rate_limited` / `file_rate_limited`), so a burst of file
  chunks can't starve ordinary chat traffic and vice versa. Classification
  looks past any `RELAY` envelope to the inner packet type.

## 9. Version negotiation

`PROTOCOL_VERSION = "2.0"` is not itself transmitted peer-to-peer; instead,
each client checks a small external status endpoint at startup
(`fetch_update_status()`) which reports the current expected
`protocolVersion`. If the client's `PROTOCOL_VERSION` doesn't match, it
refuses to start networking and shows an update prompt, since peers on
different protocol versions are not guaranteed to interoperate. A plain
app-version mismatch (`latestVersion`) is non-blocking and only shown as a
dismissable notice. If the status check itself fails (e.g. offline), the
client fails open and starts networking normally.

## 10. Security summary

| Property | Provided | Mechanism |
|---|---|---|
| Origin authenticity | Yes (per-session) | HMAC-SHA256 tag + TOFU key binding |
| Tamper detection (messages/control) | Yes | HMAC tag over inner line |
| Tamper detection (file contents) | Yes | End-to-end SHA-256 file hash |
| Confidentiality | **No** | All content is plaintext |
| Persistent identity | **No** | Keys/IDs are per-run only |
| Replay protection across runs | No | Session keys reset every run |
| Loop/duplicate suppression | Yes | `packet_id` + `seen_msg_ids` |
| Flood amplification bound | Yes | `RELAY_TTL` hop limit |

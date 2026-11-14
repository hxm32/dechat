# DeChat Documentation

This document explains how the code is organised, what each file is
responsible for, and how the pieces fit together. For installation and
usage, see `README.md`. For the wire format, see `Protocol.md`.

## Contents

1. [Overview](#overview)
2. [Running and entry points](#running-and-entry-points)
3. [Layers and import rules](#layers-and-import-rules)
4. [File reference](#file-reference)
5. [How a message travels](#how-a-message-travels)
6. [Threads](#threads)
7. [Shared state](#shared-state)
8. [Packet types](#packet-types)
9. [Files on disk](#files-on-disk)
10. [Configuration](#configuration)
11. [Extending dechat](#extending-dechat)
12. [Debugging](#debugging)

## Overview

DeChat is a peer-to-peer chat with no server. Each running instance:

1. Listens for TCP connections on port `5001`.
2. Broadcasts and listens for UDP announcements on port `5002` to find peers.
3. Keeps a limited number of direct TCP connections to other peers.
4. Floods packets across those connections so peers that are not directly
   connected can still reach each other.
5. Signs every packet it creates with a per-run HMAC key so relays cannot
   forge messages for it.

The code only uses the Python standard library.

## Running and entry points

Run from the folder that contains the `dechat/` directory:

```
python3 -m dechat
```

`dechat/__main__.py` calls `launcher.main()`, which asks for TUI or GUI and
starts the matching front end. Both front ends call the same networking
code (`net/lifecycle.py`) and the same command handler
(`frontends/commands.py`).

## Layers and import rules

The package is split into layers. A module should only import from layers
below it, which is what keeps the code free of circular imports.

```
frontends/        user interface (TUI, GUI, slash commands)
features/         what the user can do (messages, groups, files, peers)
protocol/         parsing and handling incoming packets
net/              sockets, relaying, routing, authentication, discovery
core/             config, identity, shared state, storage, helpers
```

Two notes:

- `net/connections.py` imports `protocol/dispatch.py` so received lines can
  be handled. `protocol/` and `features/` never import `net/connections.py`
  or `net/discovery.py`.
- Shared values that get reassigned at runtime (`running`, `my_name`, and a
  few others) must be read as `state.<name>`, for example `state.my_name`.
  Importing them by name would freeze the value at import time.

## File reference

### Top level

| File | Responsibility |
|---|---|
| `__main__.py` | Lets `python -m dechat` work. Calls `launcher.main()`. |
| `launcher.py` | Asks whether to use TUI or GUI. Falls back to TUI if `tkinter` can't be imported. |

### `core/`: foundations

| File | Responsibility |
|---|---|
| `config.py` | Every tunable constant: ports, file names, version numbers, connection caps, rate limits, timeouts, file-transfer sizes. |
| `identity.py` | This run's random 8-character `MY_ID` and 32-byte `MY_SESSION_KEY`. Signs packets (`sign_packet`, `make_tag`). Loads and saves the display name. |
| `state.py` | All shared runtime state: connected sockets, peer names, keys, routes, groups, ignore list, transfers, offers, rate buckets, and the matching locks. See [Shared state](#shared-state). |
| `output.py` | `ui_print`, the single function all code uses to show text. In TUI mode it prints. In GUI mode it queues text for the Tk thread. Also defines the `@@DECHAT_...@@` event tags the GUI uses for DMs and groups, and the UI mode flag. |
| `utils.py` | Small helpers: timestamps, new ids, `escape_field`/`unescape_field` for the `|` separator, control-character stripping for untrusted text, `human_size`, safe file names. |
| `colors.py` | The 12 colour codes, per-peer colour assignment, and saving your preferred colour per display name. |
| `storage.py` | Appending to `chat.log`, showing recent history on start, and the `.dechat_peers.json` cache of the last verified name per IP. |
| `updates.py` | Fetches the update-check JSON used to detect protocol mismatches and newer versions. |

### `net/`: networking

| File | Responsibility |
|---|---|
| `wire.py` | Lowest-level socket helpers: send a line, read lines from a buffer, drop a peer, send to every neighbour. |
| `relay.py` | The `RELAY` envelope. Wraps outgoing packets, marks packet ids as seen to stop loops, and forwards packets onward with a decremented TTL. |
| `routing.py` | Remembers which neighbour last delivered a packet from each peer, and uses that to unicast replies instead of flooding. Falls back to flooding. |
| `auth.py` | Decides whether a packet is genuine: binds each IP to the first peer id it announces, learns session keys from direct connections, and checks the HMAC tag. Also provides `authentic` and `authentic_for_me` used by the handlers. |
| `ratelimit.py` | Token-bucket limits per IP. File chunks get a larger bucket so transfers aren't throttled like chat. |
| `connections.py` | Accepting inbound connections, dialling outbound ones, the handshake (KEY, COLOR, NAME), the per-connection receive loop, cleanup on disconnect, and automatic reconnect. |
| `discovery.py` | UDP announce and listen loop. Decides whether to dial a discovered peer, avoiding duplicates, preferring one address per peer id, and respecting the connection cap. |
| `reaper.py` | Background cleanup every 10 seconds: stale transfers, pings, offers, routes and seen-packet ids. |
| `lifecycle.py` | `start_networking` (bind the port, show the banner, start threads) and `shutdown_networking`. |

### `protocol/`: incoming packets

| File | Responsibility |
|---|---|
| `dispatch.py` | `handle_incoming`: unwraps `RELAY` envelopes, forwards them on, splits the line, and calls the right handler from the packet table. Unknown lines are printed. |
| `chat_handlers.py` | Handlers for `MSG`, `PRIV`, `COLOR`, `NAME`, `PING`, `PONG`, `GROUP_CREATE`, `GROUP_MSG`. Exposes the `CHAT_HANDLERS` table. |
| `file_handlers.py` | Handlers for `FILE_OFFER`, `FILE_ACCEPT`, `FILE_DECLINE`, `FILE`, `FILE_ACK`. Exposes the `FILE_HANDLERS` table. |

### `features/`: user-facing actions

| File | Responsibility |
|---|---|
| `peers.py` | Name lookups, resolving a typed name or id to a peer id, listing peers, and the ignore list. |
| `messages.py` | Sending public chat, private messages and pings. Remembers recent messages for replies and `/recent`. Detects `@mentions`. |
| `groups.py` | Creating fixed-membership groups and sending group messages. |
| `file_offers.py` | Tracking incoming file offers and the `/accept`, `/reject`, `/offers` actions. |
| `file_send.py` | Offering a file, hashing it, and streaming it in acknowledged chunks with a sliding window and retransmits. |
| `file_receive.py` | Collecting chunks, acknowledging them, checking the SHA-256 hash, and saving the finished file to `Downloads`. |

### `frontends/`: user interface

| File | Responsibility |
|---|---|
| `commands.py` | The slash-command table (`/who`, `/msg`, `/color` and so on) and `process_command`. Shared by the TUI and GUI. |
| `tui.py` | The terminal interface: update check, input loop, and the TUI-only conversation commands (`/dm`, `/group`, `/groupcreate`, `/chat`). |

### `frontends/gui/`: Tkinter interface

| File | Responsibility |
|---|---|
| `app.py` | `run_gui()`: builds the window and wires everything together. This is the largest file because most widgets reference each other. |
| `themes.py` | Colour palettes and the mapping from terminal colour codes to Tk colours. |
| `markdown.py` | Renders markdown and approximate LaTeX into a Tk text widget. |
| `log_render.py` | Draws incoming lines and DM or group transcripts into text widgets, including colours and markdown. |
| `people.py` | The People window and the New Group dialog, plus the function that lists reachable peers. |
| `lock_screen.py` | The "please update" window shown when the protocol version doesn't match. |

## How a message travels

Sending a public chat message:

1. The user types text. `frontends/commands.py` passes it to
   `features/messages.send_chat_message`.
2. The message is logged locally and shown on screen.
3. It is built as a `MSG|...` line, then wrapped by `net/relay.wrap_relay`
   into `RELAY|<packet_id>|<ttl>|<tag>|<line>`. The tag is an HMAC of the
   inner line using your session key.
4. `net/wire.send_to_all` writes it to every directly connected peer.

Receiving it on another machine:

1. `net/connections.receive` reads a line from the socket and applies the
   rate limit.
2. `protocol/dispatch.handle_incoming` unwraps the envelope. If the packet
   id was already seen, it is dropped. Otherwise it is forwarded to the
   other neighbours with a lower TTL.
3. The packet type picks a handler, for example
   `protocol/chat_handlers.handle_msg`.
4. The handler calls `net/auth.authentic`, which checks the sender's
   identity and HMAC tag. If valid, the message is displayed and logged.

Point-to-point packets (private messages, pings, file transfers) are
addressed to one peer id. Every node relays them, but only the target
acts on them. File chunks and acknowledgements use learned routes when
available so they don't flood the whole mesh.

## Threads

| Thread | Started in | Purpose |
|---|---|---|
| Main | `launcher.py` | TUI input loop, or the Tk main loop. |
| Accept | `net/lifecycle.py` | Accepts inbound TCP connections. |
| Discovery | `net/lifecycle.py` | UDP announce and listen. |
| Reaper | `net/lifecycle.py` | Periodic cleanup. |
| Receive (one per peer) | `net/connections.py` | Reads and handles packets from one socket. |
| Reconnect (as needed) | `net/connections.py` | Retries a peer that dropped. |
| File stream (one per outgoing transfer) | `protocol/file_handlers.py` | Sends file chunks. |

All of these are daemon threads, so they stop when the main thread exits.
In GUI mode only the Tk thread touches widgets: other threads put text on
the queue in `core/output.py`, and the GUI drains it on a timer.

## Shared state

`core/state.py` holds the runtime data. Each container has its own lock.
Take only the lock you need and keep the section short.

| Name | Holds |
|---|---|
| `peers` / `lock` | Direct connections, IP to socket. |
| `peer_names`, `ip_to_id`, `announced_peer_ids` / `names_lock` | Display names and which IP belongs to which peer id. |
| `peer_session_keys` / `peer_keys_lock` | Each peer's HMAC key, learned from a direct `KEY` packet. |
| `seen_msg_ids` / `seen_lock` | Recently seen packet ids, for loop prevention. |
| `next_hop_for_peer` / `route_lock` | Learned routes. |
| `group_chats` / `group_lock` | Groups you belong to. |
| `ignored_ids` / `ignore_lock` | Ignored peers. |
| `pending_offers`, `pending_sends`, `incoming_transfers`, `outgoing_transfers` | File transfer bookkeeping, each with its own lock. |
| `rate_buckets`, `file_rate_buckets` | Rate-limit counters per IP. |

Values that are reassigned: `running`, `server`, `discovery_socket`,
`my_name`, `show_id_instead_of_name`, `incoming_bytes_buffered`,
`next_offer_num`. Always access these as `state.<name>`.

## Packet types

Packets are text lines with fields separated by `|`. Literal `|` and
newlines in text fields are escaped by `utils.escape_field`. The full
format is in `Protocol.md`. Summary:

| Type | Purpose | Handler |
|---|---|---|
| `KEY` | Announces your session key. Only accepted on a direct connection, sent unwrapped. | `dispatch.handle_key` |
| `NAME` | Announces your display name. | `chat_handlers.handle_name` |
| `COLOR` | Announces your colour. | `chat_handlers.handle_color` |
| `MSG` | Public chat message. | `chat_handlers.handle_msg` |
| `PRIV` | Private message to one peer. | `chat_handlers.handle_priv` |
| `PING` / `PONG` | Latency check. | `chat_handlers.handle_ping`, `handle_pong` |
| `GROUP_CREATE` | Defines a group and its members. | `chat_handlers.handle_group_create` |
| `GROUP_MSG` | Message in a group. | `chat_handlers.handle_group_msg` |
| `FILE_OFFER` | Offers a file (name, size, SHA-256). | `file_handlers.handle_file_offer` |
| `FILE_ACCEPT` / `FILE_DECLINE` | Reply to an offer. | `file_handlers.handle_file_accept`, `handle_file_decline` |
| `FILE` | One chunk of file data. | `file_handlers.handle_file_chunk` |
| `FILE_ACK` | Acknowledges one chunk. | `file_handlers.handle_file_ack` |
| `RELAY` | Envelope around any packet except `KEY`. | `dispatch.handle_incoming` |

## Files on disk

Written to the folder you run dechat from, except downloads:

| File | Contents |
|---|---|
| `chat.log` | Appended log of messages. The last 20 lines are shown at start. |
| `.dechat_name` | Your saved display name. |
| `.dechat_colors` | Preferred colour per display name. |
| `.dechat_peers.json` | Last verified name and id seen at each IP. Used only for labels. |
| `~/Downloads/` | Received files. Existing names are kept, new ones get a numeric prefix. |

## Configuration

All settings are constants in `core/config.py`. The ones you are most likely
to change:

| Constant | Default | Meaning |
|---|---|---|
| `ENCRYPTION_KEY` | `"dechat"` | Shared passphrase for message encryption. Must match on every peer. |
| `ENCRYPTION_WINDOW` | 60 | Seconds per key window. |
| `ENCRYPTION_TOLERANCE` | 1 | Extra windows accepted either side (clock drift, relay delay). |
| `CHAT_PORT` | 5001 | TCP port for chat. |
| `DISCOVERY_PORT` | 5002 | UDP port for discovery. |
| `MAX_PEER_DEGREE` | 12 | Direct connections each peer aims to keep. |
| `RELAY_TTL` | 24 | How many hops a relayed packet can travel. |
| `RATE_LIMIT_MAX_TOKENS` / `RATE_LIMIT_REFILL_PER_SEC` | 40 / 10 | Chat rate limit per IP. |
| `FILE_CHUNK_SIZE` | 1024 | Bytes per file chunk. |
| `MAX_FILE_CHUNKS` | 65536 | Caps file size (about 64 MB at the default chunk size). |
| `MAX_TOTAL_INCOMING_BYTES` | 512 MB | Memory cap for files being received. |
| `UPDATE_CHECK_URL` | see file | Endpoint used for version checks. |

If you change a value that affects the wire format or timing, such as the
chunk size or TTL, other peers may not behave as expected until they match.

## Extending dechat

**Add a slash command**

1. Write a function in `frontends/commands.py` that takes the argument string.
2. Add it to `_NO_ARG_COMMANDS` (no argument) or `_ARG_COMMANDS` (takes an
   argument).
3. Add a line to `HELP_LINES`, and to `HELP_COMMANDS` in `frontends/gui/app.py`
   if it should appear in the GUI help panel.

**Add a packet type**

1. Write a handler `handler(parts, is_direct, ip, signed_line, tag)` in
   `protocol/chat_handlers.py` or `protocol/file_handlers.py`.
2. Use `authentic(...)` for broadcast packets or `authentic_for_me(...)` for
   packets addressed to you. Never skip verification.
3. Add it to that module's handler table as `"TYPE": (field_count, handler)`.
4. Add the sending side in `features/`, using `wrap_relay` plus
   `send_to_all`, or `send_to_peer_id` for a packet addressed to one peer.
5. If the format changed in a way old peers can't read, bump
   `PROTOCOL_VERSION` in `core/config.py` and update `Protocol.md`.

**Add a GUI theme**

Add an entry to `GUI_THEMES` in `frontends/gui/themes.py` using the same keys
as the existing themes.

## Debugging

Set `DECHAT_DEBUG=1` before running to print discovery and connection
details:

```bash
DECHAT_DEBUG=1 python3 -m dechat
```

Common problems:

- **`No module named dechat`**: you are inside the `dechat` folder. Go up one
  level and run again.
- **"port 5001 is already in use"**: another dechat instance is running on
  this machine.
- **Peers don't find each other**: some networks block UDP broadcast. Use
  `/connect <ip>` on one side.
- **A peer shows as unidentified**: their `KEY` packet hasn't arrived yet, or
  their packets are failing the HMAC check. Reconnecting usually fixes it.

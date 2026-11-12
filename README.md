# DeChat

A serverless, LAN-based peer-to-peer chat app. No accounts, no central server,
no persistent identity. Run it, and it finds other people running it on the
same network and lets you talk. Comes with both a terminal UI and an optional
Tkinter GUI. Only uses the Python standard library, no installs needed.

## Features

- **Zero-config discovery.** Peers find each other via UDP broadcast on the
  local network. No IP addresses or server URLs to configure.
- **Gossip mesh networking.** Scales past a handful of users by capping how
  many direct connections each peer holds and flooding messages across the
  mesh instead of maintaining a full mesh of connections.
- **Session-based anti-spoofing.** Every peer signs its packets with an
  HMAC key generated at startup, so other peers on the mesh (including
  relays) can't forge messages, pings, or file transfers on your behalf.
- **Private messaging, pings, and @mentions.**
- **Group chats** with fixed membership.
- **File transfer** with chunked delivery, acknowledgement/retransmit, and
  SHA-256 integrity verification.
- **Message replies** with reply-linking and ping-on-reply.
- **Per-peer colors and display names**, both persisted locally.
- **Peer IP/name cache** so returning peers keep a recognizable name even
  though their session IDs change between runs.
- **Ignore list**, **message history**, and **rate limiting**.
- Both a **TUI** (plain terminal) and a **GUI** (Tkinter, with themes,
  markdown and LaTeX rendering, a settings panel, and a collapsible news panel).

<img width="638" height="754" alt="Screenshot 2026-08-23 at 8 50 37 AM" src="https://github.com/user-attachments/assets/7519de5d-0eb6-46e5-896f-f17183521957" />

## Requirements

- Python 3.7+
- No third-party dependencies.
- `tkinter` is required for the GUI. If it is unavailable, dechat falls back
  to the TUI automatically. On some Linux distributions it is a separate
  package (for example `sudo apt install python3-tk`).

## Running

Clone the repository (or download it as a ZIP from
<https://github.com/hxm32/dechat> and unzip it):

```bash
git clone https://github.com/hxm32/dechat.git
cd dechat
```

Then run the package from that folder, the one that contains the inner
`dechat/` directory:

```bash
python3 -m dechat
```

On Windows, use `python -m dechat` (or `py -m dechat`).

If you get `No module named dechat`, you are in the wrong folder. Run
`ls` (or `dir` on Windows) and check that you can see a `dechat` directory,
then `cd` up until you can.

You'll be asked whether you want the TUI or GUI:

```
Do you want to use TUI (runs in terminal) or GUI (opens an app)? [t/g]:
```

Everyone on the same local network running dechat will automatically
discover and connect to each other within a few seconds.

Chat history, your display name, and color preferences are saved in the
folder you run it from (`chat.log`, `.dechat_name`, `.dechat_colors`,
`.dechat_peers.json`). Received files are saved to your `Downloads` folder.

## Quick start

Once connected, just type to send a message to everyone. Useful commands:

| Command | Description |
|---|---|
| `/who` | List connected peers |
| `/msg <name\|id> <text>` | Send a private message |
| `/reply <msg_id> <text>` | Reply to a specific message (pings its sender) |
| `/sendfile <name\|id> <path>` | Offer to send a file |
| `/accept <n>` / `/reject <n>` | Accept or decline a pending file offer |
| `/offers` | List pending incoming file offers |
| `/ping <name\|id>` | Measure round-trip latency to a peer |
| `/ignore <name\|id>` / `/unignore <name\|id>` | Mute/unmute a peer |
| `/ignored` | List ignored peers |
| `/color <n>` / `/color random` | Change your display color |
| `/colors` | List available color numbers |
| `/connect <ip>` | Manually connect to a peer (bypasses discovery) |
| `/recent [count]` | Show recent messages with their ids |
| `/help` | Full command list |
| `/quit` | Exit |

TUI only:

| Command | Description |
|---|---|
| `/name <name>` | Set your display name (saved for next run) |
| `/showid` | Toggle showing peer IDs instead of names |
| `/dm <name\|id>` | Switch to a DM conversation (plain text goes there) |
| `/groups` | List the group chats you belong to |
| `/group <name\|id>` | Switch to a group conversation |
| `/groupcreate "name" <member...>` | Create a fixed-membership group and switch to it |
| `/chat` | Return to the public chat |

In the GUI, name, colour, and ID display are in the settings panel, and DMs
and groups have their own tabs.

Typing `@name` in a message pings that user.

## Project layout

```
dechat/
  __main__.py        entry point (python -m dechat)
  launcher.py        TUI/GUI chooser
  core/              config, identity and signing, shared state, colors,
                     logging, output queue, text helpers, update check
  net/               sockets, relay flooding, routing, packet authentication,
                     rate limits, connections, discovery, start/stop
  protocol/          packet dispatch and the chat and file packet handlers
  features/          peers and ignore list, messages, groups, file transfer
  frontends/         slash commands and the terminal UI
  frontends/gui/     Tkinter window, themes, markdown renderer, dialogs
```

## How it works

- **Discovery** happens over UDP broadcast on port `5002`. Each peer
  periodically announces `CHAT:<peer_id>`; anyone who hears it dials in over
  TCP on port `5001`.
- **Connections** are capped per peer (`MAX_PEER_DEGREE` in
  `dechat/core/config.py`) to avoid an O(n²) full mesh at scale. Small groups
  still end up fully connected automatically; larger networks rely on message
  flooding/relaying across the bounded-degree mesh to reach peers that aren't
  direct neighbors.
- **Identity** is ephemeral: a random 8-character ID is generated each run
  (or loaded from a saved display name), with no accounts or persistent
  keys. Within a run, each peer proves ownership of its ID using a
  session-local HMAC key. See [`Protocol.md`](Protocol.md) for details.
- **File transfers** are chunked, acknowledged, retried on loss, and
  SHA-256-verified end to end.

## Known limitations

- **No content encryption.** Messages, private messages, and file contents
  are sent in plaintext over the LAN and can be read by anyone who can
  see the traffic (including relay nodes on a larger mesh). The
  anti-spoofing mechanism prevents forgery and tampering, not
  eavesdropping.
- **LAN-only discovery.** Automatic discovery relies on broadcast traffic
  reaching all participants, so it's intended for a single local network
  segment. Use `/connect <ip>` to bridge networks manually if needed.
- **Ephemeral identity.** IDs, session keys, and peer trust are not
  preserved across restarts. A local `.dechat_peers.json` cache remembers
  only the last verified username seen at each IP to improve display labels;
  it is not an identity or authentication mechanism.
- **Ports.** Only one instance can run per machine, since the chat port
  (`5001`) is fixed.

## Version

- App version: `3.0`
- Protocol version: `2.0` (see [`Protocol.md`](Protocol.md))

Peers running a different protocol version than each other are not
guaranteed to be compatible, and dechat will refuse to start networking if
it detects a protocol mismatch against its update-check endpoint.

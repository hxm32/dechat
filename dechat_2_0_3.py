import hashlib
import hmac
import json
import os
import queue
import random
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

# UI abstraction
#
# The networking/protocol code below never calls print() directly. Instead it
# calls ui_print(), which is swapped out depending on which frontend the user
# picked at startup:
#   - TUI mode points ui_print at real stdout print(), so behavior is
#     identical to a plain terminal chat.
#   - GUI mode points ui_print at a function that pushes the line into a
#     thread-safe queue, which the Tkinter main loop drains and renders into
#     the chat window. This is necessary because Tkinter widgets may only be
#     touched from the main thread, while messages can arrive on background
#     networking threads at any time.
ui_output_queue = queue.Queue()
UI_MODE = "tui"  # set to "gui" by run_gui() before starting networking


def ui_print(*args, sep=" ", end="\n", flush=False):
    text = sep.join(str(a) for a in args) + end
    if UI_MODE == "gui":
        # The "> " prompt re-print used throughout the core is a TUI-only
        # affordance (it redraws the input prompt after an async message
        # interrupts it) and is meaningless in the GUI, which has its own
        # persistent input box. Drop it instead of showing a stray "> " in
        # the chat window.
        if text == "> ":
            return
        ui_output_queue.put(text)
    else:
        print(text, end="", flush=flush)


CHAT_PORT = 5001
DISCOVERY_PORT = 5002

NAME_FILE = ".dechat_name"
COLOR_FILE = ".dechat_colors"
PEER_LOG_FILE = ".dechat_peers.json"
LOG_FILE = "chat.log"
HISTORY_LINES_TO_SHOW = 20

MY_ID = str(uuid.uuid4())[:8]

# Version / news check

# dechat's wire protocol is not guaranteed to be backwards compatible across
# versions (see handle_incoming()), so both frontends check in with a tiny
# status API on startup. The API also carries a short "recent news" blurb
# that the GUI displays in a collapsible side panel. Peers running a
# different protocol version than us can't be safely chatted with, so if our
# protocol version doesn't match the API's protocolVersion we refuse to
# start networking at all and show a "please update" screen instead (see
# run_tui()/run_gui() below). A plain app-version mismatch (latestVersion)
# is not protocol-breaking, so that just shows a dismissable notice instead.
CURRENT_VERSION = "2.0.3"
PROTOCOL_VERSION = "2.0"
UPDATE_CHECK_URL = "https://deapi.hxm128.workers.dev/"
UPDATE_CHECK_TIMEOUT = 5


def fetch_update_status():
    """Hits the dechat status API and returns a dict with at least
    latestVersion/protocolVersion/recentNews/news_details on success, or
    None if the request failed for any reason (offline, bad JSON,
    unreachable host, etc). Never raises -- callers treat None as
    "couldn't check" and the caller decides what to do (the TUI/GUI both
    fail open on a network error rather than locking a user out just
    because they're offline; they only lock the screen on a confirmed
    protocol version mismatch)."""
    try:
        req = urllib.request.Request(
            UPDATE_CHECK_URL, headers={"User-Agent": "dechat"}
        )
        with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

# Session identity / anti-spoofing

# dechat has no persistent cryptographic identity (no user accounts, no
# keys saved to disk) -- by design, per the product's "just run it and
# chat" simplicity. But *within a single run of the program*, every peer
# now proves control of its own id using a random secret key generated at
# startup and never sent over the network in the clear.
#
# How it works:
#   - Each node picks MY_SESSION_KEY = 32 random bytes when it starts up.
#   - Every packet a node originates is tagged with an HMAC-SHA256 over
#     the packet's contents, keyed by MY_SESSION_KEY. Only someone who
#     knows the key can produce a tag that verifies.
#   - The *first* time another node sees a directly-connected (first-hop)
#     packet claiming a given peer_id, it treats that packet's HMAC as
#     proof of ownership and remembers the key implicitly by remembering
#     "this session's tag for peer_id must keep verifying against this
#     same secret". Concretely, it remembers the peer_id -> key binding
#     the moment the peer hands it over during the handshake (see
#     HELLO below), the same way it already remembered peer_id -> ip.
#   - After that, *any* packet claiming to be from that peer_id
#     whether direct or arriving relayed from three hops away, must
#     carry a valid HMAC under that remembered key, or it's dropped.
#
# This closes the biggest hole in the old design: previously, a relayed
# packet was accepted with no verification at all (there was no direct
# socket to check it against), so any single relay node could forge
# messages, pings, or file-transfer control packets "from" any peer_id
# on the network. Now forging a packet from a peer_id you don't control
# requires guessing a 256-bit random key, which is infeasible.
#
# What this does NOT do: it does not provide persistent identity across
# restarts (a fresh run picks a fresh key, same as the old fresh MY_ID),
# and it does not encrypt message content, see the note further down
# by CHAT_PORT/discovery() for why real encryption isn't included.
MY_SESSION_KEY = secrets.token_bytes(32)

# peer_id -> session key, learned once (TOFU, same trust model the old
# ip_to_id binding used) from that peer_id's own first-hop HELLO/handshake
# packet, and enforced for the rest of this run.
peer_keys_lock = threading.Lock()
peer_session_keys = {}


def sign_packet(inner_line):
    """Returns a short hex HMAC tag over inner_line, keyed by our own
    session key, so recipients can verify we (the process presently using
    MY_ID) really produced this packet."""
    tag = hmac.new(MY_SESSION_KEY, inner_line.encode(), hashlib.sha256).hexdigest()[:16]
    return tag


def load_saved_name():
    try:
        with open(NAME_FILE, "r") as f:
            saved = f.read().strip()
            if saved:
                return saved
    except OSError:
        pass
    return MY_ID


def save_name(name):
    try:
        with open(NAME_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass


MY_NAME = load_saved_name()
SHOW_ID_INSTEAD_OF_NAME = False

COLORS = [
    "\033[31m", "\033[32m", "\033[33m", "\033[34m",
    "\033[35m", "\033[36m", "\033[91m", "\033[92m",
    "\033[93m", "\033[94m", "\033[95m", "\033[96m",
]
RESET = "\033[0m"

color_lock = threading.Lock()
id_colors = {}
name_color_lock = threading.Lock()


def load_saved_colors():
    result = {}
    try:
        with open(COLOR_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                name, idx = line.split("=", 1)
                try:
                    result[name] = int(idx)
                except ValueError:
                    pass
    except OSError:
        pass
    return result


def save_colors():
    with name_color_lock:
        items = list(name_to_color.items())
    try:
        with open(COLOR_FILE, "w") as f:
            for name, idx in items:
                f.write(f"{name}={idx}\n")
    except OSError:
        pass


def remember_color_for_name(name, idx):
    with name_color_lock:
        name_to_color[name] = idx
    save_colors()


name_to_color = load_saved_colors()

def color_for_id(peer_id):
    with color_lock:
        if peer_id not in id_colors:
            used = set(id_colors.values())
            for idx in range(len(COLORS)):
                if idx not in used:
                    id_colors[peer_id] = idx
                    break
            else:
                id_colors[peer_id] = sum(ord(c) for c in peer_id) % len(COLORS)
        return COLORS[id_colors[peer_id]]


def set_color_for_id(peer_id, idx):
    if not isinstance(idx, int) or idx < 0 or idx >= len(COLORS):
        return
    with color_lock:
        id_colors[peer_id] = idx


def color_index_for_id(peer_id):
    """Thread-safe lookup of a peer's color index, assigning one if needed."""
    color_for_id(peer_id)  # ensures an entry exists
    with color_lock:
        return id_colors[peer_id]


def claim_color_for_id(peer_id, chosen):
    """Assign `chosen` to peer_id. Colours are display preferences, not
    unique resources, so multiple peers are intentionally allowed to use
    the same colour. Returns False only for an invalid colour index."""
    if not isinstance(chosen, int) or not (0 <= chosen < len(COLORS)):
        return False
    with color_lock:
        id_colors[peer_id] = chosen
    return True

peers = {}
lock = threading.Lock()
running = True
discovery_socket = None

# ---------------------------------------------------------------------------
# Mesh topology

# Historically every discovered peer connected directly to every other peer
# (a full mesh). That's O(n^2) sockets/threads/fds and is fine for a
# handful of people but falls over hard well before 300 (see MAX_PEER_DEGREE
# note below). Instead, each node now keeps a *bounded* number of direct
# connections and messages propagate by flooding across that graph with
# duplicate suppression (see seen_msg_ids / relay_packet below), a
# standard gossip mesh. Point-to-point packets (private messages, pings,
# file transfers) are also flooded but only *handled* by the node whose id
# matches the target, so they still reach any peer even if it's several
# hops away rather than a direct neighbor.
#
# MAX_PEER_DEGREE is deliberately generous: any network with
# MAX_PEER_DEGREE+1 or fewtotal participants ends up as a full mesh
# automatically (every node can connect to every other node), so small
# groups behave exactly as before with zero relay overhead. It only starts
# actually capping connections once the network grows past that, which is
# exactly where full mesh becomes a problem.
MAX_PEER_DEGREE = 12

# Hop limit for relayed/flooded packets. Generous enough to cross a large,
# imperfectly-connected mesh, but bounded so a routing bug can't bounce a
# packet forever
RELAY_TTL = 24

# Bounds how many outbound connect() calls can be in flight at once across
# the whole process. Without this, a burst of near-simultaneous discovery
# announces (all 300 peers broadcast on a shared ~3s cadence) can try to
# open hundreds of sockets/threads at once.
MAX_CONCURRENT_CONNECT_ATTEMPTS = 20
connect_attempt_semaphore = threading.Semaphore(MAX_CONCURRENT_CONNECT_ATTEMPTS)

attempted_lock = threading.Lock()
attempted_connections = set()
DEFER_RETRY_COOLDOWN = 15  # seconds before a deferred-to peer is eligible for a fresh connect attempt
DISCONNECT_RETRY_COOLDOWN = 8  # seconds before a just-dropped ip is eligible for a fresh discovery-triggered dial
deferred_until = {}        # ip -> time.time() after which it's eligible again
# At 300 real peers the old 100-entry cap on these sets was blown through
# almost immediately, causing them to be wiped constantly and defeating the
# anti-flood/anti-thundering-herd logic they exist for. Sized generously
# above the largest network this is designed for.
ANTI_FLOOD_SET_MAX = 2000

# Message/packet ids we've already relayed, so flooding doesn't loop or
# re-deliver forever. Bounded FIFO via insertion order (dicts preserve
# insertion order in Python 3.7+); oldest entries evicted once full.
seen_lock = threading.Lock()
seen_msg_ids = {}   # packet_id -> time.time() first seen
MAX_SEEN_IDS = 20000
SEEN_ID_MAX_AGE = 300  # seconds; well beyond RELAY_TTL's worth of propagation delay

names_lock = threading.Lock()
peer_names = {}
ip_to_id = {}

# A peer ID is intentionally ephemeral, so keep the last verified username
# seen at each IP. Discovery can use this record to label a newly-generated
# session ID before its NAME packet has made it through the mesh.
peer_log_lock = threading.Lock()


def load_peer_log():
    try:
        with open(PEER_LOG_FILE, "r") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ip: entry for ip, entry in data.items()
        if isinstance(ip, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("username"), str)
        and entry.get("username")
    }


peer_identity_log = load_peer_log()


def save_peer_identity(ip, peer_id, username):
    if not ip or not peer_id or not username:
        return
    with peer_log_lock:
        peer_identity_log[ip] = {"peer_id": peer_id, "username": username}
        snapshot = dict(peer_identity_log)
    try:
        with open(PEER_LOG_FILE, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
    except OSError:
        pass


def cached_peer_name(ip):
    with peer_log_lock:
        entry = peer_identity_log.get(ip)
    return entry.get("username") if entry else None


def remember_peer_session(ip, peer_id):
    """Associates the latest ephemeral session ID with a discovered IP.
    The association is only used to group GUI DM history; authentication
    still comes exclusively from the signed TCP protocol packets."""
    if not ip or not peer_id:
        return
    with peer_log_lock:
        entry = peer_identity_log.setdefault(ip, {})
        entry["peer_id"] = peer_id


def stable_peer_ip(peer_id, direct_ip=None):
    if direct_ip:
        return direct_ip
    with names_lock:
        for ip, known_id in ip_to_id.items():
            if known_id == peer_id:
                return ip
        for ip, known_id in announced_peer_ids.items():
            if known_id == peer_id:
                return ip
    with peer_log_lock:
        for ip, entry in peer_identity_log.items():
            if entry.get("peer_id") == peer_id:
                return ip
    return None


# Populated the instant a discovery announcement is heard from an ip,
# independent of whether a connection/handshake to that ip has completed
# yet. ip_to_id (above) only gets an entry once claim_identity() runs,
# which requires a live, fully-handshaken connection, there's a real
# window where we've started connecting to one address for a peer_id but
# haven't finished the handshake when a second address broadcasting the
# *same* peer_id shows up (e.g. a machine with two active network
# interfaces). Without an earlier signal than ip_to_id, the "don't double
# connect to the same identity" check in discovery() can miss that window
# and dial both addresses, and each side's accept_connections() then has
# to arbitrate/reset one of the resulting duplicate sockets, which,
# combined with attempted_connections being cleared on disconnect, can
# loop forever. announced_peer_ids closes that window: it's updated as
# soon as a CHAT: broadcast is heard, before any socket is even opened.
announced_peer_ids = {}  # ip -> peer_id, from discovery broadcasts only

DEBUG_DISCOVERY = os.environ.get("DECHAT_DEBUG") == "1"

ping_lock = threading.Lock()
pending_pings = {}
PING_TIMEOUT = 30

msg_history_lock = threading.Lock()
msg_history = {}         # msg_id -> (sender_name, text)
MAX_MSG_HISTORY = 200

# Ephemeral fixed-membership group chats. A GROUP_CREATE packet, signed by
# its creator, establishes the group definition for this run. Membership is
# deliberately immutable: changing membership safely would require a separate
# creator-authorized update protocol, so this version keeps the model simple.
group_lock = threading.Lock()
group_chats = {}  # group_id -> {"name": str, "creator_id": str, "members": set[str]}
MAX_GROUP_MEMBERS = 50
MAX_GROUP_NAME_LEN = 64

transfer_lock = threading.Lock()
incoming_transfers = {}
TRANSFER_TIMEOUT = 120
MAX_FILE_CHUNKS = 65536          # ~64 MB at 1KB/chunk; bounds memory per transfer
MAX_CONCURRENT_TRANSFERS = 50    # bounds transfer *count*
# MAX_CONCURRENT_TRANSFERS alone bounds how many transfers can be in
# flight at once, but not how much memory they use in total, 50
# transfers each up to MAX_FILE_CHUNKS could previously buffer close to
# 50 * 64MB (~3.2GB) of chunk data simultaneously with no overall ceiling.
# This caps the combined size (in bytes) of all incoming transfers'
# buffered chunks at once; a new transfer (or a new chunk of an existing
# one) that would push the total over this is refused rather than
# accepted, so a burst of large concurrent transfers can't exhaust
# memory regardless of how many distinct transfers or chunks arrive.
MAX_TOTAL_INCOMING_BYTES = 512 * 1024 * 1024   # 512 MB across all transfers combined
incoming_bytes_buffered = 0   # must only be read/modified with transfer_lock held

# Outgoing file transfer ack tracking (fixes: no flow control / no ack +
# retransmit for file chunks)
# Previously stream_file() fired every chunk once, as fast as it could
# read the file, with no idea whether any given chunk actually arrived.
# A single dropped chunk anywhere along the flood path (rate limiting,
# TTL exhaustion, a lossy hop) left the receiver permanently stuck with an
# incomplete transfer until it eventually timed out and was discarded --
# and the sender never even found out.
#
# Now each active outgoing transfer has an entry here recording which
# chunk indices have been acked by the receiver (see FILE_ACK handling in
# handle_incoming() -> record_chunk_ack()). stream_file() sends a bounded
# "window" of chunks at a time, waits briefly for acks, and resends
# whatever wasn't acked -- so a lost chunk gets a bounded number of
# retries instead of silently stalling the whole transfer forever.
outgoing_transfers_lock = threading.Lock()
outgoing_transfers = {}   # offer_id -> {"acked": set(), "event": threading.Event()}


def record_chunk_ack(offer_id, peer_id, chunk_index):
    """Called when a FILE_ACK arrives. Marks chunk_index as confirmed
    received for offer_id and wakes up stream_file() if it's currently
    waiting on acks for this transfer. Silently ignores acks for a
    transfer we don't recognize (already finished, cancelled, or this is
    a stray/duplicate/forged ack for something we never sent, none of
    those need to do anything)."""
    with outgoing_transfers_lock:
        entry = outgoing_transfers.get(offer_id)
        if entry is None:
            return
        entry["acked"].add(chunk_index)
        entry["event"].set()


def get_downloads_dir():
    """Returns the user's Downloads folder (created if it doesn't exist
    yet), falling back to the current directory if it can't be
    determined or created for some reason. Works on Windows, macOS, and
    Linux without any third-party dependency."""
    candidate = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        os.makedirs(candidate, exist_ok=True)
        return candidate
    except OSError:
        return os.getcwd()


offer_lock = threading.Lock()
pending_offers = {}      # offer_id -> {sender_id, filename, filesize, started}
next_offer_num = 1        # small integers shown to the user for /accept N
offer_num_to_id = {}      # display number -> offer_id
MAX_PENDING_OFFERS = 50
OFFER_TIMEOUT = 120

pending_sends_lock = threading.Lock()
pending_sends = {}       # offer_id -> filepath, held by the sender until accepted/rejected

MAX_TRACKED_PEERS = 500          # bounds peer_names/id_colors growth from spoofed ids

ignore_lock = threading.Lock()
ignored_ids = set()

log_lock = threading.Lock()

known_ips_lock = threading.Lock()
known_ips = set()
reconnect_attempts = {}
MAX_RECONNECT_ATTEMPTS = 5
# Randomized (not fixed) delay band. A fixed delay means every peer that
# lost connections in the same bad moment (e.g. many clients briefly
# dropping WiFi association at once on a saturated network) retries at
# exactly the same instant, a thundering herd. Jitter spreads retries out
# over time instead.
RECONNECT_DELAY_MIN = 3
RECONNECT_DELAY_MAX = 9
reconnecting_ips = set()  # ips that currently have a live reconnect_worker

# Simple per-IP token-bucket rate limiting on incoming lines, so one noisy
# or malicious peer can't flood the terminal/log/CPU with unbounded traffic.
rate_lock = threading.Lock()
rate_buckets = {}  # ip -> [tokens, last_refill_time]
RATE_LIMIT_MAX_TOKENS = 40      # burst allowance
RATE_LIMIT_REFILL_PER_SEC = 10  # sustained lines/sec allowed thereafter

# File chunks are sent back-to-back as fast as the sender can read the
# file (hundreds per second for a modest file), which would blow through
# the chat rate limit above almost instantly and cause chunks to be
# silently dropped, the transfer then hangs forever since nothing
# resends a dropped chunk. Give FILE* packets their own, much more
# permissive bucket instead of sharing the chat one.
file_rate_lock = threading.Lock()
file_rate_buckets = {}
FILE_RATE_LIMIT_MAX_TOKENS = 2000
FILE_RATE_LIMIT_REFILL_PER_SEC = 1000


def rate_limited(ip):
    """Returns True if this ip has exceeded its allowed message rate and
    the current line should be dropped."""
    now = time.time()
    with rate_lock:
        tokens, last = rate_buckets.get(ip, (RATE_LIMIT_MAX_TOKENS, now))
        tokens = min(RATE_LIMIT_MAX_TOKENS, tokens + (now - last) * RATE_LIMIT_REFILL_PER_SEC)
        if tokens < 1:
            rate_buckets[ip] = (tokens, now)
            return True
        rate_buckets[ip] = (tokens - 1, now)
        return False


def file_rate_limited(ip):
    """Separate, much higher-throughput token bucket for FILE* packets so
    a legitimate file transfer's rapid chunk stream isn't dropped by the
    chat rate limiter (see file_rate_buckets above). Still bounded, so a
    peer can't use file chunks to bypass rate limiting entirely."""
    now = time.time()
    with file_rate_lock:
        tokens, last = file_rate_buckets.get(ip, (FILE_RATE_LIMIT_MAX_TOKENS, now))
        tokens = min(
            FILE_RATE_LIMIT_MAX_TOKENS,
            tokens + (now - last) * FILE_RATE_LIMIT_REFILL_PER_SEC,
        )
        if tokens < 1:
            file_rate_buckets[ip] = (tokens, now)
            return True
        file_rate_buckets[ip] = (tokens - 1, now)
        return False


# Any text that originated from a peer (names, message bodies, filenames,
# unrecognized raw lines) is untrusted and must never be printed to the
# terminal or written to the log verbatim: it could contain ANSI escape
# sequences or other control characters that manipulate the terminal
# (hide/rewrite text, move the cursor, in vulnerable terminals worse).
# This strips all C0/C1 control characters except that it leaves the
# string otherwise intact (including non-ASCII printable text).
_CONTROL_CHAR_TABLE = {i: None for i in range(0, 0x20)}
_CONTROL_CHAR_TABLE[0x7F] = None  # DEL
for _i in range(0x80, 0xA0):      # C1 control range, incl. start of ANSI seqs
    _CONTROL_CHAR_TABLE[_i] = None


def display_identity(peer_id, name):
    return peer_id if SHOW_ID_INSTEAD_OF_NAME else name


def sanitize_for_display(text):
    if text is None:
        return text
    return text.translate(_CONTROL_CHAR_TABLE)


def log_message(line):
    with log_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def show_recent_history():
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
    except OSError:
        return
    if not lines:
        return
    recent = lines[-HISTORY_LINES_TO_SHOW:]
    ui_print(f"--- Last {len(recent)} message(s) ---")
    for line in recent:
        ui_print(sanitize_for_display(line.rstrip("\n")))
    ui_print("--- End of history ---")


def timestamp():
    return time.strftime("%H:%M")


def escape_field(text):
    # Escape backslash first, then the delimiter and newline, so fields
    # containing '|' or embedded newlines can't misalign protocol parsing.
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\p")
        .replace("\n", "\\n")
    )


def unescape_field(text):
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "p":
                out.append("|")
                i += 2
                continue
            elif nxt == "n":
                out.append("\n")
                i += 2
                continue
            elif nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def send_line(sock, text):
    data = text.encode() + b"\n"
    sock.sendall(data)


def send_line_to_peer(peer_id, sock, ip, text):
    """Send to a specific peer socket, cleaning up shared state on failure
    so a send racing against receive()'s teardown can't leave stale entries
    or crash the caller."""
    try:
        send_line(sock, text)
        return True
    except (ConnectionError, OSError):
        drop_peer(ip)
        return False


def drop_peer(ip):
    with lock:
        dead_peer = peers.pop(ip, None)
    if dead_peer:
        try:
            dead_peer.close()
        except OSError:
            pass


def recv_lines(sock, buffer):
    chunk = sock.recv(4096)
    if not chunk:
        return None, buffer  # peer closed the connection
    buffer += chunk
    lines = buffer.split(b"\n")
    buffer = lines[-1]
    return lines[:-1], buffer


def send_to_peer_id(inner_line):
    """Delivers a point-to-point packet (PRIV, PING, PONG, FILE_OFFER/
    FILE_ACCEPT/FILE_DECLINE, the occasional, low-volume control
    packets) to whichever peer_id it's addressed to, without requiring a
    *direct* connection to them. In the bounded-degree mesh most peers are
    reached over one or more hops rather than a direct socket, so this
    simply floods the packet (wrapped for dedup + hop-limiting) to all of
    our neighbors; each hop's handle_incoming() checks whether the packet
    is addressed to it and, if not, relays it onward automatically. This
    always "succeeds" from the sender's point of view (fire-and-forget,
    same as the old direct send once the socket write itself succeeded);
    delivery isn't otherwise acknowledged.

    High-volume traffic (file chunks and their acks) uses
    send_routed_to_peer_id() instead, see below, to avoid paying
    mesh-wide flooding cost for every single chunk of every transfer."""
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        return False
    send_to_all(wrap_relay(inner_line))
    return True

# Lightweight source routing (file transfers only)

# Flooding every packet to the entire mesh is fine for occasional chat
# messages and file-transfer control packets (offer/accept/decline are
# sent once per transfer), but a file transfer itself can be thousands of
# chunks, flooding *each chunk* to every neighbor at every hop multiplies
# its bandwidth cost by the mesh's fan-out at every hop it crosses, which
# can saturate the whole network for what should be a single sender ->
# receiver stream.
#
# Instead, each node keeps a small "reverse path" table: the direct
# neighbor a given peer_id's traffic was most recently seen arriving from.
# This is learned for free, just by watching normal signed traffic go by
# in relay_onward(), no separate route-discovery protocol needed. Once
# node A has exchanged FILE_OFFER/FILE_ACCEPT with node B (which floods,
# same as before, since it's a one-off), both A and B, and every node in
# between that relayed those control packets, now has a fresh, direct
# "next hop toward B" entry, and file chunks can be unicast hop-by-hop
# along that path instead of flooded. If a path is missing or goes stale
# (a hop drops), sending falls back to a flood for that one packet, so
# delivery is never *less* reliable than before, only more efficient in
# the common case.
route_lock = threading.Lock()
# peer_id -> (next_hop_ip, learned_at), "to reach peer_id, forward to
# next_hop_ip". Populated in relay_onward()/receive() whenever we see a
# signed, verified packet whose origin is peer_id.
next_hop_for_peer = {}
ROUTE_STALE_AFTER = 60  # seconds; a route older than this is not trusted


def learn_route(peer_id, from_ip):
    """Records "from_ip is a good next hop toward peer_id", refreshed
    every time we see fresh traffic confirmed to originate from peer_id
    arriving via from_ip (whether direct or already-relayed once to us,
    either way, from_ip is closer to peer_id than we are)."""
    if peer_id == MY_ID:
        return
    with route_lock:
        next_hop_for_peer[peer_id] = (from_ip, time.time())


def get_route(peer_id):
    with route_lock:
        entry = next_hop_for_peer.get(peer_id)
    if entry is None:
        return None
    next_hop, learned_at = entry
    if time.time() - learned_at > ROUTE_STALE_AFTER:
        return None
    with lock:
        if next_hop not in peers:
            return None
    return next_hop


def send_routed_to_peer_id(peer_id, inner_line):
    """Like send_to_peer_id(), but for high-volume traffic: tries to
    unicast along a learned reverse path first (see learn_route/get_route
    above), and only falls back to a full mesh-wide flood if we don't have
    a fresh route. Returns True/False the same way send_to_peer_id() does.
    Either way the packet is wrapped/signed/dedup-tagged identically, so a
    node relaying it onward can't tell (and doesn't need to care) whether
    it arrived via a routed unicast or a flood, relay_onward() handles
    both the same way."""
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        return False

    wrapped = wrap_relay(inner_line)
    next_hop = get_route(peer_id)
    if next_hop is not None:
        with lock:
            sock = peers.get(next_hop)
        if sock is not None:
            ok = send_line_to_peer(peer_id, sock, next_hop, wrapped)
            if ok:
                return True
            # Send to that specific hop failed (socket just died), fall
            # through to a flood rather than reporting total failure,
            # since other paths to peer_id may still exist.

    send_to_all(wrapped)
    return True


def claim_identity(ip, peer_id):
    """Binds an IP connection to the first peer_id it announces, and
    rejects any later packet on that same connection claiming to be a
    different peer_id. This stops one connected peer from spoofing
    messages as another already-known peer on the *same socket*.

    Also guards against the same remote machine being connected twice
    under two different IPs at once, which happens when it has more
    than one active network interface on the LAN (e.g. Ethernet + WiFi)
    and ends up broadcasting the same MY_ID from each address. Without
    this, both addresses get treated as distinct peers, messages/replies
    bounce unpredictably between the two connections, and the "duplicate"
    one repeatedly connects and disconnects. When a second IP announces
    an identity already bound to a different, still-connected IP, the
    newer connection is torn down and only the original is kept.

    Note: this only governs which IP is credited with which peer_id for
    bookkeeping/UI purposes (e.g. /who, reconnects). It is NOT what stops
    identity spoofing anymore, that's verify_sender()/the HMAC signature
    check below, which is what actually proves a packet came from someone
    who controls peer_id's session key."""
    with names_lock:
        bound = ip_to_id.get(ip)
        if bound is None:
            if len(ip_to_id) >= MAX_TRACKED_PEERS:
                # Bound growth of ip_to_id/peer_names from a flood of
                # distinct claimed identities. Entries are removed from
                # ip_to_id when a connection's receive() thread tears down
                # (see receive()'s cleanup), so this only rejects new
                # identities while MAX_TRACKED_PEERS connections are
                # simultaneously live/pending cleanup, not permanently.
                return False
            existing_ip = next(
                (other_ip for other_ip, other_id in ip_to_id.items() if other_id == peer_id),
                None,
            )
            if existing_ip is not None:
                with lock:
                    still_connected = existing_ip in peers
                if still_connected:
                    should_drop = True
                else:
                    should_drop = False
            else:
                should_drop = False

            if should_drop:
                threading.Thread(target=drop_peer, args=(ip,), daemon=True).start()
                return False

            ip_to_id[ip] = peer_id
            return True
        return bound == peer_id


def learn_or_verify_key(peer_id, is_direct, inner_line, tag):
    """The core anti-spoofing check. Every packet on the wire (after
    unwrapping any RELAY envelope) carries a `tag`, an HMAC-SHA256 of
    the packet keyed by its claimed origin's session key (see
    sign_packet/MY_SESSION_KEY above). This function is what actually
    decides whether a packet claiming to be from peer_id is genuine:

      - If we've never seen peer_id before: we only trust a *direct*
        (first-hop, unrelayed-so-far) packet to establish the binding
        (trust-on-first-use, same spirit as the old ip_to_id binding, but
        now keyed by an unforgeable secret instead of a bare string). A
        relayed packet claiming a brand new peer_id we've never bound a
        key for is rejected outright, we simply have no key to check it
        against, and blindly trusting a relay's word for a stranger's
        identity is exactly the hole this replaces.
      - If we already have a key on file for peer_id: EVERY packet,
        direct or relayed, must produce a tag that verifies against that
        key. This is what stops a malicious relay node from forging or
        altering messages "from" a peer_id it doesn't control, even
        though it's perfectly able to relay, and even read, that
        peer's real traffic. Only the actual holder of the session key
        can produce a valid tag.

    Returns True if the packet is authentic and should be processed,
    False if it should be silently dropped."""
    if not tag or len(tag) != 16:
        return False

    with peer_keys_lock:
        known_key = peer_session_keys.get(peer_id)

    if known_key is not None:
        expected = hmac.new(known_key, inner_line.encode(), hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(expected, tag)

    # No key on file yet. We can only learn one from a packet that is
    # itself unforgeable proof of first-hop origin, which a relayed
    # packet is not (anyone forwarding it could have altered the tag's
    # claimed owner, since we have nothing yet to check it against).
    if not is_direct:
        return False

    # This is the direct-connection TOFU moment: we don't have a key for
    # peer_id yet, and this packet arrived over the live socket to the
    # peer asserting it (not via a relay), so the only way it could carry
    # *some* 16-hex-char tag is if the sender made one up, we can't
    # verify it against anything yet either way. What we do next is bind
    # whatever secret produced *this* tag as peer_id's key from now on, by
    # deriving it from the tag itself being self-consistent is not
    # possible (HMAC isn't invertible, nor should it be), so instead the
    # actual key exchange happens explicitly via a KEY packet sent at
    # connect time (see accept_connections/connect_to_peer), and that KEY
    # packt is what populates peer_session_keys. By the time any other
    # packet type arrives, learn_key() below should already have run. If
    # it hasn't (KEY packet lost/reordered), we have nothing to check
    # against and must reject rather than silently accept unauthenticated
    # content.
    return False


def learn_key(peer_id, ip, is_direct, session_key_hex):
    """Handles an incoming KEY packet: this is peer_id's declaration of
    the session key it will use to sign every packet for the rest of this
    run. Only accepted directly off the wire from a live connection to
    that peer (is_direct), a relayed KEY claim would let any relay hop
    plant a key of its own choosing and then forge traffic under it, which
    is exactly the attack this whole mechanism exists to prevent. Once
    bound, a peer_id's key never changes for the lifetime of the
    connection; a later differing KEY claim is ignored."""
    if not is_direct:
        return
    try:
        session_key = bytes.fromhex(session_key_hex)
    except ValueError:
        return
    if len(session_key) != 32:
        return
    with peer_keys_lock:
        if peer_id not in peer_session_keys:
            peer_session_keys[peer_id] = session_key


def verify_sender(is_direct, ip, peer_id, inner_line=None, tag=None):
    """Gate used in place of a bare claim_identity() call inside
    handle_incoming(). Does two independent checks:
      1. claim_identity(): bookkeeping, which IP gets credited with
         which peer_id (unchanged from before).
      2. learn_or_verify_key(): the actual security check, proves the
         packet was produced by whoever holds peer_id's session key,
         whether the packet arrived directly or was relayed several hops.
    Both must pass. On success, also records `ip` (the neighbor this
    packet reached us through, whether directly or via relay) as a good
    next hop toward peer_id, see learn_route()/send_routed_to_peer_id(),
    since we've now cryptographically confirmed the packet really did
    originate from peer_id, so `ip` really is "closer" to them than we
    are."""
    if is_direct and not claim_identity(ip, peer_id):
        return False
    if inner_line is None or tag is None:
        # Legacy call site (shouldn't happen post-migration) -- fail
        # closed rather than silently accepting unauthenticated content.
        return False
    ok = learn_or_verify_key(peer_id, is_direct, inner_line, tag)
    if ok:
        learn_route(peer_id, ip)
    return ok


def _handle_key(parts, is_direct, ip, signed_line, tag):
    # KEY|peer_id|session_key_hex, see learn_key() above. Only ever
    # trusted when it arrives directly off the wire (is_direct); a
    # relayed KEY claim is ignored, since accepting one would let any
    # relay hop plant its own key for someone else's peer_id and then
    # forge traffic under it.
    _, peer_id, session_key_hex = parts
    if len(peer_id) != 8:
        return
    learn_key(peer_id, ip, is_direct, session_key_hex)


def _handle_group_create(parts, is_direct, ip, signed_line, tag):
    _, creator_id, group_id, group_name, member_csv = parts
    if len(creator_id) != 8 or not (8 <= len(group_id) <= 32):
        return
    if not verify_sender(is_direct, ip, creator_id, signed_line, tag):
        return
    group_name = sanitize_for_display(unescape_field(group_name)).strip()
    if not group_name or len(group_name) > MAX_GROUP_NAME_LEN:
        return
    members = {m for m in member_csv.split(",") if len(m) == 8}
    if creator_id not in members or not (2 <= len(members) <= MAX_GROUP_MEMBERS):
        return
    # Only retain groups that include us; non-members still relay the packet
    # because relay_onward() already ran above before local processing.
    if MY_ID not in members:
        return
    with group_lock:
        existing = group_chats.get(group_id)
        definition = {"name": group_name, "creator_id": creator_id, "members": members}
        if existing is not None:
            # A group id is immutable once learned. Conflicting definitions
            # are ignored rather than silently mutating membership.
            if existing != definition:
                return
        else:
            group_chats[group_id] = definition
    if UI_MODE == "gui":
        ui_print("@@DECHAT_GROUP_CREATE@@" + json.dumps({
            "group_id": group_id, "name": group_name,
            "creator_id": creator_id, "members": sorted(members),
        }, separators=(",", ":")))
    else:
        ui_print(f"Joined group '{group_name}' ({group_id}).")


def _handle_group_msg(parts, is_direct, ip, signed_line, tag):
    _, group_id, sender_id, color_idx, sender_name, text = parts
    if len(sender_id) != 8 or not (8 <= len(group_id) <= 32):
        return
    if not verify_sender(is_direct, ip, sender_id, signed_line, tag):
        return
    with group_lock:
        group = group_chats.get(group_id)
        if group is None or MY_ID not in group["members"] or sender_id not in group["members"]:
            return
    sender_name = sanitize_for_display(unescape_field(sender_name))
    text = sanitize_for_display(unescape_field(text))
    try:
        color_idx_int = int(color_idx)
        set_color_for_id(sender_id, color_idx_int)
    except ValueError:
        color_idx_int = 0
    with names_lock:
        peer_names[sender_id] = sender_name
    with ignore_lock:
        if sender_id in ignored_ids:
            return
    log_message(f"{timestamp()} [group {group['name']}] {sender_name}: {text}")
    if UI_MODE == "gui":
        ui_print("@@DECHAT_GROUP_MSG@@" + json.dumps({
            "group_id": group_id, "sender_id": sender_id,
            "sender_name": sender_name, "color_idx": color_idx_int,
            "text": text, "outgoing": False,
        }, separators=(",", ":")))
    else:
        c = color_for_id(sender_id)
        ui_print(f"{timestamp()} [group {group['name']}] {c}{sender_name}{RESET}: {text}")


def _handle_msg(parts, is_direct, ip, signed_line, tag):
    _, peer_id, color_idx, name, rest = parts
    msg_id = ""
    reply_id = ""
    text = rest
    rest_parts = rest.split("|", 2)
    if len(rest_parts) == 3:
        text, msg_id, reply_id = rest_parts

    if len(peer_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    name = sanitize_for_display(unescape_field(name))
    text = sanitize_for_display(unescape_field(text))
    msg_id = unescape_field(msg_id)
    reply_id = unescape_field(reply_id)
    try:
        set_color_for_id(peer_id, int(color_idx))
    except ValueError:
        pass
    with names_lock:
        peer_names[peer_id] = name
    with ignore_lock:
        is_ignored = peer_id in ignored_ids

    if msg_id:
        remember_message(msg_id, name, text)

    reply_tag = ""
    pinged_by_reply = False
    if reply_id:
        replied = get_remembered_message(reply_id)
        if replied:
            reply_tag = f" (reply to {replied[0]} [{reply_id}])"
            pinged_by_reply = replied[0] == MY_NAME

    id_tag = f"[{msg_id}]" if msg_id else ""
    line = f"{timestamp()} {id_tag}{reply_tag} {name}: {text}"
    log_message(line)
    if is_ignored:
        return

    c = color_for_id(peer_id)
    pinged = pinged_by_reply or mentions_me(text)
    shown_name = display_identity(peer_id, name)
    display = f"{timestamp()} {id_tag}{reply_tag} {c}{shown_name}{RESET}: {text}"

    if pinged:
        if UI_MODE == "gui":
            ui_print(f"\a<<PING>>{display}<<PING>>")
        else:
            ui_print(f"\a\033[7m{display}\033[27m")
    else:
        ui_print(display)
    ui_print("> ", end="", flush=True)


def _handle_priv(parts, is_direct, ip, signed_line, tag):
    _, target_id, sender_id, color_idx, rest = parts
    if len(sender_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, sender_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    if "|" not in rest:
        return
    name, text = rest.split("|", 1)
    name = sanitize_for_display(unescape_field(name))
    text = sanitize_for_display(unescape_field(text))
    try:
        set_color_for_id(sender_id, int(color_idx))
    except ValueError:
        pass
    with names_lock:
        peer_names[sender_id] = name
    with ignore_lock:
        is_ignored = sender_id in ignored_ids
    line = f"{timestamp()} [private] {name}: {text}"
    log_message(line)
    if is_ignored:
        return
    c = color_for_id(sender_id)
    shown_name = display_identity(sender_id, name)
    if UI_MODE == "gui":
        with color_lock:
            dm_color_idx = id_colors.get(sender_id, 0)
        dm_ip = stable_peer_ip(sender_id, ip if is_direct else None)
        ui_print("@@DECHAT_DM@@" + json.dumps({
            "peer_id": sender_id, "dm_key": dm_ip, "name": name, "text": text,
            "color_idx": dm_color_idx, "outgoing": False,
        }, separators=(",", ":")))
    else:
        ui_print(f"{timestamp()} [private] {c}{shown_name}{RESET}: {text}")
        ui_print("> ", end="", flush=True)


def _handle_color(parts, is_direct, ip, signed_line, tag):
    _, peer_id, color_idx = parts
    if len(peer_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    try:
        set_color_for_id(peer_id, int(color_idx))
    except ValueError:
        pass


def _handle_name(parts, is_direct, ip, signed_line, tag):
    _, peer_id, name = parts
    if len(peer_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    name = sanitize_for_display(unescape_field(name))
    if is_direct:
        save_peer_identity(ip, peer_id, name)
    with names_lock:
        is_new = peer_id not in peer_names
        duplicate = any(
            pid != peer_id and other == name for pid, other in peer_names.items()
        )
        peer_names[peer_id] = name
    # NOTE: name_to_color (loaded from colors.txt) is intentionally
    # NOT consulted here. It exists purely to remember *your own*
    # preferred color across restarts of this program (see the
    # MY_NAME lookups elsewhere), it must never be used to color
    # someone else's incoming peer_id, because it's keyed only by
    # display name, not by identity. Two different people who happen
    # to pick the same display name (plausible, and even likely for
    # common names) would otherwise silently inherit whichever one of
    # them you saw use that name first, every time either of them
    # reconnects, a real cross-identity color mix-up. Each
    # incoming peer_id gets an automatically assigned color the
    # normal way (color_for_id), same as if no preference existed.
    if is_new and name != peer_id:
        c = color_for_id(peer_id)
        ui_print(f"\n{c}{name}{RESET} has joined ({peer_id})")
        if duplicate:
            ui_print(
                f"Note: '{name}' is already in use by another connected "
                f"peer; use the id ({peer_id}) with /msg, /ping, etc. to "
                f"be sure you reach the right one."
            )
        ui_print("> ", end="", flush=True)


def _handle_ping(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, sent_time = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    send_to_peer_id(f"PONG|{peer_id}|{MY_ID}|{sent_time}")


def _handle_pong(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, sent_time = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    try:
        sent = float(sent_time)
    except ValueError:
        return
    rtt_ms = (time.time() - sent) * 1000
    with names_lock:
        name = peer_names.get(peer_id, peer_id)
    ui_print(f"\nPong from {name}: {rtt_ms:.1f} ms")
    ui_print("> ", end="", flush=True)
    with ping_lock:
        pending_pings.pop(peer_id, None)


def _handle_file_offer(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id, filename, filesize, file_hash = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    filename = sanitize_for_display(unescape_field(filename))
    try:
        filesize = int(filesize)
    except ValueError:
        return
    # file_hash is the sender's SHA-256 of the whole file, hex
    # encoded, so the receiver can verify the reassembled file wasn't
    # corrupted or tampered with in transit (see FILE handling below
    # and stream_file()). A malformed/missing hash just means we
    # won't be able to verify, treat it as "unknown" rather than
    # rejecting the whole offer over it.
    if len(file_hash) != 64 or any(c not in "0123456789abcdef" for c in file_hash.lower()):
        file_hash = None
    with names_lock:
        name = peer_names.get(peer_id, peer_id)

    with offer_lock:
        if len(pending_offers) >= MAX_PENDING_OFFERS:
            return
        num = register_offer(peer_id, offer_id, filename, filesize, file_hash)

    size_str = human_size(filesize)
    ui_print(f"\n{name} wants to send you a file: '{filename}' ({size_str})")
    ui_print(f"Type /accept {num} to receive it, or /reject {num} to decline.")
    ui_print("> ", end="", flush=True)


def _handle_file_decline(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    with pending_sends_lock:
        pending_sends.pop(offer_id, None)
    with names_lock:
        name = peer_names.get(peer_id, peer_id)
    ui_print(f"\n{name} declined your file offer.")
    ui_print("> ", end="", flush=True)


def _handle_file_accept(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    with pending_sends_lock:
        filepath = pending_sends.pop(offer_id, None)
    if filepath:
        threading.Thread(
            target=stream_file, args=(peer_id, offer_id, filepath), daemon=True
        ).start()


def _handle_file_chunk(parts, is_direct, ip, signed_line, tag):
    global incoming_bytes_buffered
    _, target_id, peer_id, offer_id, chunk_info, hex_data = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it

    with offer_lock:
        offer = pending_offers.get(offer_id)
    if not offer or offer["sender_id"] != peer_id or offer.get("accepted") is not True:
        # Only accept chunks for an offer this peer actually sent and
        # that we ourselves accepted; otherwise a peer could push file
        # data we never agreed to receive.
        return
    filename = offer["filename"]

    try:
        chunk_index, total_chunks = chunk_info.split("/")
        chunk_index = int(chunk_index)
        total_chunks = int(total_chunks)
        chunk_bytes = bytes.fromhex(hex_data)
    except ValueError:
        return

    if total_chunks <= 0 or not (0 <= chunk_index < total_chunks):
        return
    if total_chunks > MAX_FILE_CHUNKS:
        # Refuse absurdly large declared transfers outright rather than
        # allocating memory for them chunk by chunk.
        return

    key = offer_id
    complete_entry = None
    with transfer_lock:
        entry = incoming_transfers.get(key)
        if entry is None:
            if len(incoming_transfers) >= MAX_CONCURRENT_TRANSFERS:
                # Too many transfers already in flight; drop this one
                # rather than growing memory unboundedly.
                return
            # Pin total_chunks to what the first chunk of this transfer
            # declared. Re-checking it against every later chunk (rather
            # than trusting whatever value arrives last) stops a
            # malicious/buggy peer from making the transfer "complete"
            # with a different chunk count than it started with, which
            # previously could leave gaps in `chunks` and crash the
            # reassembly below with a KeyError.
            entry = {"chunks": {}, "started": time.time(), "total": total_chunks, "bytes": 0}
            incoming_transfers[key] = entry
        if total_chunks != entry["total"] or chunk_index >= entry["total"]:
            return
        if chunk_index not in entry["chunks"]:
            # Only charge new chunk data against the global byte
            # ceiling once per chunk index, a legitimate resend of
            # an already-buffered chunk (e.g. a duplicate that slipped
            # through before our ack reached the sender) shouldn't be
            # double-counted against the limit.
            if incoming_bytes_buffered + len(chunk_bytes) > MAX_TOTAL_INCOMING_BYTES:
                # Accepting this chunk would push the combined size of
                # every in-flight incoming transfer's buffered data
                # over the global ceiling. Drop it rather than
                # allocating past that bound; the sender's ack/retry
                # logic (see stream_file()) will eventually retry it,
                # by which point other transfers may have freed up
                # room, or this one will time out and get reaped like
                # any other stalled transfer.
                return
            incoming_bytes_buffered += len(chunk_bytes)
            entry["bytes"] += len(chunk_bytes)
        entry["chunks"][chunk_index] = chunk_bytes
        entry["started"] = time.time()  # refresh on activity
        have_all = len(entry["chunks"]) == entry["total"]
        if have_all:
            complete_entry = incoming_transfers.pop(key)
            incoming_bytes_buffered -= complete_entry["bytes"]

    # Acknowledge this specific chunk so the sender's stream_file()
    # can stop waiting on it and knows not to retransmit it. Routed
    # (unicast along the learned path back to the sender) rather than
    # flooded, see send_routed_to_peer_id(), since acks are sent
    # one per chunk and flooding each one would double the mesh-wide
    # bandwidth cost of the whole transfer for no benefit.
    send_routed_to_peer_id(peer_id, f"FILE_ACK|{peer_id}|{MY_ID}|{offer_id}|{chunk_index}")

    if complete_entry is not None:
        chunks = complete_entry["chunks"]
        total = complete_entry["total"]
        # Defensive even though the count check above should guarantee
        # every index 0..total-1 is present: reassembling with .get(...)
        # instead of chunks[i] means a bug here fails soft (silently
        # drops a piece) rather than crashing the receive thread.
        data = b"".join(chunks.get(i, b"") for i in range(total))

        expected_hash = offer.get("file_hash")
        if expected_hash:
            actual_hash = hashlib.sha256(data).hexdigest()
            if not hmac.compare_digest(actual_hash, expected_hash):
                # Integrity check failed: what we reassembled doesn't
                # match the hash the sender advertised in FILE_OFFER,
                # which the sender computed straight from the file on
                # disk. This catches both accidental corruption
                # (dropped/reordered bytes despite the ack/retransmit
                # logic) and any tampering by a misbehaving relay hop
                # along the way (relays can see file chunks in plain
                # text, see the confidentiality note by
                # DISCOVERY_PORT, but altering them is now
                # detectable, even though reading them isn't
                # preventable without real encryption). Refuse to
                # write a file we can't vouch for.
                with names_lock:
                    name = peer_names.get(peer_id, peer_id)
                with offer_lock:
                    pending_offers.pop(offer_id, None)
                ui_print(
                    f"\nReceived '{sanitize_for_display(filename)}' from {name}, "
                    f"but it FAILED the integrity check (hash mismatch) -- "
                    f"discarding it rather than saving a possibly-corrupted "
                    f"or tampered file. Ask them to resend."
                )
                ui_print("> ", end="", flush=True)
                return

        safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "received_file"
        downloads_dir = get_downloads_dir()
        out_name = safe_name
        counter = 1
        while True:
            try:
                out_path = os.path.join(downloads_dir, out_name)
                with open(out_path, "xb") as f:
                    f.write(data)
                break
            except FileExistsError:
                out_name = f"{counter}_{safe_name}"
                counter += 1
        with names_lock:
            name = peer_names.get(peer_id, peer_id)
        with offer_lock:
            pending_offers.pop(offer_id, None)
        verified_note = " (integrity verified)" if expected_hash else ""
        ui_print(f"\nReceived file '{sanitize_for_display(filename)}' from {name}, saved to {out_path}{verified_note}")
        ui_print("> ", end="", flush=True)


def _handle_file_ack(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id, chunk_index = parts
    if len(peer_id) != 8 or len(target_id) != 8:
        return
    if not verify_sender(is_direct, ip, peer_id, signed_line, tag):
        return
    if target_id != MY_ID:
        return  # not for us; relay_onward() in handle_incoming already forwarded it
    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return
    record_chunk_ack(offer_id, peer_id, chunk_index)


# Dispatch table for handle_incoming(): wire packet type -> (expected
# field count after splitting, handler function). Splitting on the type
# first lets each handler assume its exact shape; a known type that
# doesn't arrive with the right field count falls through to
# handle_incoming()'s default case below, same as an unrecognized type.
_PACKET_HANDLERS = {
    "KEY": (3, _handle_key),
    "GROUP_CREATE": (5, _handle_group_create),
    "GROUP_MSG": (6, _handle_group_msg),
    "MSG": (5, _handle_msg),
    "PRIV": (5, _handle_priv),
    "COLOR": (3, _handle_color),
    "NAME": (3, _handle_name),
    "PING": (4, _handle_ping),
    "PONG": (4, _handle_pong),
    "FILE_OFFER": (7, _handle_file_offer),
    "FILE_DECLINE": (4, _handle_file_decline),
    "FILE_ACCEPT": (4, _handle_file_accept),
    "FILE": (6, _handle_file_chunk),
    "FILE_ACK": (5, _handle_file_ack),
}


def handle_incoming(raw, ip):
    """Unwraps one line off the wire (a RELAY envelope if present) and
    dispatches it to the handler for its packet type via
    _PACKET_HANDLERS. Anything unrecognized, or a known type that
    arrived with the wrong number of fields, falls through to the
    default case: print the raw line and a fresh prompt, exactly as an
    old/unknown client's line would appear."""
    if not raw or len(raw) > 10000:
        return

    # RELAY envelope: "RELAY|packet_id|ttl|tag|<inner protocol line>". The
    # tag is an HMAC-SHA256 signature (see sign_packet/wrap_relay) proving
    # the packet really came from whoever it claims to be from, checked
    # once the inner line is parsed and its claimed origin peer_id is
    # known (see verify_sender/learn_or_verify_key below). This is how a
    # packet crosses more than one hop in the bounded-degree mesh, every
    # node unwraps it, processes the inner line if applicable (e.g. it's a
    # broadcast, or it's targeted at this node), and floods it onward to
    # its other neighbors exactly once. Duplicate copies arriving later
    # via a different path are silently dropped by mark_seen().
    #
    # is_direct tracks whether the peer at `ip` is the one actually
    # asserting the identity in this packet (True), vs. this packet having
    # been relayed on behalf of some other, possibly many-hops-away origin
    # (False). This distinction matters for claim_identity(): binding
    # ip -> peer_id is only meaningful/safe when the peer at `ip` is the
    # one who originated the claim.
    #
    # Every relayable packet is wrapped with ttl starting at RELAY_TTL and
    # decremented by exactly 1 per hop (see wrap_relay/relay_onward). So a
    # packet arriving with ttl == RELAY_TTL is still on its first hop,
    # the neighbor at `ip` who just sent it to us IS its originator, not
    # merely a relay, and is just as trustworthy to bind identity from
    # as old-style unwrapped traffic. This matters because the handshake
    # NAME/COLOR a peer sends the moment we connect is itself wrapped (so
    # it can also propagate further than one hop), and without this check
    # it would never be treated as direct, leaving every peer permanently
    # "(unidentified)" in /who.
    is_direct = True
    tag = None
    if raw.startswith("RELAY|"):
        env = raw.split("|", 4)
        if len(env) != 5:
            return
        _, packet_id, ttl_str, tag, inner = env
        if not (1 <= len(packet_id) <= 64):
            return
        if len(tag) != 16:
            return
        try:
            ttl = int(ttl_str)
        except ValueError:
            return
        is_direct = (ttl == RELAY_TTL)
        if not mark_seen(packet_id):
            return  # already processed/relayed this one
        relay_onward(packet_id, ttl, tag, inner, from_ip=ip)
        raw = inner  # fall through and process the inner packet locally

    # `raw` at this point is exactly the inner protocol line that `tag`
    # (if any) was signed over, verify_sender() below checks each
    # packet's HMAC against this exact string, so it must not be mutated
    # before the check.
    signed_line = raw

    # Each packet type has a different number of leading fixed fields
    # followed by one "rest" field that may itself contain further
    # escaped "|"-delimited data (e.g. MSG's text|msgid|replyid, or
    # PRIV's name|text), so the maxsplit has to match the type, not a
    # single global constant, or a legitimately-escaped payload field can
    # get sliced at the wrong boundary. Peek at the type first with an
    # unbounded split of just the leading token, then re-split with the
    # right maxsplit for that type below.
    packet_type = raw.split("|", 1)[0]
    split_at = {
        "MSG": 4, "PRIV": 4, "COLOR": 2, "NAME": 2, "PING": 3, "PONG": 3,
        "FILE_OFFER": 6, "FILE_DECLINE": 3, "FILE_ACCEPT": 3, "FILE": 5,
        "FILE_ACK": 4, "KEY": 2, "GROUP_CREATE": 4, "GROUP_MSG": 5,
    }.get(packet_type, 4)
    parts = raw.split("|", split_at)

    dispatch = _PACKET_HANDLERS.get(parts[0])
    if dispatch is not None:
        expected_len, handler = dispatch
        if len(parts) == expected_len:
            handler(parts, is_direct, ip, signed_line, tag)
            return

    ui_print(sanitize_for_display(raw))
    ui_print("> ", end="", flush=True)


def receive(peer, address):
    ip = address[0]
    buffer = b""

    # Use a periodic timeout (instead of blocking forever) so this thread
    # notices `running` going False and can't be leaked if a peer stops
    # responding without closing the socket.
    try:
        peer.settimeout(60)
    except OSError:
        pass

    while running:
        try:
            lines, buffer = recv_lines(peer, buffer)

            if lines is None:
                if DEBUG_DISCOVERY:
                    ui_print(f"[receive] {ip} peer closed the connection (recv returned empty)")
                break  # peer closed the connection

            for line in lines:
                if not line:
                    continue
                # File packets are usually wrapped in a RELAY envelope now
                # (RELAY|packet_id|ttl|tag|FILE|...) rather than arriving
                # bare, since delivery to a possibly-multi-hop-away peer
                # goes through the flood/relay path. Classify past the
                # envelope so file chunks (and their acks) still land in
                # the high-throughput bucket instead of being throttled
                # by the much stricter chat rate limit (which would
                # otherwise silently drop chunks and hang the transfer,
                # see file_rate_buckets above).
                #
                # The envelope is RELAY|packet_id|ttl|tag|inner_line (5
                # parts) since the HMAC signature tag was added, split
                # into 5, not 4, or envelope_parts[3] ends up being the
                # tag itself (a 16-char hex string) instead of the inner
                # line, and every relayed FILE*/FILE_ACK packet is
                # silently misclassified as ordinary chat traffic and
                # throttled far too aggressively for a per-chunk control
                # stream.
                classify_target = line
                if classify_target.startswith(b"RELAY|"):
                    envelope_parts = classify_target.split(b"|", 4)
                    if len(envelope_parts) == 5:
                        classify_target = envelope_parts[4]
                is_file_packet = classify_target.startswith(
                    (b"FILE|", b"FILE_OFFER|", b"FILE_ACCEPT|", b"FILE_DECLINE|", b"FILE_ACK|")
                )
                limited = file_rate_limited(ip) if is_file_packet else rate_limited(ip)
                if not limited:
                    handle_incoming(line.decode(errors="replace"), ip)

        except socket.timeout:
            continue
        except (ConnectionError, OSError) as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[receive] {ip} ConnectionError/OSError: {e!r}")
            break
        except Exception as e:
            ui_print(f"\nUnexpected error in receive: {e}")
            break

    with lock:
        removed_peer = peers.pop(ip, None)
    if removed_peer:
        try:
            removed_peer.close()
        except OSError:
            pass

    with attempted_lock:
        attempted_connections.discard(ip)
        # Give a short cooldown before this ip is eligible for a fresh
        # discovery-triggered dial, rather than making it instantly
        # eligible again. Without this, a connection that gets reset
        # immediately after connecting (e.g. two addresses racing for the
        # same underlying identity, or a genuine transient network blip)
        # gets re-dialed on literally the next discovery announce, which
        # can arrive within a second or two, turning one bad connection
        # into a tight reconnect loop instead of backing off.
        deferred_until[ip] = time.time() + DISCONNECT_RETRY_COOLDOWN

    with rate_lock:
        rate_buckets.pop(ip, None)

    with file_rate_lock:
        file_rate_buckets.pop(ip, None)

    with names_lock:
        peer_id = ip_to_id.pop(ip, None)

    if peer_id:
        with color_lock:
            id_colors.pop(peer_id, None)
        with names_lock:
            peer_names.pop(peer_id, None)
        with peer_keys_lock:
            # Drop the session key we had bound to this peer_id so that if
            # they reconnect (a fresh TCP connection, possibly after
            # restarting dechat) they can establish a new key via a fresh
            # direct KEY packet, keys are per-run, not persistent, by
            # design (see MY_SESSION_KEY). This is safe: a new key can
            # only be learned from a *direct* KEY packet (learn_key()
            # rejects relayed ones), so a stranger can't hijack peer_id by
            # racing to claim it over relay right after this disconnect
            # they'd need an actual direct socket connection, which is the
            # same trust boundary claim_identity() already polices.
            peer_session_keys.pop(peer_id, None)

    ui_print(f"\nDisconnected: {ip}")
    ui_print("> ", end="", flush=True)

    with known_ips_lock:
        should_retry = ip in known_ips
        if should_retry:
            if ip in reconnecting_ips:
                # A reconnect_worker for this ip is already running (e.g.
                # from a prior flappy disconnect); don't start a duplicate.
                should_retry = False
            else:
                reconnecting_ips.add(ip)
    if should_retry and running:
        threading.Thread(target=reconnect_worker, args=(ip,), daemon=True).start()


def reconnect_worker(ip):
    try:
        attempt = 0
        while running and attempt < MAX_RECONNECT_ATTEMPTS:
            # Randomized delay rather than a fixed one: if many peers drop
            # at once (e.g. a shared WiFi hiccup affecting lots of clients
            # simultaneously), a fixed delay means every affected node
            # retries at exactly the same instant, compounding load right
            # when the network is already stressed. Jitter spreads the
            # retries out over the window instead.
            time.sleep(random.uniform(RECONNECT_DELAY_MIN, RECONNECT_DELAY_MAX))
            if not running:
                return
            with known_ips_lock:
                if ip not in known_ips:
                    return
            with lock:
                if ip in peers:
                    return
            attempt += 1
            connect_to_peer(ip)
            with lock:
                if ip in peers:
                    return
    finally:
        with known_ips_lock:
            reconnecting_ips.discard(ip)


def reaper():
    """Periodically clears out abandoned file transfers, pings that never
    got a response, stale routing entries, and old packet-id dedup
    records, so none of them accumulate in memory forever."""
    global incoming_bytes_buffered
    while running:
        time.sleep(10)
        now = time.time()

        with transfer_lock:
            stale_keys = [
                key for key, entry in incoming_transfers.items()
                if now - entry["started"] > TRANSFER_TIMEOUT
            ]
            for key in stale_keys:
                stale_entry = incoming_transfers.pop(key, None)
                if stale_entry is not None:
                    incoming_bytes_buffered -= stale_entry.get("bytes", 0)

        with ping_lock:
            stale_pings = [
                pid for pid, sent in pending_pings.items()
                if now - sent > PING_TIMEOUT
            ]
            for pid in stale_pings:
                pending_pings.pop(pid, None)

        with offer_lock:
            stale_offers = [
                oid for oid, offer in pending_offers.items()
                if now - offer["started"] > OFFER_TIMEOUT
            ]
            for oid in stale_offers:
                pending_offers.pop(oid, None)

        with seen_lock:
            stale_ids = [
                pid for pid, seen_at in seen_msg_ids.items()
                if now - seen_at > SEEN_ID_MAX_AGE
            ]
            for pid in stale_ids:
                seen_msg_ids.pop(pid, None)

        with route_lock:
            stale_routes = [
                pid for pid, (_, learned_at) in next_hop_for_peer.items()
                if now - learned_at > ROUTE_STALE_AFTER
            ]
            for pid in stale_routes:
                next_hop_for_peer.pop(pid, None)


def accept_connections(server):
    while running:
        try:
            peer, address = server.accept()
        except OSError:
            break

        ip = address[0]

        with lock:
            if ip in peers:
                peer.close()
                continue
            # Inbound connections get a somewhat higher ceiling than
            # MAX_PEER_DEGREE (which mainly governs our own *outbound*
            # dialing, see at_degree_cap()/discovery()). Refusing all
            # inbound the instant we hit the same cap would let two nodes
            # each sitting right at the cap simply be unable to reach each
            # other, and in the worst case could fragment the mesh into
            # disconnected islands. A modest extra allowance keeps the
            # graph well-connected without going back to unbounded O(n^2).
            if len(peers) >= MAX_PEER_DEGREE * 2:
                peer.close()
                continue
            peers[ip] = peer

        with attempted_lock:
            deferred_until.pop(ip, None)

        with known_ips_lock:
            known_ips.add(ip)

        ui_print(f"\nConnected: {ip}")
        ui_print("> ", end="", flush=True)

        # Sent bare (never wrapped in RELAY) and first, so the peer at the
        # other end can bind our session key before anything we sign
        # arrives, see learn_key()/learn_or_verify_key() above. A bare
        # line is always treated as direct by handle_incoming().
        send_line_to_peer(MY_ID, peer, ip, f"KEY|{MY_ID}|{MY_SESSION_KEY.hex()}")

        idx_num = color_index_for_id(MY_ID)  # thread-safe; assigns one if missing
        send_line_to_peer(MY_ID, peer, ip, wrap_relay(f"COLOR|{MY_ID}|{idx_num}"))
        send_line_to_peer(MY_ID, peer, ip, wrap_relay(f"NAME|{MY_ID}|{escape_field(MY_NAME)}"))

        threading.Thread(
            target=receive,
            args=(peer, address),
            daemon=True
        ).start()


def connect_to_peer(ip, remote_id=None):
    with lock:
        if ip in peers:
            return

    # If we already know this peer's id (e.g. from a discovery broadcast)
    # and it's "greater" than ours, let them be the one to dial us instead.
    # Without this tie-break, two peers that see each other's broadcast at
    # nearly the same time both dial out simultaneously; whichever socket
    # loses the peers[ip] race gets closed immediately in
    # accept_connections(), which looks like an instant disconnect and
    # retriggers discovery/reconnect on the losing side, over and over.
    if remote_id and remote_id > MY_ID:
        with attempted_lock:
            deferred_until[ip] = time.time() + DEFER_RETRY_COOLDOWN
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] deferring to {ip} (their id {remote_id} > ours {MY_ID}); waiting for them to dial in")
        return

    # Bound how many connect() calls can be in flight across the whole
    # process at once (see MAX_CONCURRENT_CONNECT_ATTEMPTS). A burst of
    # near-simultaneous discovery announces at scale could otherwise try to
    # open hundreds of sockets/threads at the same moment. This blocks the
    # calling thread (each caller already runs on its own short-lived
    # thread, see discovery()/reconnect_worker()), so excess attempts
    # simply queue up rather than piling on all at once.
    acquired = connect_attempt_semaphore.acquire(timeout=10)
    if not acquired:
        with attempted_lock:
            attempted_connections.discard(ip)
            deferred_until.pop(ip, None)
        return

    try:
        try:
            peer = socket.socket()
            peer.settimeout(3)
            peer.connect((ip, CHAT_PORT))
            # Leave a timeout in place (set again inside receive()) rather than
            # switching to blocking mode, so the receive thread can't block
            # forever on an unresponsive peer.
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            with lock:
                if ip in peers:
                    peer.close()
                    return
                peers[ip] = peer

            with known_ips_lock:
                known_ips.add(ip)

            ui_print(f"\nConnected: {ip}")
            ui_print("> ", end="", flush=True)

            # See the matching comment in accept_connections(): bare, sent
            # first, so our session key is on file before any signed
            # packet from us arrives.
            send_line_to_peer(MY_ID, peer, ip, f"KEY|{MY_ID}|{MY_SESSION_KEY.hex()}")

            idx_num = color_index_for_id(MY_ID)  # thread-safe; assigns one if missing
            send_line_to_peer(MY_ID, peer, ip, wrap_relay(f"COLOR|{MY_ID}|{idx_num}"))
            send_line_to_peer(MY_ID, peer, ip, wrap_relay(f"NAME|{MY_ID}|{escape_field(MY_NAME)}"))

            threading.Thread(
                target=receive,
                args=(peer, (ip, CHAT_PORT)),
                daemon=True
            ).start()

        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] connect_to_peer({ip}) OSError: {e}")
            with attempted_lock:
                attempted_connections.discard(ip)
                deferred_until.pop(ip, None)
    finally:
        connect_attempt_semaphore.release()


def at_degree_cap():
    """True if we already have as many direct connections as
    MAX_PEER_DEGREE allows. Below this, we behave exactly like the old
    full-mesh design (connect to everyone we discover); above it, new
    discoveries are left unconnected and rely on the existing mesh to
    relay their traffic instead (see wrap_relay/relay_onward)."""
    with lock:
        return len(peers) >= MAX_PEER_DEGREE


def get_local_ip():
    """Returns this machine's real LAN-facing IP address, used to compute
    the subnet broadcast address for discovery.

    socket.gethostbyname(socket.gethostname()) is unreliable for this:
    on many Linux systems (especially Debian/Ubuntu-based), /etc/hosts
    maps the machine's own hostname to 127.0.0.1 or 127.0.1.1, which
    silently produces a useless "127.0.0.255" broadcast target and
    leaves discovery depending entirely on 255.255.255.255, which
    plenty of routers/switches don't forward the way you'd expect.

    Instead, open a UDP socket and connect() it to an arbitrary external
    address. UDP connect() does not actually send any packets; it just
    asks the OS to pick a local (source) address for that route, which
    is reliably the real LAN-facing IP regardless of /etc/hosts."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def announce_interval_for_known_peers():
    """Adaptive UDP announce cadence. Scales up smoothly with how many
    distinct peers we've seen recently, instead of a fixed 3s for every
    network size:
      - Small networks (a handful of peers): stays fast (near the old 3s),
        so joining/discovery still feels near-instant and never "takes
        ages" to connect, exactly like the original behavior.
      - Large networks (hundreds of peers): backs off toward a capped
        maximum, so total broadcast traffic doesn't keep scaling linearly
        with peer count forever (300 peers at a fixed 3s cadence is ~100
        broadcast packets/sec network-wide, which is a real, continuous
        tax on WiFi airtime). The cap keeps it from ever backing off so
        far that a new joiner has to wait a long time to be noticed.
    """
    with names_lock:
        known = len(ip_to_id)
    # Roughly: 3s up to ~15 peers, scaling up to ~10s by ~300 peers, capped
    # there so it never gets slower no matter how large the network gets.
    interval = 3 + (known / 40.0)
    return min(interval, 10)


def _broadcast_announce(udp, broadcast_targets):
    """Sends one "CHAT:<MY_ID>" announcement to every broadcast target.
    Extracted from discovery()'s main loop; failures on one target don't
    stop the others."""
    message = f"CHAT:{MY_ID}".encode()
    for target in broadcast_targets:
        try:
            udp.sendto(message, (target, DISCOVERY_PORT))
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] sent announce {message} to {target}")
        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] broadcast to {target} failed: {e}")


def _process_discovery_announce(peer_id, ip):
    """Handles one "CHAT:<peer_id>" announcement heard from `ip` on the
    discovery socket: decides whether we should attempt a direct
    connection to it, and does so if appropriate. Extracted from
    discovery()'s main loop, see the comments below (moved verbatim from
    there) for why each check exists."""
    if peer_id == MY_ID:
        return

    remember_peer_session(ip, peer_id)
    remembered_name = cached_peer_name(ip)
    if remembered_name:
        with names_lock:
            peer_names.setdefault(peer_id, remembered_name)

    with names_lock:
        announced_peer_ids[ip] = peer_id

    with attempted_lock:
        now = time.time()
        still_deferred = deferred_until.get(ip, 0) > now
        if ip in attempted_connections or still_deferred:
            if DEBUG_DISCOVERY:
                reason = "deferred cooldown active" if still_deferred else "already attempted"
                ui_print(f"[discovery] {ip} skipped ({reason})")
            return
        attempted_connections.add(ip)

    with lock:
        known = ip in peers

    with names_lock:
        # If this exact peer_id is already connected via a
        # *different* IP, or we've already heard a discovery
        # announcement from a different IP for this same peer_id
        # and haven't ruled it out yet, don't open a second
        # connection to it. This happens when one machine has
        # multiple active network interfaces (e.g. Ethernet +
        # WiFi) on the same LAN: it broadcasts the same MY_ID from
        # each address, and without this check we'd treat each
        # address as a distinct peer, connect to both, and then
        # flap endlessly as the "duplicate" identity gets detected
        # and torn down. Checking announced_peer_ids in addition to
        # ip_to_id closes the race where the first connection is
        # still mid-handshake (so ip_to_id doesn't have it yet)
        # when the second address's announcement arrives.
        already_connected_elsewhere = any(
            other_id == peer_id and other_ip != ip
            for other_ip, other_id in ip_to_id.items()
        )
        already_announced_elsewhere = any(
            other_id == peer_id and other_ip != ip
            for other_ip, other_id in announced_peer_ids.items()
        )

    if already_connected_elsewhere:
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] {ip} skipped (peer id {peer_id} already connected via another address)")
        with attempted_lock:
            attempted_connections.discard(ip)
        return

    if already_announced_elsewhere:
        # Same identity heard from more than one address and
        # neither is connected yet, pick one deterministically
        # (lexicographically smallest IP) so we don't race our own
        # other candidate connection, and so every peer on the
        # network converges on the same choice for this identity
        # instead of each picking differently.
        with names_lock:
            candidates = sorted(
                a for a, i in announced_peer_ids.items() if i == peer_id
            )
        preferred = candidates[0] if candidates else ip
        if ip != preferred:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] {ip} skipped (peer id {peer_id} also seen at {preferred}; preferring that address)")
            with attempted_lock:
                attempted_connections.discard(ip)
            return

    if not known and at_degree_cap():
        # We already have as many direct connections as we're
        # willing to hold (see MAX_PEER_DEGREE). Don't dial this
        # one, their traffic will still reach us via relay
        # through whichever peers we *are* connected to. This is
        # the crux of what keeps 300 peers from becoming a 44,850
        # edge full mesh: past a modest peer count, discovery stops
        # actively growing the connection graph and just lets the
        # existing mesh do the routing.
        with attempted_lock:
            # Release this ip's "attempted" mark quickly instead of
            # holding it under the full cooldown, since we didn't
            # actually try to connect, our degree may free up
            # again shortly (a neighbor disconnecting) and we'd
            # like to be able to reconsider this ip then.
            attempted_connections.discard(ip)
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] {ip} skipped (at connection cap, will rely on relay)")
        return

    if not known:
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] attempting connect_to_peer({ip})")
        connect_to_peer(ip, remote_id=peer_id)
        if DEBUG_DISCOVERY:
            with lock:
                ok = ip in peers
            if ok:
                ui_print(f"[discovery] connect_to_peer({ip}) succeeded")
            elif peer_id > MY_ID:
                ui_print(f"[discovery] connect_to_peer({ip}) deferred (expected -- waiting for them to dial in)")
            else:
                ui_print(f"[discovery] connect_to_peer({ip}) failed")


def discovery():
    global discovery_socket
    udp = None
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("", DISCOVERY_PORT))
        udp.settimeout(1)
        discovery_socket = udp
    except OSError as e:
        discovery_socket = None
        if udp is not None:
            try:
                udp.close()
            except OSError:
                pass
        ui_print(
            f"Peer discovery is unavailable on UDP port {DISCOVERY_PORT}: {e}"
        )
        return

    broadcast_targets = ["255.255.255.255"]
    local_ip = get_local_ip()
    if local_ip:
        parts = local_ip.split(".")
        if len(parts) == 4:
            subnet_broadcast = ".".join(parts[:3] + ["255"])
            if subnet_broadcast not in broadcast_targets:
                broadcast_targets.append(subnet_broadcast)

    if DEBUG_DISCOVERY:
        ui_print(f"[discovery] listening on UDP {DISCOVERY_PORT}, MY_ID={MY_ID}")
        ui_print(f"[discovery] local_ip={local_ip}")
        ui_print(f"[discovery] broadcast targets: {broadcast_targets}")

    last_announcement = 0

    while running:
        current_time = time.time()

        if current_time - last_announcement > announce_interval_for_known_peers():
            _broadcast_announce(udp, broadcast_targets)
            last_announcement = current_time

        try:
            data, address = udp.recvfrom(1024)
            message = data.decode(errors="replace")

            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] recv {message!r} from {address}")

            if not message.startswith("CHAT:"):
                continue

            _process_discovery_announce(message[5:], address[0])

        except socket.timeout:
            with attempted_lock:
                if len(attempted_connections) > ANTI_FLOOD_SET_MAX:
                    attempted_connections.clear()
                now = time.time()
                expired = [ip for ip, until in deferred_until.items() if until <= now]
                for ip in expired:
                    deferred_until.pop(ip, None)
                if len(deferred_until) > ANTI_FLOOD_SET_MAX:
                    deferred_until.clear()
        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] recvfrom failed, exiting loop: {e}")
            break

    udp.close()


def send_to_all(message, exclude_ip=None):
    """Sends to every directly-connected neighbor (optionally skipping the
    one a relayed packet arrived from). With a bounded-degree mesh this is
    no longer "everyone in the network", it's "my direct neighbors",
    and flooding (see relay_packet) is what gets a message the rest of the
    way across the network."""
    with lock:
        items = list(peers.items())

    for ip, peer in items:
        if ip == exclude_ip:
            continue
        try:
            send_line(peer, message)
        except (ConnectionError, OSError):
            drop_peer(ip)


def mark_seen(packet_id):
    """Records that we've now relayed/handled this packet id. Returns True
    if it was NOT already seen (i.e. this call is the one that should act
    on it), False if it's a duplicate that arrived via another path in the
    mesh and should be ignored.

    Entries store the time they were first seen so reaper() can evict
    ones old enough that a genuine duplicate is no longer plausible (see
    SEEN_ID_MAX_AGE below), in addition to the existing capacity-based
    FIFO eviction. Capacity alone meant a single large file transfer
    (potentially tens of thousands of chunks, each getting its own packet
    id) could fill the entire table and evict recent, still-relevant
    entries from unrelated chat/control traffic well before they'd have
    aged out naturally, age-based reaping keeps the table's actual
    working set closer to "recently active" rather than just "most
    recently inserted, regardless of type or volume of traffic."""
    with seen_lock:
        if packet_id in seen_msg_ids:
            return False
        seen_msg_ids[packet_id] = time.time()
        if len(seen_msg_ids) > MAX_SEEN_IDS:
            oldest = next(iter(seen_msg_ids))
            seen_msg_ids.pop(oldest, None)
        return True


def new_packet_id():
    return uuid.uuid4().hex[:12]


def wrap_relay(inner_line):
    """Wraps a fully-formed protocol line (e.g. "MSG|...") in a
    RELAY envelope carrying a fresh packet id, hop-count, and an HMAC
    signature over inner_line keyed by our own session key, so it can be
    flooded across the mesh with duplicate suppression AND verified by
    every recipient as genuinely having come from us (see
    learn_or_verify_key/sign_packet above). Every packet type that needs
    to reach more than a direct neighbor (broadcast chat, and every
    point-to-point type since the target may be several hops away) goes
    out wrapped like this."""
    packet_id = new_packet_id()
    mark_seen(packet_id)  # we originated it; never re-relay our own echo
    tag = sign_packet(inner_line)
    return f"RELAY|{packet_id}|{RELAY_TTL}|{tag}|{inner_line}"


def relay_onward(packet_id, ttl, tag, inner_line, from_ip):
    """Floods `inner_line` onward to every direct neighbor except the one
    it arrived from, decrementing ttl and preserving the original
    signature tag untouched (relays must never re-sign, only the
    original origin's key can produce a tag that verifies, which is the
    whole point). No-ops once ttl is exhausted. This is what lets a
    point-to-point or broadcast packet reach a peer that isn't a direct
    connection: every node in the mesh acts as a router for every other
    node's traffic. Caller is responsible for having already checked
    mark_seen()."""
    if ttl <= 0:
        return
    send_to_all(f"RELAY|{packet_id}|{ttl - 1}|{tag}|{inner_line}", exclude_ip=from_ip)


def list_peers():
    with lock:
        ips = list(peers.keys())

    if not ips:
        ui_print("No peers connected.")
    else:
        ui_print("Connected peers:")
        for ip in ips:
            with names_lock:
                peer_id = ip_to_id.get(ip)
                name = peer_names.get(peer_id) if peer_id else None
            if peer_id:
                c = color_for_id(peer_id)
                with color_lock:
                    color_idx = id_colors.get(peer_id, "?")
                display_name = name or "(unknown)"
                ui_print(f"  {c}{display_name}{RESET}  id={peer_id}  ip={ip}  color={color_idx}")
            else:
                ui_print(f"  (unidentified)  ip={ip}")


def new_msg_id():
    return uuid.uuid4().hex[:4]


def remember_message(msg_id, sender_name, text):
    with msg_history_lock:
        msg_history[msg_id] = (sender_name, text)
        if len(msg_history) > MAX_MSG_HISTORY:
            oldest = next(iter(msg_history))
            msg_history.pop(oldest, None)


def get_remembered_message(msg_id):
    with msg_history_lock:
        return msg_history.get(msg_id)


def list_recent_messages(count=10):
    with msg_history_lock:
        items = list(msg_history.items())[-count:]
    if not items:
        ui_print("No recent messages to show.")
        return
    ui_print(f"Last {len(items)} message(s):")
    for msg_id, (sender_name, text) in items:
        ui_print(f"  [{msg_id}] {sender_name}: {text}")


def mentions_me(text):
    lowered = text.lower()
    return f"@{MY_NAME.lower()}" in lowered


def resolve_peer_id(target):
    """Resolves a user-typed name or peer_id (as used by /msg, /ping,
    /sendfile, /ignore, etc.) to a single peer_id, or None if it can't be
    resolved to exactly one.

    If `target` is an 8-char id, that's returned directly. Otherwise
    `target` is looked up as a display name, but if more than one
    currently-known peer shares that display name, this now reports the
    ambiguity to the user and returns None, rather than silently picking
    whichever one happened to be first in dict iteration order: a message
    typed for one "Alice" could previously be silently delivered to a
    *different* "Alice" (e.g. an impersonator who deliberately reused a
    name they saw in chat) with no indication anything went wrong.
    Prints its own error message in both the "not found" and "ambiguous"
    cases, so callers can simply check for None and return."""
    with names_lock:
        direct_ids = set(ip_to_id.values())
        if len(target) == 8 and (target in peer_names or target in direct_ids):
            return target
        matches = [pid for pid, name in peer_names.items() if name == target]
    if len(target) == 8:
        # A peer may be reachable only through the mesh and therefore have no
        # direct ip_to_id binding on this node. A fresh learned route is enough
        # to treat an exact 8-character id as reachable.
        with route_lock:
            route = next_hop_for_peer.get(target)
        if route is not None and time.time() - route[1] <= ROUTE_STALE_AFTER:
            return target

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(matches)
        ui_print(
            f"'{target}' matches more than one connected peer ({ids}). "
            f"Use the exact id instead of the name to be sure you reach "
            f"the right one."
        )
        return None
    ui_print(f"No known peer named or identified as '{target}'.")
    return None


def send_ping(target):
    peer_id = resolve_peer_id(target)

    if not peer_id:
        return

    sent_time = time.time()
    with ping_lock:
        pending_pings[peer_id] = sent_time

    # peer_id may be several hops away now rather than a direct connection
    # -- send_to_peer_id floods it across the mesh; every hop forwards it
    # onward automatically until it reaches the node whose id matches.
    if send_to_peer_id(f"PING|{peer_id}|{MY_ID}|{sent_time}"):
        ui_print(f"Pinging {target}...")
    else:
        with ping_lock:
            pending_pings.pop(peer_id, None)
        ui_print("Failed to send ping (no peers connected).")


def send_private_message(target, text):
    peer_id = resolve_peer_id(target)

    if not peer_id:
        return False

    my_color = color_index_for_id(MY_ID)
    with names_lock:
        resolved_name = peer_names.get(peer_id, target)
    log_message(f"{timestamp()} [private to {resolved_name}] {MY_NAME}: {text}")

    ok = send_to_peer_id(
        f"PRIV|{peer_id}|{MY_ID}|{my_color}|{escape_field(MY_NAME)}|{escape_field(text)}"
    )
    if not ok:
        ui_print("Failed to send private message (no peers connected).")
        return False
    if UI_MODE == "gui":
        dm_ip = stable_peer_ip(peer_id)
        ui_print("@@DECHAT_DM@@" + json.dumps({
            "peer_id": peer_id, "dm_key": dm_ip, "name": resolved_name, "text": text,
            "color_idx": my_color, "outgoing": True,
        }, separators=(",", ":")))
    else:
        ui_print(f"{timestamp()} [private to {resolved_name}] {COLORS[my_color]}{MY_NAME}{RESET}: {text}")
    return True


def create_group_chat(name, member_ids):
    name = name.strip()
    members = set(member_ids)
    members.add(MY_ID)
    if not name or len(name) > MAX_GROUP_NAME_LEN:
        ui_print(f"Group name must be 1-{MAX_GROUP_NAME_LEN} characters.")
        return None
    if not (2 <= len(members) <= MAX_GROUP_MEMBERS):
        ui_print(f"A group must contain 2-{MAX_GROUP_MEMBERS} members including you.")
        return None
    with names_lock:
        known_ids = set(peer_names) | {MY_ID}
    if not members.issubset(known_ids):
        ui_print("Could not create group because one or more selected peers are no longer known.")
        return None
    group_id = uuid.uuid4().hex[:12]
    definition = {"name": name, "creator_id": MY_ID, "members": members}
    with group_lock:
        group_chats[group_id] = definition
    member_csv = ",".join(sorted(members))
    send_to_all(wrap_relay(
        f"GROUP_CREATE|{MY_ID}|{group_id}|{escape_field(name)}|{member_csv}"
    ))
    if UI_MODE == "gui":
        ui_print("@@DECHAT_GROUP_CREATE@@" + json.dumps({
            "group_id": group_id, "name": name, "creator_id": MY_ID,
            "members": sorted(members),
        }, separators=(",", ":")))
    return group_id


def send_group_message(group_id, text):
    with group_lock:
        group = group_chats.get(group_id)
        if group is None or MY_ID not in group["members"]:
            ui_print("That group is no longer available.")
            return False
        group_name = group["name"]
    my_color = color_index_for_id(MY_ID)
    inner = (
        f"GROUP_MSG|{group_id}|{MY_ID}|{my_color}|"
        f"{escape_field(MY_NAME)}|{escape_field(text)}"
    )
    with lock:
        has_peers = bool(peers)
    if not has_peers:
        ui_print("Failed to send group message (no peers connected).")
        return False
    send_to_all(wrap_relay(inner))
    log_message(f"{timestamp()} [group {group_name}] {MY_NAME}: {text}")
    if UI_MODE == "gui":
        ui_print("@@DECHAT_GROUP_MSG@@" + json.dumps({
            "group_id": group_id, "sender_id": MY_ID,
            "sender_name": MY_NAME, "color_idx": my_color,
            "text": text, "outgoing": True,
        }, separators=(",", ":")))
    else:
        ui_print(f"{timestamp()} [group {group_name}] {COLORS[my_color]}{MY_NAME}{RESET}: {text}")
    return True


def ignore_peer(target):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        return
    with ignore_lock:
        ignored_ids.add(peer_id)
    ui_print(f"Ignoring messages from {target}.")


def unignore_peer(target):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        # target might itself be a raw peer_id that's no longer resolvable
        # to a name (e.g. the peer disconnected and was purged from
        # peer_names). Only report success if it was actually removed.
        with ignore_lock:
            was_ignored = target in ignored_ids
            ignored_ids.discard(target)
        if was_ignored:
            ui_print(f"No longer ignoring '{target}'.")
        else:
            ui_print(f"'{target}' was not being ignored.")
        return
    with ignore_lock:
        ignored_ids.discard(peer_id)
    ui_print(f"No longer ignoring {target}.")


def list_ignored():
    with ignore_lock:
        ids = list(ignored_ids)
    if not ids:
        ui_print("Not ignoring anyone.")
        return
    ui_print("Ignored peers:")
    for pid in ids:
        with names_lock:
            name = peer_names.get(pid, pid)
        ui_print(f"  {name} ({pid})")


def human_size(num_bytes):
    """Formats a byte count as a human-readable string ("2.3 MB", etc.).
    Uses the *last* entry in UNITS as a catch-all for anything too big to
    fit the earlier ones, rather than hardcoding which unit name that is,
    so appending a bigger unit (e.g. "TB") to the tuple later just
    works, instead of silently falling through and returning None the
    way a hardcoded `unit == "GB"` check would the moment "GB" stops
    being the last entry."""
    size = float(num_bytes)
    units = ("B", "KB", "MB", "GB")
    for i, unit in enumerate(units):
        is_last = (i == len(units) - 1)
        if size < 1024 or is_last:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def register_offer(sender_id, offer_id, filename, filesize, file_hash=None):
    """Must be called with offer_lock held. Stores an incoming offer and
    returns the small display number the user will type into /accept.
    file_hash is the sender's claimed SHA-256 (hex) of the whole file,
    used to verify integrity once all chunks are reassembled, None if
    the offer didn't include a valid one (e.g. an old/other client)."""
    global next_offer_num
    num = next_offer_num
    next_offer_num += 1
    offer_num_to_id[num] = offer_id
    pending_offers[offer_id] = {
        "sender_id": sender_id,
        "filename": filename,
        "filesize": filesize,
        "file_hash": file_hash,
        "started": time.time(),
        "accepted": None,
        "num": num,
    }
    return num


def resolve_offer(ref):
    """Resolves a user-typed /accept or /reject argument (a small display
    number, or a raw offer_id) to (offer_id, offer_dict)."""
    with offer_lock:
        if ref.isdigit():
            offer_id = offer_num_to_id.get(int(ref))
        else:
            offer_id = ref if ref in pending_offers else None
        if offer_id is None:
            return None, None
        return offer_id, pending_offers.get(offer_id)


def accept_offer(ref):
    offer_id, offer = resolve_offer(ref)
    if not offer:
        ui_print(f"No pending file offer '{ref}'.")
        return
    with offer_lock:
        offer["accepted"] = True
    # The sender may be several hops away rather than a direct connection;
    # send_to_peer_id floods the acceptance across the mesh to them.
    ok = send_to_peer_id(f"FILE_ACCEPT|{offer['sender_id']}|{MY_ID}|{offer_id}")
    if not ok:
        ui_print("No peers connected; can't reach that sender right now.")
        with offer_lock:
            pending_offers.pop(offer_id, None)
        return
    ui_print(f"Accepted '{offer['filename']}'. Receiving...")


def reject_offer(ref):
    offer_id, offer = resolve_offer(ref)
    if not offer:
        ui_print(f"No pending file offer '{ref}'.")
        return
    with offer_lock:
        pending_offers.pop(offer_id, None)
    send_to_peer_id(f"FILE_DECLINE|{offer['sender_id']}|{MY_ID}|{offer_id}")
    ui_print(f"Declined '{offer['filename']}'.")


def list_pending_offers():
    with offer_lock:
        items = sorted(pending_offers.items(), key=lambda kv: kv[1]["num"])
    if not items:
        ui_print("No pending file offers.")
        return
    ui_print("Pending file offers:")
    for offer_id, offer in items:
        with names_lock:
            name = peer_names.get(offer["sender_id"], offer["sender_id"])
        ui_print(f"  [{offer['num']}] '{offer['filename']}' ({human_size(offer['filesize'])}) from {name}")


def safe_display_filename(filepath):
    """Extracts just the filename component for display/advertising to a
    peer, independent of which OS produced the path. The old version only
    split on '/', which left the *entire* path intact (including any
    Windows drive letter/backslash-separated directories, e.g.
    "C:\\Users\\alice\\secret\\report.docx") when running on Windows or
    when handed a Windows-style path, exposing local directory
    structure to the recipient and only getting caught (if at all) by the
    receiver's own separate sanitization. Using os.path.basename with
    both separators normalized first strips directory info from either
    style of path, on any platform."""
    normalized = filepath.replace("\\", "/")
    return os.path.basename(normalized) or "file"


def hash_file(filepath):
    """Returns the hex SHA-256 of a file's contents, read in fixed-size
    chunks so hashing a large file doesn't require loading it all into
    memory at once. Used so the receiver can verify the reassembled file
    matches exactly what was sent, and detect corruption (or tampering by
    a misbehaving relay hop) that would otherwise go completely unnoticed,
    see the FILE_OFFER/FILE handling in handle_incoming()."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def send_file(target, filepath):
    peer_id = resolve_peer_id(target)

    if not peer_id:
        return

    try:
        file_size = os.path.getsize(filepath)
    except OSError as e:
        ui_print(f"Could not read file: {e}")
        return

    try:
        file_hash = hash_file(filepath)
    except OSError as e:
        ui_print(f"Could not read file: {e}")
        return

    filename = safe_display_filename(filepath)
    offer_id = uuid.uuid4().hex[:8]

    with pending_sends_lock:
        pending_sends[offer_id] = filepath

    ok = send_to_peer_id(
        f"FILE_OFFER|{peer_id}|{MY_ID}|{offer_id}|{escape_field(filename)}|{file_size}|{file_hash}"
    )
    if ok:
        ui_print(f"Offered '{filename}' ({human_size(file_size)}) to {target}. Waiting for them to accept...")
    else:
        with pending_sends_lock:
            pending_sends.pop(offer_id, None)
        ui_print("Failed to send file offer (no peers connected).")


def stream_file(peer_id, offer_id, filepath):
    # File chunks are sent routed (unicast hop-by-hop along a learned
    # reverse path when one is known, see send_routed_to_peer_id()),
    # falling back to a mesh-wide flood only when no path is known yet or
    # it's gone stale. This keeps per-transfer bandwidth close to O(path
    # length) instead of O(mesh edges) in the common case, while still
    # working (just less efficiently) if routing info isn't available.
    #
    # Chunks are sent in a bounded sliding window and each one must be
    # acknowledged (FILE_ACK, see record_chunk_ack()) before we consider
    # it delivered. Anything still un-acked after a short timeout is
    # resent, up to a retry cap, so a chunk lost anywhere along the way
    # (rate limiting, a lossy hop, TTL exhaustion) gets recovered
    # automatically instead of leaving the receiver stuck with a
    # permanently incomplete file until it times out.
    try:
        file_size = os.path.getsize(filepath)
    except OSError as e:
        ui_print(f"\nCould not read file: {e}")
        ui_print("> ", end="", flush=True)
        return

    chunk_size = 1024
    total = max(1, (file_size + chunk_size - 1) // chunk_size)
    filename = safe_display_filename(filepath)

    if total > MAX_FILE_CHUNKS:
        # The receiver enforces this same cap and will simply drop chunks
        # past it, so a transfer this large would appear to "hang" with
        # no explanation. Fail immediately and tell the user why, instead
        # of streaming a doomed transfer.
        ui_print(
            f"\n'{filename}' is too large to send ({human_size(file_size)}); "
            f"the limit is {human_size(MAX_FILE_CHUNKS * chunk_size)}."
        )
        ui_print("> ", end="", flush=True)
        return

    WINDOW_SIZE = 32          # chunks in flight at once
    ACK_WAIT_SECONDS = 2.0    # how long to wait for acks before resending
    MAX_RETRIES_PER_CHUNK = 10

    try:
        with open(filepath, "rb") as f:
            # Read every chunk up front into a list. This app already
            # caps transfers at MAX_FILE_CHUNKS (~64MB at 1KB/chunk) via
            # the receiver's check on the same total; holding the whole
            # file in memory for the duration of a send is consistent
            # with that existing bound and keeps re-sending a chunk on
            # timeout simple (no seek/re-read bookkeeping).
            chunks = []
            while True:
                chunk = f.read(chunk_size)
                if not chunk and file_size > 0:
                    break
                chunks.append(chunk)
                if file_size == 0 or len(chunks) >= total:
                    break
    except OSError as e:
        ui_print(f"\nCould not read file: {e}")
        ui_print("> ", end="", flush=True)
        return

    event = threading.Event()
    with outgoing_transfers_lock:
        outgoing_transfers[offer_id] = {"acked": set(), "event": event}

    try:
        _stream_file_inner(peer_id, offer_id, filename, chunks, total, event,
                            WINDOW_SIZE, ACK_WAIT_SECONDS, MAX_RETRIES_PER_CHUNK)
    except Exception as e:
        # Catch-all so an unanticipated bug in the send loop is at least
        # visible to the user as a failed transfer, rather than the
        # exception being silently swallowed by this being a daemon
        # thread (which would otherwise leave the user watching a
        # transfer that just goes quiet forever with no explanation).
        ui_print(f"\nSending '{filename}' failed unexpectedly: {e}")
        ui_print("> ", end="", flush=True)
    finally:
        with outgoing_transfers_lock:
            outgoing_transfers.pop(offer_id, None)


def _stream_file_inner(peer_id, offer_id, filename, chunks, total, event,
                        WINDOW_SIZE, ACK_WAIT_SECONDS, MAX_RETRIES_PER_CHUNK):
    """The actual sliding-window send loop, split out from stream_file()
    purely so the outer function can wrap it in one broad exception
    handler (see stream_file's try/except Exception above) without that
    handler visually burying the loop's own logic."""
    retries = [0] * total
    next_to_send = 0
    in_flight = {}  # chunk_index -> time sent

    def send_chunk(idx):
        ok = send_routed_to_peer_id(
            peer_id, f"FILE|{peer_id}|{MY_ID}|{offer_id}|{idx}/{total}|{chunks[idx].hex()}"
        )
        in_flight[idx] = time.time()
        return ok

    while True:
        with outgoing_transfers_lock:
            acked = outgoing_transfers[offer_id]["acked"]
            acked_count = len(acked)

        if acked_count >= total:
            break

        # Top up the window with fresh chunks that haven't been sent
        # yet at all.
        while next_to_send < total and len(in_flight) < WINDOW_SIZE:
            if next_to_send not in acked:
                ok = send_chunk(next_to_send)
                if not ok:
                    ui_print(f"\nFailed to send '{filename}' (no peers connected).")
                    ui_print("> ", end="", flush=True)
                    return
            next_to_send += 1

        # Wake up either when an ack arrives, or after a short poll
        # interval, much shorter than ACK_WAIT_SECONDS itself, so we
        # check each in-flight chunk's *individual* age against the
        # timeout rather than re-evaluating the whole window in one
        # lump every ACK_WAIT_SECONDS. Without this, a window sent at
        # roughly the same moment would all "expire" together on the
        # same wakeup even under perfectly normal latency (nothing
        # actually lost), causing needless mass retransmits.
        event.wait(timeout=0.2)
        event.clear()

        with outgoing_transfers_lock:
            acked = set(outgoing_transfers[offer_id]["acked"])

        now = time.time()
        done = [idx for idx in in_flight if idx in acked]
        for idx in done:
            in_flight.pop(idx, None)

        expired = [
            idx for idx, sent_at in in_flight.items()
            if now - sent_at >= ACK_WAIT_SECONDS
        ]
        for idx in expired:
            in_flight.pop(idx, None)
            # Not acked and its individual wait expired, resend,
            # unless we've already retried it past the cap (a peer
            # that never acks a specific chunk after this many
            # attempts is treated as having failed the transfer,
            # rather than retrying forever).
            retries[idx] += 1
            if retries[idx] > MAX_RETRIES_PER_CHUNK:
                ui_print(
                    f"\nGiving up sending '{filename}': chunk {idx} was never "
                    f"acknowledged after {MAX_RETRIES_PER_CHUNK} attempts. "
                    f"The peer may have disconnected or be unreachable."
                )
                ui_print("> ", end="", flush=True)
                return
            ok = send_chunk(idx)
            if not ok:
                ui_print(f"\nFailed to send '{filename}' (no peers connected).")
                ui_print("> ", end="", flush=True)
                return

    ui_print(f"\nSent '{filename}' ({total} chunk(s)), all acknowledged.")
    ui_print("> ", end="", flush=True)


def print_help():
    ui_print("Commands:")
    ui_print("  /quit                     exit the chat")
    ui_print("  /who                      list connected peers with id, ip, and color")
    ui_print("  /connect <ip>             manually connect to a peer by IP")
    ui_print("  /recent [count]           show recent messages with their ids (default 10)")
    ui_print("  /reply <msg_id> <text>    reply to a specific message (pings its sender)")
    ui_print("  Tip: typing @name in a message pings that user")
    ui_print("  /name <newname>           change your display name (saved for next time)")
    ui_print("  /colors                   list available color numbers")
    ui_print("  /color <n>                change your color to number n")
    ui_print("  /color random             pick a random color")
    ui_print("  /showid                   toggle showing peer IDs instead of names")
    ui_print("  /clear                    clear the screen")
    ui_print("  /ping <name|id>           measure round-trip latency to a peer")
    ui_print("  /sendfile <name|id> <path> offer to send a file to a peer")
    ui_print("  /accept <n>               accept a pending file offer by number")
    ui_print("  /reject <n>               decline a pending file offer by number")
    ui_print("  /offers                   list pending incoming file offers")
    ui_print("  /msg <name|id> <text>     send a one-off private message to one peer")
    ui_print("  /dm <name|id>             enter a DM conversation; normal text goes there")
    ui_print("  /groups                   list group chats you belong to")
    ui_print("  /group <name|id>          enter a group conversation; normal text goes there")
    ui_print('  /groupcreate "name" <member...> create a fixed-membership group and enter it')
    ui_print("  /chat                     return to the public chat")
    ui_print("  /ignore <name|id>         stop showing messages from a peer")
    ui_print("  /unignore <name|id>       resume showing messages from a peer")
    ui_print("  /ignored                  list currently ignored peers")
    ui_print("  /help                     show this message")


def print_colors():
    with color_lock:
        mine = id_colors.get(MY_ID)
    ui_print(f"You have {len(COLORS)} colors to choose from (0-{len(COLORS)-1}).")
    if mine is not None:
        ui_print(f"Your current color: {COLORS[mine]}{mine}{RESET}")
    ui_print("Use /color <n> to change, or /color random for a random one.")


server = None


def start_networking():
    """Binds the TCP listen socket, prints the startup banner, replays
    recent history, picks our initial color, and launches the background
    threads. Shared by both TUI and GUI modes. Returns False (without
    raising) if the chat port is already in use, e.g. by another running
    instance of dechat on this machine, so callers can exit cleanly
    instead of crashing on a raw traceback."""
    global server

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("", CHAT_PORT))
    except OSError:
        ui_print(
            f"Could not start dechat: port {CHAT_PORT} is already in use.\n"
            f"Another instance of dechat may already be running on this machine."
        )
        try:
            server.close()
        except OSError:
            pass
        return False
    server.listen()

    ui_print(r"  _____        _____ _           _   ")
    ui_print(r" |  __ \      / ____| |         | |  ")
    ui_print(r" | |  | | ___| |    | |__   __ _| |_ ")
    ui_print(r" | |  | |/ _ \ |    | '_ \ / _` | __|")
    ui_print(r" | |__| |  __/ |____| | | | (_| | |_ ")
    ui_print(r" |_____/ \___|\_____|_| |_|\__,_|__|")
    ui_print(" ")
    ui_print(f"Welcome to DeChat version {CURRENT_VERSION}!")
    ui_print("Your ID:", MY_ID)
    ui_print("Quick start: /who (list peers)  /name <n>  /msg <n> <text>  /sendfile <n> <path>  /help (full list)")
    ui_print("Looking for other users...")

    show_recent_history()

    with name_color_lock:
        my_preferred = name_to_color.get(MY_NAME)
    if my_preferred is not None:
        set_color_for_id(MY_ID, my_preferred)
    else:
        color_for_id(MY_ID)

    threading.Thread(target=accept_connections, args=(server,), daemon=True).start()
    threading.Thread(target=discovery, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    return True


def shutdown_networking():
    global running
    running = False

    if discovery_socket:
        try:
            discovery_socket.close()
        except OSError:
            pass

    with lock:
        for peer in peers.values():
            try:
                peer.shutdown(socket.SHUT_RDWR)
                peer.close()
            except OSError:
                pass

    if server:
        try:
            server.close()
        except OSError:
            pass

    ui_print("\nGoodbye!")


def _cmd_color(arg):
    with color_lock:
        current = id_colors.get(MY_ID)

    if arg == "random":
        choices = [i for i in range(len(COLORS)) if i != current]
        chosen = choices[uuid.uuid4().int % len(choices)] if choices else 0
        claim_color_for_id(MY_ID, chosen)
        remember_color_for_name(MY_NAME, chosen)
        send_to_all(wrap_relay(f"COLOR|{MY_ID}|{chosen}"))
        ui_print(f"Color set to {COLORS[chosen]}{chosen}{RESET}")
    elif arg.isdigit():
        chosen = int(arg)
        if chosen < 0 or chosen >= len(COLORS):
            ui_print(f"Pick a number from 0 to {len(COLORS)-1}.")
        else:
            claim_color_for_id(MY_ID, chosen)
            remember_color_for_name(MY_NAME, chosen)
            send_to_all(wrap_relay(f"COLOR|{MY_ID}|{chosen}"))
            ui_print(f"Color set to {COLORS[chosen]}{chosen}{RESET}")
    else:
        ui_print("Usage: /color <n> or /color random")


def _cmd_showid():
    global SHOW_ID_INSTEAD_OF_NAME
    if UI_MODE == "gui":
        ui_print("Use the settings panel to toggle showing IDs instead of names.")
    else:
        SHOW_ID_INSTEAD_OF_NAME = not SHOW_ID_INSTEAD_OF_NAME
        state = "on" if SHOW_ID_INSTEAD_OF_NAME else "off"
        ui_print(f"Show IDs instead of names: {state}")


def _cmd_name(raw_name):
    global MY_NAME
    MY_NAME = raw_name.strip() or MY_NAME
    save_name(MY_NAME)
    send_to_all(wrap_relay(f"NAME|{MY_ID}|{escape_field(MY_NAME)}"))
    ui_print(f"Name set to: {MY_NAME}")


def _cmd_ping(target):
    if target:
        send_ping(target)
    else:
        ui_print("Usage: /ping <name|id>")


def _cmd_sendfile(rest):
    args = rest.split(" ", 1)
    if len(args) == 2:
        send_file(args[0], args[1])
    else:
        ui_print("Usage: /sendfile <name|id> <path>")


def _cmd_msg(rest):
    args = rest.split(" ", 1)
    if len(args) == 2:
        send_private_message(args[0], args[1])
    else:
        ui_print("Usage: /msg <name|id> <text>")


def _cmd_ignore(target):
    if target:
        ignore_peer(target)
    else:
        ui_print("Usage: /ignore <name|id>")


def _cmd_unignore(target):
    if target:
        unignore_peer(target)
    else:
        ui_print("Usage: /unignore <name|id>")


def _cmd_accept(ref):
    if ref:
        accept_offer(ref)
    else:
        ui_print("Usage: /accept <n>")


def _cmd_reject(ref):
    if ref:
        reject_offer(ref)
    else:
        ui_print("Usage: /reject <n>")


def _cmd_connect(target_ip):
    if target_ip:
        ui_print(f"Connecting to {target_ip}...")
        threading.Thread(target=connect_to_peer, args=(target_ip,), daemon=True).start()
    else:
        ui_print("Usage: /connect <ip>")


def _cmd_recent(arg):
    if arg:
        try:
            count = max(1, min(int(arg), MAX_MSG_HISTORY))
        except ValueError:
            ui_print("Usage: /recent [count]")
            count = None
    else:
        count = 10
    if count:
        list_recent_messages(count)


def _cmd_reply(rest):
    args = rest.split(" ", 1)
    if len(args) == 2 and args[0]:
        reply_id, text = args
        reply_id = reply_id.lstrip("[").rstrip("]")
        target = get_remembered_message(reply_id)
        if not target:
            ui_print(f"No known message with id '{reply_id}'. It may be too old or invalid.")
        else:
            send_chat_message(text, reply_id=reply_id)
    else:
        ui_print("Usage: /reply <msg_id> <text>")


def process_command(message):
    """Handles one line of input exactly as the original interactive loop
    did. Returns False if the app should quit, True otherwise. Shared by
    both the TUI's input() loop and the GUI's entry-box handler.

    This is purely the dispatch: which command matched, and what argument
    slice to hand its helper. Each multi-line command body lives in its
    own _cmd_*() function above, unchanged from the original -- the
    matching order and conditions here are untouched."""
    if message == "/quit":
        return False

    elif message == "/who":
        list_peers()

    elif message == "/help":
        print_help()

    elif message == "/colors":
        print_colors()

    elif message == "/color" or message.startswith("/color "):
        _cmd_color(message[len("/color"):].strip())

    elif message == "/showid":
        _cmd_showid()

    elif message.startswith("/name "):
        _cmd_name(message[6:])

    elif message == "/clear":
        if UI_MODE != "gui":
            print("\033[2J\033[H", end="")

    elif message.startswith("/ping "):
        _cmd_ping(message[len("/ping "):].strip())

    elif message.startswith("/sendfile "):
        _cmd_sendfile(message[len("/sendfile "):].strip())

    elif message.startswith("/msg "):
        _cmd_msg(message[len("/msg "):].strip())

    elif message.startswith("/ignore "):
        _cmd_ignore(message[len("/ignore "):].strip())

    elif message.startswith("/unignore "):
        _cmd_unignore(message[len("/unignore "):].strip())

    elif message == "/ignored":
        list_ignored()

    elif message == "/offers":
        list_pending_offers()

    elif message.startswith("/accept "):
        _cmd_accept(message[len("/accept "):].strip())

    elif message.startswith("/reject "):
        _cmd_reject(message[len("/reject "):].strip())

    elif message.startswith("/connect "):
        _cmd_connect(message[len("/connect "):].strip())

    elif message == "/recent" or message.startswith("/recent "):
        _cmd_recent(message[len("/recent"):].strip())

    elif message.startswith("/reply "):
        _cmd_reply(message[len("/reply "):].strip())

    elif message.startswith("/"):
        ui_print(f"Unknown command: {message}. Type /help for a list of commands.")

    elif message:
        send_chat_message(message)

    return True


def send_chat_message(message, reply_id=""):
    my_color = color_index_for_id(MY_ID)
    msg_id = new_msg_id()
    remember_message(msg_id, MY_NAME, message)

    reply_tag = ""
    if reply_id:
        replied = get_remembered_message(reply_id)
        if replied:
            reply_tag = f" (reply to {replied[0]} [{reply_id}])"

    id_tag = f"[{msg_id}]" if msg_id else ""
    display_line = f"{id_tag}{reply_tag} {COLORS[my_color]}{MY_NAME}{RESET}: {message}"

    if UI_MODE == "gui":
        ui_print(f"{timestamp()} {display_line}")
    else:
        print(f"\033[1A\033[2K{timestamp()} {display_line}")
    log_message(f"{timestamp()} [{msg_id}]{reply_tag} {MY_NAME}: {message}")
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        ui_print("(No peers connected yet - message saved locally but not delivered to anyone.)")
    inner = f"MSG|{MY_ID}|{my_color}|{escape_field(MY_NAME)}|{escape_field(message)}|{escape_field(msg_id)}|{escape_field(reply_id)}"
    send_to_all(wrap_relay(inner))


# TUI frontend (plain terminal)
def print_tui_update_lock():
    """The TUI's version-lock screen. Deliberately bare-bones: unlike the
    GUI's side panel, the TUI never shows recentNews/news_details, only the
    fact that an update is required."""
    print("=" * 60)
    print("Different versions have different chat protocols.")
    print("Your version will not work unless you update")
    print("=" * 60)
    print("Please update to the latest version.")


def print_tui_outdated_notice():
    """A dismissable heads-up shown when a newer app version exists but our
    protocol version still matches, so chatting is safe -- unlike
    print_tui_update_lock(), this never blocks the user from continuing."""
    print("=" * 60)
    print("You are not on the latest version of dechat.")
    print("The app will still work, but you may want to update when you can.")
    print("=" * 60)
    input("Press Enter to continue... ")


def run_tui():
    import shlex

    global UI_MODE
    UI_MODE = "tui"

    status = fetch_update_status()
    if status is not None:
        protocol = status.get("protocolVersion")
        if protocol is not None and protocol != PROTOCOL_VERSION:
            print_tui_update_lock()
            return

        latest = status.get("latestVersion")
        if latest is not None and latest != CURRENT_VERSION:
            print_tui_outdated_notice()

    if not start_networking():
        return

    ui_print('TUI conversations: /dm <name|id>, /groups, /group <name|id>, /groupcreate "name" <member...>, /chat')

    # TUI-only conversation state.  The GUI has real tabs and never sees or
    # handles the commands below; keeping this state local to run_tui() prevents
    # TUI navigation commands from leaking into the shared command processor.
    conversation = {"mode": "chat", "target": None}

    def resolve_tui_group(target):
        target = target.strip()
        if not target:
            return None
        with group_lock:
            snapshot = {
                gid: {"name": group["name"], "members": set(group["members"])}
                for gid, group in group_chats.items()
                if MY_ID in group.get("members", set())
            }
        if target in snapshot:
            return target
        folded = target.casefold()
        matches = [gid for gid, group in snapshot.items() if group["name"].casefold() == folded]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ui_print(
                f"More than one group is named '{target}'. Use the group id from /groups."
            )
        else:
            ui_print(f"No group named or identified as '{target}'. Use /groups to list groups.")
        return None

    def show_tui_groups():
        with group_lock:
            snapshot = [
                (gid, group["name"], sorted(group["members"]))
                for gid, group in group_chats.items()
                if MY_ID in group.get("members", set())
            ]
        if not snapshot:
            ui_print("You are not in any group chats.")
            return
        with names_lock:
            names = dict(peer_names)
        ui_print("Groups:")
        for gid, name, members in sorted(snapshot, key=lambda item: item[1].casefold()):
            shown_members = [MY_NAME if pid == MY_ID else names.get(pid, pid) for pid in members]
            ui_print(f"  {name} [{gid}]  members: {', '.join(shown_members)}")

    def conversation_prompt():
        mode = conversation["mode"]
        target = conversation["target"]
        if mode == "dm" and target:
            with names_lock:
                name = peer_names.get(target, target)
            return f"DM @{name}> "
        if mode == "group" and target:
            with group_lock:
                group = group_chats.get(target)
                name = group["name"] if group else target
            return f"#{name}> "
        return "> "

    def process_tui_input(message):
        # TUI-only navigation commands.  These deliberately do not exist in
        # process_command(), so entering them in the GUI cannot activate them.
        if message == "/chat":
            conversation["mode"] = "chat"
            conversation["target"] = None
            ui_print("Switched to public chat.")
            return True

        if message == "/dm" or message.startswith("/dm "):
            target = message[len("/dm"):].strip()
            if not target:
                ui_print("Usage: /dm <name|id>")
                return True
            peer_id = resolve_peer_id(target)
            if peer_id:
                conversation["mode"] = "dm"
                conversation["target"] = peer_id
                with names_lock:
                    name = peer_names.get(peer_id, target)
                ui_print(f"DM with {name}. Type /chat to return to public chat.")
            return True

        if message == "/groups":
            show_tui_groups()
            return True

        if message == "/group" or message.startswith("/group "):
            target = message[len("/group"):].strip()
            if not target:
                ui_print("Usage: /group <name|group_id>")
                return True
            group_id = resolve_tui_group(target)
            if group_id:
                conversation["mode"] = "group"
                conversation["target"] = group_id
                with group_lock:
                    group_name = group_chats[group_id]["name"]
                ui_print(f"Group chat '{group_name}'. Type /chat to return to public chat.")
            return True

        if message == "/groupcreate" or message.startswith("/groupcreate "):
            rest = message[len("/groupcreate"):].strip()
            try:
                args = shlex.split(rest)
            except ValueError as exc:
                ui_print(f"Could not parse group command: {exc}")
                ui_print('Usage: /groupcreate "group name" <member1> [member2 ...]')
                return True
            if len(args) < 2:
                ui_print('Usage: /groupcreate "group name" <member1> [member2 ...]')
                return True
            group_name, raw_members = args[0], args[1:]
            member_ids = []
            for raw_member in raw_members:
                peer_id = resolve_peer_id(raw_member)
                if not peer_id:
                    return True
                if peer_id != MY_ID and peer_id not in member_ids:
                    member_ids.append(peer_id)
            if not member_ids:
                ui_print("Select at least one other member.")
                return True
            group_id = create_group_chat(group_name, member_ids)
            if group_id:
                conversation["mode"] = "group"
                conversation["target"] = group_id
                ui_print(f"Created group '{group_name}' [{group_id}] and switched to it.")
            return True

        # Existing slash commands remain one-shot commands in every TUI
        # conversation.  /quit's False return is propagated to the outer loop.
        if message.startswith("/"):
            return process_command(message)

        if not message:
            return True

        if conversation["mode"] == "dm":
            peer_id = conversation["target"]
            if peer_id:
                send_private_message(peer_id, message)
            return True

        if conversation["mode"] == "group":
            group_id = conversation["target"]
            if group_id:
                if not send_group_message(group_id, message):
                    # If the group definition disappeared, fall back to public
                    # chat rather than silently routing later text somewhere else.
                    with group_lock:
                        still_exists = group_id in group_chats
                    if not still_exists:
                        conversation["mode"] = "chat"
                        conversation["target"] = None
            return True

        send_chat_message(message)
        return True

    while True:
        try:
            try:
                message = input(conversation_prompt())
            except EOFError:
                message = "/quit"

            if not process_tui_input(message):
                break

        except KeyboardInterrupt:
            break

    shutdown_networking()

# GUI frontend (tkinter, styled after bitchat's terminal-green look)

def show_gui_update_lock(BG, PANEL_BG, FG, DIM_FG, ACCENT, FONT_FAMILY):
    """Standalone 'please update' window shown instead of the chat UI when
    our PROTOCOL_VERSION doesn't match the API's protocolVersion. Networking
    is never started in this path, so a peer running an incompatible
    protocol version can't accidentally get onto the network."""
    import tkinter as tk

    lock_root = tk.Tk()
    lock_root.title("dechat - update required")
    lock_root.configure(bg=BG)
    lock_root.geometry("520x360")
    lock_root.minsize(420, 300)

    LOCK_TITLE_FONT = (FONT_FAMILY, 20, "bold")
    LOCK_SUB_FONT = (FONT_FAMILY, 13, "bold")
    LOCK_BODY_FONT = (FONT_FAMILY, 12)

    wrap = tk.Frame(lock_root, bg=BG)
    wrap.pack(fill="both", expand=True, padx=28, pady=28)

    tk.Label(
        wrap, text="Please update to the latest version",
        fg=ACCENT, bg=BG, font=LOCK_TITLE_FONT, wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 14))

    tk.Label(
        wrap,
        text=(
            "Different versions have different chat protocols. "
            "Your version will not work unless you update"
        ),
        fg=FG, bg=BG, font=LOCK_SUB_FONT, wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 18))

    tk.Label(
        wrap,
        text=f"Your version: {CURRENT_VERSION}",
        fg=DIM_FG, bg=BG, font=LOCK_BODY_FONT, justify="left",
    ).pack(anchor="w")

    tk.Button(
        wrap, text="Quit", command=lock_root.destroy,
        bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
        font=LOCK_SUB_FONT, relief="flat", padx=16, pady=6,
    ).pack(anchor="w", pady=(24, 0))

    lock_root.mainloop()




# GUI theme palettes. Static presentation data with no dependency on any
# other run_gui() state, kept at module level so it doesn't clutter the
# top of that function; run_gui() aliases it locally as THEMES.
GUI_THEMES = {
    "classic": {
        "bg": "#0a0e0a",
        "panel": "#0f140f",
        "fg": "#33ff66",
        "dim": "#1f8f3a",
        "accent": "#33ff66",
        "error": "#ff5f5f",
        "ping_bg": "#5a4a00",
    },
    "light": {
        "bg": "#f5fbf5",
        "panel": "#dbe8db",
        "fg": "#000000",
        "dim": "#1a752f",
        "accent": "#000000",
        "error": "#D81F1F",
        "ping_bg": "#ffdc3f",
    },
    "solar-light": {
        "bg": "#fdf6e3",
        "panel": "#eee8d5",
        "fg": "#000000",
        "dim": "#8b7e44",
        "accent": "#000000",
        "error": "#D81F1F",
        "ping_bg": "#ffdc3f",
    },
    "sky": {
        "bg": "#ebfbff",
        "panel": "#bef3ff",
        "fg": "#000000",
        "dim": "#006076",
        "accent": "#000000",
        "error": "#D81F1F",
        "ping_bg": "#45ddff",
    },
    "discord": {
        "bg": "#323339",
        "panel": "#5764ef",
        "fg": "#FFFFFF",
        "dim": "#cdd7ff",
        "accent": "#FFFFFF",
        "error": "#D81F1F",
        "ping_bg": "#5764ef",
    },
    "solar-dark": {
        "bg": "#002b36",
        "panel": "#00212b",
        "fg": "#FFFFFF",
        "dim": "#c0e4e3",
        "accent": "#FFFFFF",
        "error": "#B92C2C",
        "ping_bg": "#1e817e",
    },
    "pink": {
        "bg": "#13151a",
        "panel": "#1c1e24",
        "fg": "#e2e8f0",
        "dim": "#64748b",
        "accent": "#f472b6",
        "error": "#f87171",
        "ping_bg": "#5a3047"
    },
    "chocolate": {
        "bg": "#431F0A",
        "panel": "#794020",
        "fg": "#EFEFEF",
        "dim": "#ffffe4",
        "accent": "#EFEFEF",
        "error": "#D81F1F",
        "ping_bg": "#925E40",
    },
    "catppuccin-latte": {
        "bg": "#eff1f5",
        "panel": "#e6e9ef",
        "fg": "#4c4f69",
        "dim": "#8c8fa1",
        "accent": "#8839ef",
        "error": "#d20f39",
        "ping_bg": "#ddd5f5",
    },
    "catppuccin-frappe": {
        "bg": "#303446",
        "panel": "#292c3c",
        "fg": "#c6d0f5",
        "dim": "#838ba7",
        "accent": "#ca9ee6",
        "error": "#e78284",
        "ping_bg": "#4b4059",
    },
}


def run_gui():
    import tkinter as tk
    from tkinter import scrolledtext, filedialog
    import tkinter.font as tkfont
    import re as _re

    global UI_MODE
    UI_MODE = "gui"

    # GUI themes are intentionally presentation-only data (see GUI_THEMES
    # above). Theme switching is a presentation concern, so the shared
    # networking/command core does not need any global theme state or
    # references to Tk widgets.
    THEMES = GUI_THEMES
    current_theme = "classic"
    _initial_theme = THEMES[current_theme]
    BG = _initial_theme["bg"]
    PANEL_BG = _initial_theme["panel"]
    FG = _initial_theme["fg"]
    DIM_FG = _initial_theme["dim"]
    ACCENT = _initial_theme["accent"]

    # Pick a monospace font up front (before any window exists) so both the
    # lock screen and the main chat UI use the same resolved family.
    _probe = tk.Tk()
    _probe.withdraw()
    _mono_candidates = [
        "Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "Cascadia Mono",
        "Courier New", "Courier",
    ]
    _available = set(tkfont.families(_probe))
    FONT_FAMILY = next((f for f in _mono_candidates if f in _available), "Courier")
    _probe.destroy()

    # Check in with the update API before building the chat UI at all. A
    # confirmed protocol version mismatch locks the user onto an update
    # screen and never starts networking. A plain app-version mismatch
    # (latestVersion) is not protocol-breaking, so that's just a dismissable
    # notice shown once the chat UI is up. If the check itself fails
    # (offline, host unreachable, bad response) we fail open and let chat
    # proceed -- being unable to reach the news API isn't the same as being
    # on an incompatible protocol version.
    update_status = fetch_update_status()
    protocol_version = update_status.get("protocolVersion") if update_status else None
    if protocol_version is not None and protocol_version != PROTOCOL_VERSION:
        show_gui_update_lock(BG, PANEL_BG, FG, DIM_FG, ACCENT, FONT_FAMILY)
        return

    latest_version = update_status.get("latestVersion") if update_status else None
    is_outdated = latest_version is not None and latest_version != CURRENT_VERSION

    root = tk.Tk()
    root.title(f"dechat* @{MY_NAME}")
    root.configure(bg=BG)
    root.geometry("640x760")
    root.minsize(480, 420)

    # GUI-only user preferences. These stay local to run_gui(); networking and
    # protocol code do not depend on them.
    gui_settings = {
        "sound_on_ping": True,
        "show_timestamps": True,
        "show_presence": True,
        "show_id": False,
    }

    # FONT_FAMILY was already resolved (against a throwaway root) before the
    # update-version check above, so the lock screen and the chat UI below
    # use the same monospace family without probing twice.
    FONT = (FONT_FAMILY, 12)
    MONO_BOLD = (FONT_FAMILY, 12, "bold")
    TITLE_FONT = (FONT_FAMILY, 18, "bold")
    NAME_FONT = (FONT_FAMILY, 18)
    SMALL_BOLD = (FONT_FAMILY, 14, "bold")
    SMALL_FONT = (FONT_FAMILY, 12)

    #header bar, styled like bitchat
    header = tk.Frame(root, bg=BG)
    header.pack(side="top", fill="x", padx=14, pady=(12, 6))

    title_label = tk.Label(
        header, text="dechat", fg=ACCENT, bg=BG, font=TITLE_FONT
    )
    title_label.pack(side="left")

    name_label = tk.Label(
        header, text=f" @{MY_NAME}", fg=FG, bg=BG, font=NAME_FONT
    )
    name_label.pack(side="left")

    peer_count_var = tk.StringVar(value="0 peers")
    people_btn = tk.Button(
        header, textvariable=peer_count_var, bg=BG, fg=FG,
        activebackground=BG, activeforeground=ACCENT, font=SMALL_BOLD,
        relief="flat", padx=6, bd=0, command=lambda: show_people_window(),
    )
    people_btn.pack(side="right")

    news_toggle_btn = tk.Button(
        header, text="news", bg=BG, fg=DIM_FG, activebackground=BG, activeforeground=ACCENT,
        font=SMALL_FONT, relief="flat", padx=6, bd=0,
        command=lambda: toggle_news_panel(),
    )
    news_toggle_btn.pack(side="right", padx=(0, 8))

    settings_toggle_btn = tk.Button(
        header, text="settings", bg=BG, fg=DIM_FG, activebackground=BG, activeforeground=ACCENT,
        font=SMALL_FONT, relief="flat", padx=6, bd=0,
    )
    settings_toggle_btn.pack(side="right", padx=(0, 14))

    divider = tk.Frame(root, bg=DIM_FG, height=1)
    divider.pack(side="top", fill="x", padx=0)

    # --- dismissable "not on latest version" notice ---
    # Only shown when latestVersion != CURRENT_VERSION but the protocol
    # version still matches (a real mismatch never reaches this point --
    # see the protocol_version check above, which shows show_gui_update_lock
    # and returns before the chat UI is built at all). Purely informational:
    # closing it just hides the bar, nothing else changes.
    if is_outdated:
        outdated_bar = tk.Frame(root, bg=PANEL_BG)
        outdated_bar.pack(side="top", fill="x")

        outdated_inner = tk.Frame(outdated_bar, bg=PANEL_BG)
        outdated_inner.pack(fill="x", padx=14, pady=8)

        tk.Label(
            outdated_inner,
            text="You're not on the latest version of dechat. The app will still work as normal.",
            fg=FG, bg=PANEL_BG, font=SMALL_FONT, anchor="w", justify="left",
        ).pack(side="left", fill="x", expand=True)

        tk.Button(
            outdated_inner, text="Dismiss", command=lambda: outdated_bar.pack_forget(),
            bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
            font=SMALL_FONT, relief="flat", padx=10, pady=2,
        ).pack(side="right", padx=(10, 0))

    # --- input bar, styled like bitchat ---
    # Packed here, early, with side="bottom" so it is pinned to the bottom
    # edge of the window and always keeps its natural size. Only the
    # expandable chat log (packed last, below) shrinks to make room for
    # the help panel — the input bar itself can never be squeezed out.
    input_bar = tk.Frame(root, bg=BG)
    input_bar.pack(side="bottom", fill="x", padx=10, pady=(0, 12))

    # --- command helper panel, styled like bitchat's command list ---
    # Also bottom-anchored (directly above the input bar) for the same
    # reason: so showing it can only shrink the chat log, never hide input.
    help_visible = tk.BooleanVar(value=False)
    help_frame = tk.Frame(root, bg=PANEL_BG, highlightbackground=DIM_FG, highlightthickness=1)

    # --- collapsible news panel ---
    # Shows recentNews/news_details from the same update_status API response
    # fetched at the top of run_gui() (before this window even existed).
    # Packed side="right" *before* chat_frame below, so it claims a fixed
    # slice of the window and the chat log (which fills/expands) shrinks to
    # make room for it, the same pattern used for the bottom help panel.
    NEWS_PANEL_WIDTH = 240
    news_visible = tk.BooleanVar(value=False)
    news_frame = tk.Frame(
        root, bg=PANEL_BG, width=NEWS_PANEL_WIDTH,
        highlightbackground=DIM_FG, highlightthickness=1,
    )
    news_frame.pack_propagate(False)

    news_inner = tk.Frame(news_frame, bg=PANEL_BG)
    news_inner.pack(fill="both", expand=True, padx=14, pady=14)

    news_heading_label = tk.Label(
        news_inner, text="WHAT'S NEW", fg=DIM_FG, bg=PANEL_BG,
        font=SMALL_FONT, anchor="w",
    )
    news_heading_label.pack(fill="x", pady=(0, 8))

    recent_news = (update_status or {}).get("recentNews") or "No news right now."
    news_details = (update_status or {}).get("news_details") or ""

    recent_news_label = tk.Label(
        news_inner, text=recent_news, fg=ACCENT, bg=PANEL_BG,
        font=MONO_BOLD, anchor="w", justify="left", wraplength=NEWS_PANEL_WIDTH - 28,
    )
    recent_news_label.pack(fill="x", pady=(0, 8))

    news_details_label = None
    if news_details:
        news_details_label = tk.Label(
            news_inner, text=news_details, fg=FG, bg=PANEL_BG,
            font=SMALL_FONT, anchor="w", justify="left", wraplength=NEWS_PANEL_WIDTH - 28,
        )
        news_details_label.pack(fill="x")

    # --- settings panel ---
    SETTINGS_PANEL_WIDTH = 270
    settings_visible = tk.BooleanVar(value=False)
    settings_frame = tk.Frame(
        root, bg=PANEL_BG, width=SETTINGS_PANEL_WIDTH,
        highlightbackground=DIM_FG, highlightthickness=1,
    )
    settings_frame.pack_propagate(False)
    settings_inner = tk.Frame(settings_frame, bg=PANEL_BG)
    settings_inner.pack(fill="both", expand=True, padx=14, pady=14)

    settings_title_label = tk.Label(
        settings_inner, text="SETTINGS", fg=ACCENT, bg=PANEL_BG,
        font=MONO_BOLD, anchor="w",
    )
    settings_title_label.pack(fill="x", pady=(0, 14))

    settings_labels = []
    settings_controls = []

    def setting_label(text):
        label = tk.Label(settings_inner, text=text, fg=DIM_FG, bg=PANEL_BG, font=SMALL_FONT, anchor="w")
        label.pack(fill="x", pady=(8, 3))
        settings_labels.append(label)
        return label

    setting_label("Theme")
    theme_var = tk.StringVar(value=current_theme)
    theme_menu = tk.OptionMenu(settings_inner, theme_var, *THEMES.keys())
    theme_menu.configure(
        bg=PANEL_BG, fg=FG, activebackground=DIM_FG, activeforeground=BG,
        highlightbackground=DIM_FG, font=SMALL_FONT, relief="flat", bd=0,
    )
    theme_menu["menu"].configure(bg=PANEL_BG, fg=FG, font=SMALL_FONT)
    theme_menu.pack(fill="x")
    settings_controls.append(theme_menu)

    setting_label("Chat colour")
    color_index_for_id(MY_ID)
    with color_lock:
        _starting_color = id_colors.get(MY_ID, 0)
    color_var = tk.StringVar(value=str(_starting_color))
    color_menu = tk.OptionMenu(settings_inner, color_var, *[str(i) for i in range(len(COLORS))])
    color_menu.configure(
        bg=PANEL_BG, fg=FG, activebackground=DIM_FG, activeforeground=BG,
        highlightbackground=DIM_FG, font=SMALL_FONT, relief="flat", bd=0,
    )
    color_menu["menu"].configure(bg=PANEL_BG, fg=FG, font=SMALL_FONT)
    color_menu.pack(fill="x")
    settings_controls.append(color_menu)

    setting_label("Display name")
    name_var = tk.StringVar(value=MY_NAME)
    name_entry = tk.Entry(
        settings_inner, textvariable=name_var, bg=BG, fg=FG, insertbackground=FG,
        font=SMALL_FONT, relief="flat", highlightthickness=1,
        highlightbackground=DIM_FG, highlightcolor=ACCENT,
    )
    name_entry.pack(fill="x", ipady=5)
    settings_controls.append(name_entry)

    sound_var = tk.BooleanVar(value=gui_settings["sound_on_ping"])
    timestamps_var = tk.BooleanVar(value=gui_settings["show_timestamps"])
    presence_var = tk.BooleanVar(value=gui_settings["show_presence"])
    show_id_var = tk.BooleanVar(value=gui_settings["show_id"])

    for text, var in (
        ("Ping Sound Effect?", sound_var),
        ("Timestamps?", timestamps_var),
        ("Connection Notices?", presence_var),
        ("Show IDs Instead of Names?", show_id_var),
    ):
        cb = tk.Checkbutton(
            settings_inner, text=text, variable=var, bg=PANEL_BG, fg=FG,
            activebackground=PANEL_BG, activeforeground=FG, selectcolor=PANEL_BG,
            font=SMALL_FONT, anchor="w",
        )
        cb.pack(fill="x", pady=(8, 0))
        settings_controls.append(cb)

    settings_status = tk.StringVar(value="")
    settings_status_label = tk.Label(
        settings_inner, textvariable=settings_status, fg=DIM_FG, bg=PANEL_BG,
        font=SMALL_FONT, anchor="w", justify="left", wraplength=SETTINGS_PANEL_WIDTH - 28,
    )
    settings_status_label.pack(fill="x", pady=(10, 0))

    def toggle_news_panel():
        if news_visible.get():
            news_frame.pack_forget()
            news_visible.set(False)
            news_toggle_btn.config(fg=THEMES[current_theme]["dim"])
        else:
            if settings_visible.get():
                settings_frame.pack_forget()
                settings_visible.set(False)
                settings_toggle_btn.config(fg=THEMES[current_theme]["dim"])
            # before=chat_frame: chat_frame already claimed all remaining
            # space with expand=True/fill="both" when it was packed below.
            # Packing news_frame at the "right" side with side="right" alone
            # (after chat_frame already exists) would only get the sliver
            # chat_frame hasn't claimed yet, pack() resolves widths in
            # packing order, and chat_frame comes first and takes
            # everything. Inserting news_frame *before* chat_frame in the
            # packing list makes it claim its slice first, so chat_frame
            # shrinks to fit what's left, instead of the other way around.
            # This mirrors how help_frame uses before=input_bar below.
            news_frame.pack(side="right", fill="y", padx=(0, 10), pady=8, before=chat_frame)
            news_visible.set(True)
            news_toggle_btn.config(fg=THEMES[current_theme]["accent"])

    # --- conversation area ---
    chat_frame = tk.Frame(root, bg=BG)
    chat_frame.pack(side="top", fill="both", expand=True, padx=10, pady=8)

    active_tab = tk.StringVar(value="chat")
    tab_bar = tk.Frame(chat_frame, bg=BG)
    tab_bar.pack(side="top", fill="x", pady=(0, 6))
    tab_buttons = {}
    for _key, _label in (("chat", "Chat"), ("dms", "DMs"), ("groups", "Groups")):
        _btn = tk.Button(
            tab_bar, text=_label, bg=BG, fg=DIM_FG, activebackground=BG,
            activeforeground=ACCENT, font=SMALL_FONT, relief="flat", bd=0, padx=8,
            command=lambda k=_key: switch_conversation_tab(k),
        )
        _btn.pack(side="left", padx=(0, 4))
        tab_buttons[_key] = _btn

    # --- scrolling public chat log ---
    chat_log = scrolledtext.ScrolledText(
        chat_frame,
        bg=BG,
        fg=FG,
        insertbackground=FG,
        font=FONT,
        wrap="word",
        borderwidth=0,
        highlightthickness=0,
        state="disabled",
    )
    chat_log.pack(fill="both", expand=True)
    chat_log.tag_configure("dim", foreground=DIM_FG)
    chat_log.tag_configure("system", foreground=DIM_FG, font=FONT + ("italic",))
    chat_log.tag_configure("error", foreground=_initial_theme["error"])
    chat_log.tag_configure("me", font=MONO_BOLD)
    chat_log.tag_configure("pinged", background=_initial_theme["ping_bg"], font=MONO_BOLD)

    # Every peer name/message the core prints is wrapped in one of the ANSI
    # SGR codes from COLORS (see near the top of this file) followed by
    # RESET ("\033[0m"). The TUI just dumps these straight to a real
    # terminal, which interprets them. Tkinter's Text widget has no idea
    # what to do with raw escape bytes though, so without translation they
    # either show up as garbage or (since classify_and_append strips them)
    # get silently discarded — which is why only "me" ever got a color: that
    # branch had its own hardcoded tag, but everyone else's ANSI color was
    # just thrown away along with the escape codes. This map gives each of
    # the 12 ANSI codes the core can emit a matching Tk-renderable hex color
    # and a tag name, so peer names/messages keep their assigned color.
    ANSI_TO_TAG = {
        "\033[31m": "ansi31", "\033[32m": "ansi32", "\033[33m": "ansi33",
        "\033[34m": "ansi34", "\033[35m": "ansi35", "\033[36m": "ansi36",
        "\033[91m": "ansi91", "\033[92m": "ansi92", "\033[93m": "ansi93",
        "\033[94m": "ansi94", "\033[95m": "ansi95", "\033[96m": "ansi96",
    }
    ANSI_TAG_COLORS = {
        "ansi31": "#ff6b6b", "ansi32": "#4fd671", "ansi33": "#e5c04b",
        "ansi34": "#5da9ff", "ansi35": "#d878e0", "ansi36": "#4fd6d6",
        "ansi91": "#ff8f8f", "ansi92": "#7dffa0", "ansi93": "#ffe066",
        "ansi94": "#8fc2ff", "ansi95": "#eaa3f2", "ansi96": "#7dffff",
    }
    for _tag, _color in ANSI_TAG_COLORS.items():
        chat_log.tag_configure(_tag, foreground=_color)

    # --- DM and group panes ---
    dm_frame = tk.Frame(chat_frame, bg=BG)
    dm_sidebar = tk.Frame(dm_frame, bg=PANEL_BG, width=190)
    dm_sidebar.pack(side="left", fill="y", padx=(0, 8))
    dm_sidebar.pack_propagate(False)

    dm_online_label = tk.Label(
        dm_sidebar, text="ONLINE", bg=PANEL_BG, fg=DIM_FG,
        font=SMALL_BOLD, anchor="w",
    )
    dm_online_label.pack(fill="x", padx=8, pady=(7, 2))
    online_dm_list = tk.Listbox(
        dm_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False, height=7,
    )
    online_dm_list.pack(fill="x", padx=6, pady=(0, 6))

    dm_recent_label = tk.Label(
        dm_sidebar, text="RECENT DMS", bg=PANEL_BG, fg=DIM_FG,
        font=SMALL_BOLD, anchor="w",
    )
    dm_recent_label.pack(fill="x", padx=8, pady=(2, 2))
    dm_list = tk.Listbox(
        dm_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False,
    )
    dm_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))
    dm_log = scrolledtext.ScrolledText(
        dm_frame, bg=BG, fg=FG, insertbackground=FG, font=FONT, wrap="word",
        borderwidth=0, highlightthickness=0, state="disabled",
    )
    dm_log.pack(side="left", fill="both", expand=True)

    group_frame = tk.Frame(chat_frame, bg=BG)
    group_sidebar = tk.Frame(group_frame, bg=PANEL_BG, width=170)
    group_sidebar.pack(side="left", fill="y", padx=(0, 8))
    group_sidebar.pack_propagate(False)
    new_group_btn = tk.Button(
        group_sidebar, text="+ New group", bg=PANEL_BG, fg=ACCENT,
        activebackground=DIM_FG, activeforeground=BG, font=SMALL_FONT,
        relief="flat", command=lambda: open_new_group_dialog(),
    )
    new_group_btn.pack(fill="x", padx=6, pady=(6, 2))
    group_list = tk.Listbox(
        group_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False,
    )
    group_list.pack(fill="both", expand=True, padx=6, pady=(2, 6))
    group_log = scrolledtext.ScrolledText(
        group_frame, bg=BG, fg=FG, insertbackground=FG, font=FONT, wrap="word",
        borderwidth=0, highlightthickness=0, state="disabled",
    )
    group_log.pack(side="left", fill="both", expand=True)

    for _text_widget in (dm_log, group_log):
        _text_widget.tag_configure("system", foreground=DIM_FG, font=FONT + ("italic",))
        _text_widget.tag_configure("me", font=MONO_BOLD)
        for _i, (_tag, _color) in enumerate(ANSI_TAG_COLORS.items()):
            _text_widget.tag_configure(f"peer{_i}", foreground=_color)

    dm_conversations = {}   # ip -> {peer_id, name, messages[(outgoing,name,text,color_idx)]}
    dm_order = []
    online_dm_order = []    # [(peer_id, display_name, ip)] currently reachable
    selected_dm = {"key": None}
    group_messages = {}     # group_id -> messages[(outgoing,name,text,color_idx)]
    group_order = []
    selected_group = {"group_id": None}

    # --- transcript rendering, and DM/group list + tab-switching helpers ---
    def _display_time_prefix():
        return (timestamp() + " ") if gui_settings["show_timestamps"] else ""

    def _render_transcript(widget, messages):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for outgoing, sender_name, text, color_idx in messages:
            prefix = _display_time_prefix()
            if outgoing:
                widget.insert("end", f"{prefix}{MY_NAME}: ", "me")
                widget.insert("end", text + "\n")
            else:
                tag = f"peer{color_idx}" if isinstance(color_idx, int) and 0 <= color_idx < len(COLORS) else None
                widget.insert("end", f"{prefix}{sender_name}: ", tag or ())
                widget.insert("end", text + "\n")
        widget.configure(state="disabled")
        widget.see("end")

    def _refresh_online_dm_list():
        rows = [(pid, name, ip) for pid, name, _, ip in _reachable_people() if pid != MY_ID]
        online_dm_order[:] = rows
        online_dm_list.delete(0, "end")
        for pid, name, _ in rows:
            online_dm_list.insert("end", f"● {name}")

    def _refresh_dm_list():
        dm_list.delete(0, "end")
        with names_lock:
            names_snapshot = dict(peer_names)
        for dm_key in dm_order:
            conversation = dm_conversations[dm_key]
            peer_id = conversation["peer_id"]
            if peer_id in names_snapshot:
                conversation["name"] = names_snapshot[peer_id]
            dm_list.insert("end", conversation["name"])
        if selected_dm["key"] in dm_order:
            idx = dm_order.index(selected_dm["key"])
            dm_list.selection_set(idx)
            dm_list.see(idx)

    def open_dm(peer_id, name=None, ip=None):
        if peer_id == MY_ID:
            return
        dm_key = ip or stable_peer_ip(peer_id) or peer_id
        if dm_key not in dm_conversations:
            with names_lock:
                resolved = name or peer_names.get(peer_id, peer_id)
            dm_conversations[dm_key] = {"peer_id": peer_id, "name": resolved, "messages": []}
            dm_order.append(dm_key)
        elif name:
            dm_conversations[dm_key]["name"] = name
        dm_conversations[dm_key]["peer_id"] = peer_id
        selected_dm["key"] = dm_key
        _refresh_dm_list()
        _render_transcript(dm_log, dm_conversations[dm_key]["messages"])
        switch_conversation_tab("dms")

    def _on_dm_select(event=None):
        sel = dm_list.curselection()
        if not sel:
            return
        dm_key = dm_order[sel[0]]
        selected_dm["key"] = dm_key
        _render_transcript(dm_log, dm_conversations[dm_key]["messages"])
        if active_tab.get() == "dms":
            prompt_var.set(f"DM @{dm_conversations[dm_key]['name']}>")

    def _on_online_dm_select(event=None):
        sel = online_dm_list.curselection()
        if not sel or sel[0] >= len(online_dm_order):
            return
        pid, name, ip = online_dm_order[sel[0]]
        # Clear immediately so keyboard navigation does not retrigger a stale
        # selection while open_dm() refreshes the recent-conversation list.
        online_dm_list.selection_clear(0, "end")
        open_dm(pid, name, ip)

    dm_list.bind("<<ListboxSelect>>", _on_dm_select)
    online_dm_list.bind("<<ListboxSelect>>", _on_online_dm_select)

    def handle_dm_event(data):
        pid = data.get("peer_id")
        if not isinstance(pid, str) or len(pid) != 8:
            return
        name = str(data.get("name") or pid)
        dm_key = data.get("dm_key") or stable_peer_ip(pid) or pid
        if dm_key not in dm_conversations:
            dm_conversations[dm_key] = {"peer_id": pid, "name": name, "messages": []}
            dm_order.append(dm_key)
        else:
            dm_conversations[dm_key]["name"] = name
            dm_conversations[dm_key]["peer_id"] = pid
        dm_conversations[dm_key]["messages"].append((
            bool(data.get("outgoing")), MY_NAME if data.get("outgoing") else name,
            str(data.get("text", "")), int(data.get("color_idx", 0)),
        ))
        _refresh_dm_list()
        if selected_dm["key"] == dm_key:
            _render_transcript(dm_log, dm_conversations[dm_key]["messages"])

    def _refresh_group_list():
        group_list.delete(0, "end")
        with group_lock:
            snapshot = {gid: group_chats.get(gid) for gid in group_order}
        for gid in group_order:
            group = snapshot.get(gid)
            group_list.insert("end", group["name"] if group else gid)
        if selected_group["group_id"] in group_order:
            idx = group_order.index(selected_group["group_id"])
            group_list.selection_set(idx)
            group_list.see(idx)

    def _on_group_select(event=None):
        sel = group_list.curselection()
        if not sel:
            return
        gid = group_order[sel[0]]
        selected_group["group_id"] = gid
        _render_transcript(group_log, group_messages.get(gid, []))
        if active_tab.get() == "groups":
            with group_lock:
                group_name = group_chats.get(gid, {}).get("name", gid)
            prompt_var.set(f"#{group_name}>")

    group_list.bind("<<ListboxSelect>>", _on_group_select)

    def handle_group_create_event(data):
        gid = data.get("group_id")
        if not isinstance(gid, str):
            return
        if gid not in group_order:
            group_order.append(gid)
            group_messages.setdefault(gid, [])
        _refresh_group_list()
        if selected_group["group_id"] is None:
            selected_group["group_id"] = gid
            _refresh_group_list()
            _render_transcript(group_log, group_messages[gid])

    def handle_group_message_event(data):
        gid = data.get("group_id")
        if not isinstance(gid, str):
            return
        if gid not in group_order:
            group_order.append(gid)
        group_messages.setdefault(gid, []).append((
            bool(data.get("outgoing")), str(data.get("sender_name") or data.get("sender_id") or "unknown"),
            str(data.get("text", "")), int(data.get("color_idx", 0)),
        ))
        _refresh_group_list()
        if selected_group["group_id"] == gid:
            _render_transcript(group_log, group_messages[gid])

    def switch_conversation_tab(tab):
        if tab not in ("chat", "dms", "groups"):
            return
        active_tab.set(tab)
        chat_log.pack_forget()
        dm_frame.pack_forget()
        group_frame.pack_forget()
        if tab == "chat":
            chat_log.pack(fill="both", expand=True)
            prompt_var.set(f"<@{MY_NAME}>")
        elif tab == "dms":
            dm_frame.pack(fill="both", expand=True)
            dm_key = selected_dm["key"]
            prompt_var.set(f"DM @{dm_conversations[dm_key]['name']}>" if dm_key in dm_conversations else "DM> ")
        else:
            group_frame.pack(fill="both", expand=True)
            gid = selected_group["group_id"]
            with group_lock:
                gname = group_chats.get(gid, {}).get("name") if gid else None
            prompt_var.set(f"#{gname}>" if gname else "GROUP> ")
        theme = THEMES[current_theme]
        for key, btn in tab_buttons.items():
            btn.configure(fg=theme["accent"] if key == tab else theme["dim"])
        entry.focus_set()

    # --- people list, "new group" dialog ---
    def _reachable_people():
        rows = []
        local_ip = get_local_ip() or "local"
        with color_lock:
            my_color_idx = id_colors.get(MY_ID, 0)
        rows.append((MY_ID, MY_NAME, my_color_idx, local_ip))
        with lock:
            direct_ips = list(peers.keys())
        direct_by_id = {}
        with names_lock:
            for ip in direct_ips:
                pid = ip_to_id.get(ip)
                if pid:
                    direct_by_id[pid] = ip
            names_snapshot = dict(peer_names)
        with route_lock:
            route_snapshot = dict(next_hop_for_peer)
        now = time.time()
        reachable = set(direct_by_id)
        reachable.update(pid for pid, (_, learned) in route_snapshot.items() if now - learned <= ROUTE_STALE_AFTER)
        reachable.discard(MY_ID)
        with color_lock:
            colors_snapshot = dict(id_colors)
        for pid in sorted(reachable, key=lambda x: names_snapshot.get(x, x).lower()):
            rows.append((pid, names_snapshot.get(pid, "(unknown)"), colors_snapshot.get(pid, 0), direct_by_id.get(pid, "via mesh")))
        return rows

    def show_people_window():
        win = tk.Toplevel(root)
        win.title("People")
        theme = THEMES[current_theme]
        win.configure(bg=theme["bg"])
        win.geometry("620x360")
        win.minsize(520, 260)
        heading = tk.Frame(win, bg=theme["bg"])
        heading.pack(fill="x", padx=12, pady=(12, 6))
        for text, width in (("Name", 18), ("Colour", 8), ("ID", 12), ("IP / route", 18)):
            tk.Label(heading, text=text, width=width, anchor="w", bg=theme["bg"], fg=theme["dim"], font=SMALL_BOLD).pack(side="left")
        body = tk.Frame(win, bg=theme["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        rows = _reachable_people()
        for pid, name, color_idx, ip_text in rows:
            row = tk.Frame(body, bg=theme["bg"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=name, width=18, anchor="w", bg=theme["bg"], fg=theme["fg"], font=SMALL_FONT).pack(side="left")
            swatch_color = list(ANSI_TAG_COLORS.values())[color_idx % len(ANSI_TAG_COLORS)]
            swatch = tk.Label(row, text=str(color_idx), width=8, bg=swatch_color, fg="#000000", font=SMALL_FONT)
            swatch.pack(side="left")
            tk.Label(row, text=pid, width=12, anchor="w", bg=theme["bg"], fg=theme["fg"], font=SMALL_FONT).pack(side="left")
            tk.Label(row, text=ip_text, width=18, anchor="w", bg=theme["bg"], fg=theme["fg"], font=SMALL_FONT).pack(side="left")
            if pid != MY_ID:
                tk.Button(row, text="DM", command=lambda p=pid, n=name, i=ip_text, w=win: (w.destroy(), open_dm(p, n, i if i != "via mesh" else None)), bg=theme["panel"], fg=theme["accent"], activebackground=theme["dim"], activeforeground=theme["bg"], relief="flat", font=SMALL_FONT).pack(side="right")

    def open_new_group_dialog():
        rows = [r for r in _reachable_people() if r[0] != MY_ID]
        if not rows:
            append_log("No reachable peers are available for a group.\n", "error")
            return
        win = tk.Toplevel(root)
        win.title("New group")
        theme = THEMES[current_theme]
        win.configure(bg=theme["bg"])
        win.geometry("420x480")
        tk.Label(win, text="Group name", bg=theme["bg"], fg=theme["dim"], font=SMALL_FONT, anchor="w").pack(fill="x", padx=14, pady=(14, 4))
        name_field = tk.Entry(win, bg=theme["bg"], fg=theme["fg"], insertbackground=theme["fg"], font=SMALL_FONT)
        name_field.pack(fill="x", padx=14, ipady=5)
        tk.Label(win, text="Members", bg=theme["bg"], fg=theme["dim"], font=SMALL_FONT, anchor="w").pack(fill="x", padx=14, pady=(14, 4))
        choices = []
        list_frame = tk.Frame(win, bg=theme["panel"])
        list_frame.pack(fill="both", expand=True, padx=14)
        for pid, pname, _, _ in rows:
            var = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(list_frame, text=f"{pname} ({pid})", variable=var, bg=theme["panel"], fg=theme["fg"], activebackground=theme["panel"], activeforeground=theme["fg"], selectcolor=theme["panel"], font=SMALL_FONT, anchor="w")
            cb.pack(fill="x", padx=8, pady=3)
            choices.append((pid, var))
        status = tk.StringVar(value="")
        tk.Label(win, textvariable=status, bg=theme["bg"], fg=theme["error"], font=SMALL_FONT, anchor="w").pack(fill="x", padx=14, pady=(8, 0))
        def create_now():
            selected = [pid for pid, var in choices if var.get()]
            if not name_field.get().strip():
                status.set("Enter a group name.")
                return
            if not selected:
                status.set("Select at least one other member.")
                return
            gid = create_group_chat(name_field.get().strip(), selected)
            if gid:
                selected_group["group_id"] = gid
                win.destroy()
                switch_conversation_tab("groups")
        tk.Button(win, text="Create group", command=create_now, bg=theme["panel"], fg=theme["accent"], activebackground=theme["dim"], activeforeground=theme["bg"], font=SMALL_BOLD, relief="flat").pack(fill="x", padx=14, pady=14)

    # --- incoming-line rendering: ui_output_queue lines -> styled chat log ---
    _ansi_split_re = _re.compile(r"(\x1b\[[0-9;]*m)")

    def strip_ansi(text):
        return _ansi_split_re.sub("", text)

    def append_log(text, tag=None):
        chat_log.configure(state="normal")
        if tag:
            chat_log.insert("end", text, tag)
        else:
            chat_log.insert("end", text)
        chat_log.configure(state="disabled")
        chat_log.see("end")

    def append_log_ansi(text, extra_tag=None):
        """Inserts text that may contain raw ANSI color codes, translating
        each colored segment into the matching Tk tag (combined with
        extra_tag, e.g. "error", if given) instead of showing/discarding
        the escape codes."""
        chat_log.configure(state="normal")
        current_tag = None
        for part in _ansi_split_re.split(text):
            if not part:
                continue
            if part in ANSI_TO_TAG:
                current_tag = ANSI_TO_TAG[part]
                continue
            if part == "\033[0m":
                current_tag = None
                continue
            tags = tuple(t for t in (current_tag, extra_tag) if t)
            chat_log.insert("end", part, tags)
        chat_log.configure(state="disabled")
        chat_log.see("end")

    def classify_and_append(line):
        raw = line.rstrip("\n")
        if raw.startswith("@@DECHAT_DM@@"):
            try:
                handle_dm_event(json.loads(raw[len("@@DECHAT_DM@@"):]))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
            return
        if raw.startswith("@@DECHAT_GROUP_CREATE@@"):
            try:
                handle_group_create_event(json.loads(raw[len("@@DECHAT_GROUP_CREATE@@"):]))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
            return
        if raw.startswith("@@DECHAT_GROUP_MSG@@"):
            try:
                handle_group_message_event(json.loads(raw[len("@@DECHAT_GROUP_MSG@@"):]))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
            return
        stripped = strip_ansi(raw)

        # Presence notifications are a UI preference; protocol state still
        # updates normally even when these lines are hidden.
        if not gui_settings["show_presence"] and (
            "Connected:" in stripped or "Disconnected:" in stripped or "has joined" in stripped
        ):
            return

        def maybe_hide_timestamp(value):
            if gui_settings["show_timestamps"]:
                return value
            # Chat/display lines use HH:MM as their first five characters.
            plain = strip_ansi(value)
            if len(plain) >= 6 and plain[2] == ":" and plain[5] == " ":
                return value[6:]
            return value

        if raw.startswith("\a<<PING>>") and raw.endswith("<<PING>>"):
            payload = raw[len("\a<<PING>>"):-len("<<PING>>")]
            if gui_settings["sound_on_ping"]:
                root.bell()
            append_log_ansi(maybe_hide_timestamp(payload) + "\n", "pinged")
        elif stripped.startswith("*") and stripped.endswith("*"):
            append_log(stripped + "\n", "system")
        elif (
            "Connected:" in stripped or "Disconnected:" in stripped or "Pong from" in stripped
            or "has joined" in stripped or "wants to send you a file" in stripped
            or "Type /accept" in stripped
        ):
            append_log_ansi(maybe_hide_timestamp(raw) + "\n", "system")
        elif stripped.startswith(f"{timestamp()} {MY_NAME}:") or f" {MY_NAME}: " in stripped[:40]:
            append_log_ansi(maybe_hide_timestamp(raw) + "\n", "me")
        elif "Unknown command" in stripped or "Usage:" in stripped or "Failed" in stripped or "Could not" in stripped:
            append_log(maybe_hide_timestamp(stripped) + "\n", "error")
        else:
            append_log_ansi(maybe_hide_timestamp(raw) + "\n")

    COMMANDS = [
        ("/connect <ip>", "manually connect to a peer by IP"),
        ("/recent [count]", "show recent messages with their ids"),
        ("/reply <msg_id> <text>", "reply to a message (pings its sender)"),
        ("/name <newname>", "change your display name"),
        ("/ping <name>", "measure round-trip latency"),
        ("/sendfile <name> <path>", "offer to send a file to a peer"),
        ("/accept <n>", "accept a pending file offer"),
        ("/reject <n>", "decline a pending file offer"),
        ("/offers", "list pending incoming file offers"),
        ("/ignore <name>", "stop showing a peer's messages"),
        ("/unignore <name>", "resume showing a peer's messages"),
        ("/clear", "clear the chat window"),
        ("/quit", "exit dechat"),
    ]
    for cmd, desc in COMMANDS:
        row = tk.Frame(help_frame, bg=PANEL_BG)
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text=cmd, fg=ACCENT, bg=PANEL_BG, font=FONT, anchor="w", width=24).pack(side="left")
        tk.Label(row, text=desc, fg=DIM_FG, bg=PANEL_BG, font=FONT, anchor="w").pack(side="left")

    # --- help panel / open-panel-exclusivity (Escape closes whichever panel is open) ---
    def toggle_help():
        if help_visible.get():
            help_frame.pack_forget()
            help_visible.set(False)
            help_btn.config(text="?")
        else:
            # side="bottom" + before=input_bar: pin it directly above the
            # (also bottom-anchored) input bar, so opening it only eats into
            # the chat log's space above, and the input bar stays put.
            help_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 8), before=input_bar)
            help_visible.set(True)
            help_btn.config(text="x")

    def close_open_panel(event=None):
        if help_visible.get():
            toggle_help()
        elif settings_visible.get():
            toggle_settings_panel()
        elif news_visible.get():
            toggle_news_panel()

    root.bind("<Escape>", close_open_panel)

    # --- input entry, prompt, and theme/settings application ---
    prompt_var = tk.StringVar(value=f"<@{MY_NAME}>")
    prompt_label = tk.Label(input_bar, textvariable=prompt_var, fg=FG, bg=BG, font=MONO_BOLD)
    prompt_label.pack(side="left", padx=(2, 6))

    entry = tk.Entry(
        input_bar, bg=BG, fg=FG, insertbackground=FG, font=FONT,
        relief="flat", highlightthickness=1, highlightbackground=DIM_FG,
        highlightcolor=ACCENT,
    )
    entry.pack(side="left", fill="x", expand=True, ipady=6)
    entry.focus_set()

    def apply_theme(name):
        """Apply a GUI theme explicitly to every themed GUI surface."""
        nonlocal current_theme

        if name not in THEMES:
            return False
        theme = THEMES[name]
        bg = theme["bg"]
        panel = theme["panel"]
        fg = theme["fg"]
        dim = theme["dim"]
        accent = theme["accent"]

        # Main window and structural frames.
        root.configure(bg=bg)
        header.configure(bg=bg)
        divider.configure(bg=dim)
        input_bar.configure(bg=bg)
        chat_frame.configure(bg=bg)
        help_frame.configure(bg=panel, highlightbackground=dim)
        news_frame.configure(bg=panel, highlightbackground=dim)
        news_inner.configure(bg=panel)
        settings_frame.configure(bg=panel, highlightbackground=dim)
        settings_inner.configure(bg=panel)

        # Header.
        title_label.configure(bg=bg, fg=accent)
        name_label.configure(bg=bg, fg=fg)
        people_btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=accent)
        news_toggle_btn.configure(bg=bg, fg=accent if news_visible.get() else dim, activebackground=bg, activeforeground=accent)
        settings_toggle_btn.configure(bg=bg, fg=accent if settings_visible.get() else dim, activebackground=bg, activeforeground=accent)

        # Chat area -- configure this explicitly; ScrolledText must not rely on
        # generic widget-tree colour replacement.
        chat_log.configure(bg=bg, fg=fg, insertbackground=fg)
        chat_log.tag_configure("dim", foreground=dim)
        chat_log.tag_configure("system", foreground=dim)
        chat_log.tag_configure("error", foreground=theme["error"])
        chat_log.tag_configure("pinged", background=theme["ping_bg"])

        # Input controls.
        prompt_label.configure(bg=bg, fg=fg)
        entry.configure(bg=bg, fg=fg, insertbackground=fg, highlightbackground=dim, highlightcolor=accent)
        send_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)
        help_btn.configure(bg=panel, fg=dim, activebackground=dim, activeforeground=bg)

        # News panel.
        news_heading_label.configure(bg=panel, fg=dim)
        recent_news_label.configure(bg=panel, fg=accent)
        if news_details_label is not None:
            news_details_label.configure(bg=panel, fg=fg)

        # Help panel rows/labels were created dynamically, so recolour this
        # small subtree by widget type.
        for child in help_frame.winfo_children():
            try:
                child.configure(bg=panel)
            except tk.TclError:
                pass
            for grandchild in child.winfo_children():
                try:
                    current_fg = grandchild.cget("fg")
                    grandchild.configure(bg=panel, fg=accent if current_fg in (THEMES[current_theme]["accent"], THEMES[current_theme]["fg"]) and grandchild.cget("width") == 24 else dim)
                except tk.TclError:
                    pass

        # Settings panel.
        settings_title_label.configure(bg=panel, fg=accent)
        settings_status_label.configure(bg=panel, fg=dim)
        for label in settings_labels:
            label.configure(bg=panel, fg=dim)
        name_entry.configure(bg=bg, fg=fg, insertbackground=fg, highlightbackground=dim, highlightcolor=accent)
        theme_menu.configure(bg=panel, fg=fg, activebackground=dim, activeforeground=bg, highlightbackground=dim)
        theme_menu["menu"].configure(bg=panel, fg=fg)
        color_menu.configure(bg=panel, fg=fg, activebackground=dim, activeforeground=bg, highlightbackground=dim)
        color_menu["menu"].configure(bg=panel, fg=fg)
        for control in settings_controls:
            if isinstance(control, tk.Checkbutton):
                control.configure(bg=panel, fg=fg, activebackground=panel, activeforeground=fg, selectcolor=panel)
        apply_settings_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)

        # Conversation tabs and their panes.
        tab_bar.configure(bg=bg)
        for key, btn in tab_buttons.items():
            btn.configure(bg=bg, fg=accent if key == active_tab.get() else dim, activebackground=bg, activeforeground=accent)
        for frame in (dm_frame, group_frame):
            frame.configure(bg=bg)
        for sidebar in (dm_sidebar, group_sidebar):
            sidebar.configure(bg=panel)
        for listbox in (online_dm_list, dm_list, group_list):
            listbox.configure(bg=panel, fg=fg, selectbackground=dim, selectforeground=fg)
        for sidebar_label in (dm_online_label, dm_recent_label):
            sidebar_label.configure(bg=panel, fg=dim)
        new_group_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)
        for text_widget in (dm_log, group_log):
            text_widget.configure(bg=bg, fg=fg, insertbackground=fg)
            text_widget.tag_configure("system", foreground=dim)

        current_theme = name
        theme_var.set(name)
        return True

    def apply_settings():
        global SHOW_ID_INSTEAD_OF_NAME
        requested_theme = theme_var.get().strip().lower()
        if requested_theme not in THEMES:
            settings_status.set("Unknown theme.")
            return

        new_name = name_var.get().strip()
        if not new_name:
            settings_status.set("Display name cannot be empty.")
            return

        try:
            requested_color = int(color_var.get())
        except ValueError:
            settings_status.set("Invalid chat colour.")
            return
        if not (0 <= requested_color < len(COLORS)):
            settings_status.set("Invalid chat colour.")
            return
        with color_lock:
            current_color = id_colors.get(MY_ID)

        apply_theme(requested_theme)
        if requested_color != current_color:
            process_command(f"/color {requested_color}")
        gui_settings["sound_on_ping"] = bool(sound_var.get())
        gui_settings["show_timestamps"] = bool(timestamps_var.get())
        gui_settings["show_presence"] = bool(presence_var.get())
        gui_settings["show_id"] = bool(show_id_var.get())
        SHOW_ID_INSTEAD_OF_NAME = gui_settings["show_id"]

        if new_name != MY_NAME:
            process_command(f"/name {new_name}")
            # Preserve the selected colour under the new saved display name too.
            remember_color_for_name(MY_NAME, requested_color)
            name_label.config(text=f" @{MY_NAME}")
            root.title(f"dechat* @{MY_NAME}")
            name_var.set(MY_NAME)
            switch_conversation_tab(active_tab.get())

        settings_status.set("Settings applied.")

    apply_settings_btn = tk.Button(
        settings_inner, text="Apply", command=apply_settings,
        bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=12, pady=5,
    )
    apply_settings_btn.pack(fill="x", pady=(14, 0))
    settings_controls.append(apply_settings_btn)

    def toggle_settings_panel():
        if settings_visible.get():
            settings_frame.pack_forget()
            settings_visible.set(False)
            settings_toggle_btn.config(fg=THEMES[current_theme]["dim"])
            return
        if news_visible.get():
            news_frame.pack_forget()
            news_visible.set(False)
            news_toggle_btn.config(fg=THEMES[current_theme]["dim"])
        settings_frame.pack(side="right", fill="y", padx=(0, 10), pady=8, before=chat_frame)
        settings_visible.set(True)
        settings_toggle_btn.config(fg=THEMES[current_theme]["accent"])

    settings_toggle_btn.configure(command=toggle_settings_panel)

    # --- sending input: slash commands, DMs, group messages, file picker ---
    def send_current():
        text = entry.get()
        entry.delete(0, "end")
        if not text:
            return
        # Slash commands remain available from any tab, but ordinary text is
        # routed to the active conversation.
        if not text.startswith("/") and active_tab.get() == "dms":
            dm_key = selected_dm["key"]
            conversation = dm_conversations.get(dm_key) if dm_key else None
            pid = conversation.get("peer_id") if conversation else None
            if not conversation or not pid:
                dm_log.configure(state="normal")
                dm_log.insert("end", "Select a person from the People button first.\n", "system")
                dm_log.configure(state="disabled")
                return
            send_private_message(pid, text)
            return
        if not text.startswith("/") and active_tab.get() == "groups":
            gid = selected_group["group_id"]
            if not gid:
                group_log.configure(state="normal")
                group_log.insert("end", "Create or select a group first.\n", "system")
                group_log.configure(state="disabled")
                return
            send_group_message(gid, text)
            return
        if text == "/help":
            toggle_help()
            return
        if text == "/clear":
            target_log = chat_log
            if active_tab.get() == "dms":
                target_log = dm_log
            elif active_tab.get() == "groups":
                target_log = group_log
            target_log.configure(state="normal")
            target_log.delete("1.0", "end")
            target_log.configure(state="disabled")
            return
        if text == "/sendfile" or text.startswith("/sendfile "):
            parts = text.split(" ", 2)
            if len(parts) < 3:
                # Offer a native file picker if no path was typed.
                target = parts[1] if len(parts) > 1 else None
                if not target:
                    append_log("Usage: /sendfile <name|id> <path>\n", "error")
                    return
                path = filedialog.askopenfilename(title="Choose file to send")
                if not path:
                    return
                text = f"/sendfile {target} {path}"
        should_continue = process_command(text)
        if text.startswith("/name "):
            # MY_NAME just changed; reflect it in the header and prompt.
            name_label.config(text=f" @{MY_NAME}")
            prompt_var.set(f"<@{MY_NAME}>")
            root.title(f"dechat* @{MY_NAME}")
        if not should_continue:
            on_close()

    send_btn = tk.Button(
        input_bar, text="Send", command=send_current,
        bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=14,
    )
    send_btn.pack(side="left", padx=(8, 0))
    entry.bind("<Return>", lambda e: send_current())

    help_btn = tk.Button(
        input_bar, text="?", command=toggle_help,
        bg=PANEL_BG, fg=DIM_FG, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=10,
    )
    help_btn.pack(side="left", padx=(6, 0))

    # --- background refresh loops, window lifecycle, and mainloop kickoff ---
    def refresh_peer_count():
        # Count currently reachable other people (direct or a fresh mesh route)
        # and keep the DM tab's always-visible online list in sync.
        count = max(0, len(_reachable_people()) - 1)
        peer_count_var.set(f"{count} peer" if count == 1 else f"{count} peers")
        _refresh_online_dm_list()
        root.after(1000, refresh_peer_count)

    def drain_queue():
        try:
            while True:
                line = ui_output_queue.get_nowait()
                classify_and_append(line)
        except queue.Empty:
            pass
        root.after(80, drain_queue)

    closing = {"done": False}

    def on_close():
        if closing["done"]:
            return
        closing["done"] = True
        shutdown_networking()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    if not start_networking():
        from tkinter import messagebox
        messagebox.showerror(
            "dechat",
            f"Could not start dechat: port {CHAT_PORT} is already in use.\n"
            f"Another instance of dechat may already be running on this machine.",
        )
        root.destroy()
        return

    append_log("Type /help to toggle the command list, or /quit to exit.\n", "system")
    switch_conversation_tab("chat")

    refresh_peer_count()
    drain_queue()
    root.mainloop()

# Launcher: ask the user whether they want the TUI or the GUI
def choose_mode():
    while True:
        try:
            choice = input(
                "Do you want to use TUI (runs in terminal) or GUI (opens an app)? [t/g]: "
            ).strip().lower()
        except EOFError:
            return "t"
        if choice in ("g", "gui"):
            return "g"
        if choice in ("t", "tui", ""):
            return "t"
        print("Please type 't' for TUI or 'g' for GUI.")


if __name__ == "__main__":
    mode = choose_mode()
    if mode == "g":
        try:
            run_gui()
        except ImportError:
            print("tkinter isn't available on this system, falling back to TUI.")
            run_tui()
    else:
        run_tui()

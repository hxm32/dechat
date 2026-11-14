"""Shared runtime state.

Containers and locks can be imported by name. Values that get rebound
(running, server, discovery_socket, my_name, show_id_instead_of_name,
incoming_bytes_buffered, next_offer_num) must be accessed as state.<name>.
"""
import threading

from dechat.core.config import MAX_CONCURRENT_CONNECT_ATTEMPTS
from dechat.core.identity import load_saved_name

running = True
server = None
discovery_socket = None
my_name = load_saved_name()
show_id_instead_of_name = False

# Direct connections: ip -> socket
lock = threading.Lock()
peers = {}

# Identity bookkeeping
names_lock = threading.Lock()
peer_names = {}
ip_to_id = {}
announced_peer_ids = {}

# peer_id -> session key learned from that peer's direct KEY packet
peer_keys_lock = threading.Lock()
peer_session_keys = {}

# Connection attempts
attempted_lock = threading.Lock()
attempted_connections = set()
deferred_until = {}
connect_attempt_semaphore = threading.Semaphore(MAX_CONCURRENT_CONNECT_ATTEMPTS)

known_ips_lock = threading.Lock()
known_ips = set()
reconnecting_ips = set()

# Relay dedup and routes
seen_lock = threading.Lock()
seen_msg_ids = {}
route_lock = threading.Lock()
next_hop_for_peer = {}

# Rate limiting
rate_lock = threading.Lock()
rate_buckets = {}
file_rate_lock = threading.Lock()
file_rate_buckets = {}

# Chat features
ping_lock = threading.Lock()
pending_pings = {}
msg_history_lock = threading.Lock()
msg_history = {}
group_lock = threading.Lock()
group_chats = {}
ignore_lock = threading.Lock()
ignored_ids = set()

# File transfers
transfer_lock = threading.Lock()
incoming_transfers = {}
incoming_bytes_buffered = 0
outgoing_transfers_lock = threading.Lock()
outgoing_transfers = {}
offer_lock = threading.Lock()
pending_offers = {}
next_offer_num = 1
offer_num_to_id = {}
pending_sends_lock = threading.Lock()
pending_sends = {}

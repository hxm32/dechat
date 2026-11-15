import time

from dechat.core.config import MAX_SEEN_IDS, RELAY_TTL
from dechat.core.identity import sign_packet
from dechat.core.state import lock, peers, seen_lock, seen_msg_ids
from dechat.core.utils import new_packet_id
from dechat.net.wire import send_to_all


def mark_seen(packet_id):
    """True if this is the first time we've seen packet_id."""
    with seen_lock:
        if packet_id in seen_msg_ids:
            return False
        seen_msg_ids[packet_id] = time.time()
        if len(seen_msg_ids) > MAX_SEEN_IDS:
            oldest = next(iter(seen_msg_ids))
            seen_msg_ids.pop(oldest, None)
        return True


def wrap_relay(inner_line):
    """Wraps a protocol line as RELAY|packet_id|ttl|tag|inner_line."""
    packet_id = new_packet_id()
    mark_seen(packet_id)
    tag = sign_packet(inner_line)
    return f"RELAY|{packet_id}|{RELAY_TTL}|{tag}|{inner_line}"


def relay_onward(packet_id, ttl, tag, inner_line, from_ip):
    """Floods a packet to all neighbors except the sender. The tag is never re-signed."""
    if ttl <= 0:
        return
    send_to_all(f"RELAY|{packet_id}|{ttl - 1}|{tag}|{inner_line}", exclude_ip=from_ip)


def send_to_peer_id(inner_line):
    """Floods a point-to-point packet. Only its target acts on it."""
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        return False
    send_to_all(wrap_relay(inner_line))
    return True

import time

from dechat.core.config import ROUTE_STALE_AFTER
from dechat.core.identity import MY_ID
from dechat.core.state import lock, next_hop_for_peer, peers, route_lock
from dechat.net.relay import wrap_relay
from dechat.net.wire import send_line_to_peer, send_to_all


def learn_route(peer_id, from_ip):
    """Records from_ip as a next hop toward peer_id."""
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
    """Unicasts along a learned route, falling back to a flood."""
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        return False

    wrapped = wrap_relay(inner_line)
    next_hop = get_route(peer_id)
    if next_hop is not None:
        with lock:
            sock = peers.get(next_hop)
        if sock is not None and send_line_to_peer(sock, next_hop, wrapped):
            return True

    send_to_all(wrapped)
    return True

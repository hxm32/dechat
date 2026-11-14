import hmac
import threading

from dechat.core.config import MAX_TRACKED_PEERS
from dechat.core.identity import MY_ID, make_tag
from dechat.core.state import ip_to_id, lock, names_lock, peer_keys_lock, peer_session_keys, peers
from dechat.net.routing import learn_route
from dechat.net.wire import drop_peer


def claim_identity(ip, peer_id):
    """Binds an ip to the first peer_id it announces.

    Also rejects a second ip claiming an id that is already bound to a
    connected ip (one machine with two network interfaces).
    """
    with names_lock:
        bound = ip_to_id.get(ip)
        if bound is not None:
            return bound == peer_id

        if len(ip_to_id) >= MAX_TRACKED_PEERS:
            return False

        existing_ip = next(
            (other_ip for other_ip, other_id in ip_to_id.items() if other_id == peer_id),
            None,
        )
        if existing_ip is not None:
            with lock:
                still_connected = existing_ip in peers
            if still_connected:
                threading.Thread(target=drop_peer, args=(ip,), daemon=True).start()
                return False

        ip_to_id[ip] = peer_id
        return True


def learn_or_verify_key(peer_id, tag, inner_line):
    """Checks the packet's HMAC against the key we hold for peer_id.

    With no key on file the packet is rejected. Keys only come from a
    direct KEY packet (see learn_key), never from relayed traffic.
    """
    if not tag or len(tag) != 16:
        return False
    with peer_keys_lock:
        known_key = peer_session_keys.get(peer_id)
    if known_key is None:
        return False
    return hmac.compare_digest(make_tag(known_key, inner_line), tag)


def learn_key(peer_id, is_direct, session_key_hex):
    """Stores a peer's session key. Only accepted from a direct connection."""
    if not is_direct:
        return
    try:
        session_key = bytes.fromhex(session_key_hex)
    except ValueError:
        return
    if len(session_key) != 32:
        return
    with peer_keys_lock:
        peer_session_keys.setdefault(peer_id, session_key)


def verify_sender(is_direct, ip, peer_id, inner_line, tag):
    """Identity bookkeeping plus HMAC check. Learns a route on success."""
    if is_direct and not claim_identity(ip, peer_id):
        return False
    if inner_line is None or tag is None:
        return False
    ok = learn_or_verify_key(peer_id, tag, inner_line)
    if ok:
        learn_route(peer_id, ip)
    return ok


def authentic(peer_id, is_direct, ip, signed_line, tag):
    return len(peer_id) == 8 and verify_sender(is_direct, ip, peer_id, signed_line, tag)


def authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
    """Valid, verified packet addressed to us."""
    return (
        len(target_id) == 8
        and authentic(peer_id, is_direct, ip, signed_line, tag)
        and target_id == MY_ID
    )

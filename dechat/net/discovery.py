import socket
import time

from dechat.core import state
from dechat.core.config import ANTI_FLOOD_SET_MAX, DEBUG_DISCOVERY, DISCOVERY_PORT
from dechat.core.identity import MY_ID
from dechat.core.output import ui_print
from dechat.core.state import (
    announced_peer_ids,
    attempted_connections,
    attempted_lock,
    deferred_until,
    ip_to_id,
    lock,
    names_lock,
    peer_names,
    peers,
)
from dechat.core.storage import cached_peer_name, remember_peer_session
from dechat.net.connections import at_degree_cap, connect_to_peer


def get_local_ip():
    """LAN-facing ip, found by asking the OS which source address it would use."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def announce_interval():
    """Announce every 3s for small networks, backing off to 10s for large ones."""
    with names_lock:
        known = len(ip_to_id)
    return min(3 + known / 40.0, 10)


def _broadcast_announce(udp, broadcast_targets):
    message = f"CHAT:{MY_ID}".encode()
    for target in broadcast_targets:
        try:
            udp.sendto(message, (target, DISCOVERY_PORT))
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] sent announce {message} to {target}")
        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] broadcast to {target} failed: {e}")


def _forget_attempt(ip):
    with attempted_lock:
        attempted_connections.discard(ip)


def _process_announce(peer_id, ip):
    """Decides whether to dial a peer heard on the discovery socket."""
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
        still_deferred = deferred_until.get(ip, 0) > time.time()
        if ip in attempted_connections or still_deferred:
            if DEBUG_DISCOVERY:
                reason = "deferred cooldown active" if still_deferred else "already attempted"
                ui_print(f"[discovery] {ip} skipped ({reason})")
            return
        attempted_connections.add(ip)

    with lock:
        known = ip in peers

    # The same id heard from two addresses (e.g. Ethernet + WiFi) must only get one connection
    with names_lock:
        connected_elsewhere = any(
            other_id == peer_id and other_ip != ip
            for other_ip, other_id in ip_to_id.items()
        )
        announced_elsewhere = any(
            other_id == peer_id and other_ip != ip
            for other_ip, other_id in announced_peer_ids.items()
        )

    if connected_elsewhere:
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] {ip} skipped (peer id {peer_id} already connected via another address)")
        _forget_attempt(ip)
        return

    if announced_elsewhere:
        # Every node picks the lowest address so they converge on the same one
        with names_lock:
            candidates = sorted(a for a, i in announced_peer_ids.items() if i == peer_id)
        preferred = candidates[0] if candidates else ip
        if ip != preferred:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] {ip} skipped (peer id {peer_id} also seen at {preferred}; preferring that address)")
            _forget_attempt(ip)
            return

    if not known and at_degree_cap():
        # Their traffic will still arrive through relays
        _forget_attempt(ip)
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


def _broadcast_targets():
    targets = ["255.255.255.255"]
    local_ip = get_local_ip()
    if local_ip:
        octets = local_ip.split(".")
        if len(octets) == 4:
            subnet_broadcast = ".".join(octets[:3] + ["255"])
            if subnet_broadcast not in targets:
                targets.append(subnet_broadcast)
    return targets, local_ip


def _prune_anti_flood_sets():
    with attempted_lock:
        if len(attempted_connections) > ANTI_FLOOD_SET_MAX:
            attempted_connections.clear()
        now = time.time()
        for ip in [ip for ip, until in deferred_until.items() if until <= now]:
            deferred_until.pop(ip, None)
        if len(deferred_until) > ANTI_FLOOD_SET_MAX:
            deferred_until.clear()


def discovery():
    udp = None
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("", DISCOVERY_PORT))
        udp.settimeout(1)
        state.discovery_socket = udp
    except OSError as e:
        state.discovery_socket = None
        if udp is not None:
            try:
                udp.close()
            except OSError:
                pass
        ui_print(f"Peer discovery is unavailable on UDP port {DISCOVERY_PORT}: {e}")
        return

    broadcast_targets, local_ip = _broadcast_targets()

    if DEBUG_DISCOVERY:
        ui_print(f"[discovery] listening on UDP {DISCOVERY_PORT}, MY_ID={MY_ID}")
        ui_print(f"[discovery] local_ip={local_ip}")
        ui_print(f"[discovery] broadcast targets: {broadcast_targets}")

    last_announcement = 0

    while state.running:
        current_time = time.time()
        if current_time - last_announcement > announce_interval():
            _broadcast_announce(udp, broadcast_targets)
            last_announcement = current_time

        try:
            data, address = udp.recvfrom(1024)
            message = data.decode(errors="replace")

            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] recv {message!r} from {address}")

            if message.startswith("CHAT:"):
                _process_announce(message[5:], address[0])

        except socket.timeout:
            _prune_anti_flood_sets()
        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] recvfrom failed, exiting loop: {e}")
            break

    udp.close()

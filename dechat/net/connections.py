import random
import socket
import threading
import time

from dechat.core import state
from dechat.core.colors import color_index_for_id, forget_color
from dechat.core.config import (
    CHAT_PORT,
    DEBUG_DISCOVERY,
    DEFER_RETRY_COOLDOWN,
    DISCONNECT_RETRY_COOLDOWN,
    MAX_PEER_DEGREE,
    MAX_RECONNECT_ATTEMPTS,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
)
from dechat.core.identity import MY_ID, MY_SESSION_KEY
from dechat.core.output import ui_print
from dechat.core.state import (
    attempted_connections,
    attempted_lock,
    connect_attempt_semaphore,
    deferred_until,
    file_rate_buckets,
    file_rate_lock,
    ip_to_id,
    known_ips,
    known_ips_lock,
    lock,
    names_lock,
    peer_keys_lock,
    peer_names,
    peer_session_keys,
    peers,
    rate_buckets,
    rate_lock,
    reconnecting_ips,
)
from dechat.core.utils import escape_field
from dechat.net.ratelimit import file_rate_limited, rate_limited
from dechat.net.relay import wrap_relay
from dechat.net.wire import recv_lines, send_line_to_peer
from dechat.protocol.dispatch import handle_incoming

_FILE_PREFIXES = (b"FILE|", b"FILE_OFFER|", b"FILE_ACCEPT|", b"FILE_DECLINE|", b"FILE_ACK|")


def _is_file_packet(line):
    """True for FILE* packets, looking inside a RELAY envelope if present."""
    target = line
    if target.startswith(b"RELAY|"):
        envelope = target.split(b"|", 4)
        if len(envelope) == 5:
            target = envelope[4]
    return target.startswith(_FILE_PREFIXES)


def _send_handshake(peer, ip):
    """KEY goes out bare and first so the peer has our key before anything signed."""
    send_line_to_peer(peer, ip, f"KEY|{MY_ID}|{MY_SESSION_KEY.hex()}")
    send_line_to_peer(peer, ip, wrap_relay(f"COLOR|{MY_ID}|{color_index_for_id(MY_ID)}"))
    send_line_to_peer(peer, ip, wrap_relay(f"NAME|{MY_ID}|{escape_field(state.my_name)}"))


def _start_receiver(peer, address):
    threading.Thread(target=receive, args=(peer, address), daemon=True).start()


def at_degree_cap():
    with lock:
        return len(peers) >= MAX_PEER_DEGREE


def receive(peer, address):
    ip = address[0]
    buffer = b""

    try:
        peer.settimeout(60)
    except OSError:
        pass

    while state.running:
        try:
            lines, buffer = recv_lines(peer, buffer)
            if lines is None:
                if DEBUG_DISCOVERY:
                    ui_print(f"[receive] {ip} peer closed the connection (recv returned empty)")
                break

            for line in lines:
                if not line:
                    continue
                limited = file_rate_limited(ip) if _is_file_packet(line) else rate_limited(ip)
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

    _cleanup_connection(ip)
    _maybe_reconnect(ip)


def _cleanup_connection(ip):
    with lock:
        removed_peer = peers.pop(ip, None)
    if removed_peer:
        try:
            removed_peer.close()
        except OSError:
            pass

    with attempted_lock:
        attempted_connections.discard(ip)
        # Cooldown avoids a tight redial loop after a connection reset
        deferred_until[ip] = time.time() + DISCONNECT_RETRY_COOLDOWN

    with rate_lock:
        rate_buckets.pop(ip, None)
    with file_rate_lock:
        file_rate_buckets.pop(ip, None)

    with names_lock:
        peer_id = ip_to_id.pop(ip, None)

    if peer_id:
        forget_color(peer_id)
        with names_lock:
            peer_names.pop(peer_id, None)
        # A reconnecting peer must send a fresh direct KEY packet
        with peer_keys_lock:
            peer_session_keys.pop(peer_id, None)

    ui_print(f"\nDisconnected: {ip}")
    ui_print("> ", end="", flush=True)


def _maybe_reconnect(ip):
    with known_ips_lock:
        should_retry = ip in known_ips and ip not in reconnecting_ips
        if should_retry:
            reconnecting_ips.add(ip)
    if should_retry and state.running:
        threading.Thread(target=reconnect_worker, args=(ip,), daemon=True).start()


def reconnect_worker(ip):
    try:
        attempt = 0
        while state.running and attempt < MAX_RECONNECT_ATTEMPTS:
            # Jitter so peers that dropped together don't all retry at once
            time.sleep(random.uniform(RECONNECT_DELAY_MIN, RECONNECT_DELAY_MAX))
            if not state.running:
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


def accept_connections(server):
    while state.running:
        try:
            peer, address = server.accept()
        except OSError:
            break

        ip = address[0]

        with lock:
            if ip in peers:
                peer.close()
                continue
            # Inbound gets a higher ceiling than outbound so two nodes at the cap can still reach each other
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

        _send_handshake(peer, ip)
        _start_receiver(peer, address)


def connect_to_peer(ip, remote_id=None):
    with lock:
        if ip in peers:
            return

    # Tie-break: the lower id dials, so two peers don't dial each other at once
    if remote_id and remote_id > MY_ID:
        with attempted_lock:
            deferred_until[ip] = time.time() + DEFER_RETRY_COOLDOWN
        if DEBUG_DISCOVERY:
            ui_print(f"[discovery] deferring to {ip} (their id {remote_id} > ours {MY_ID}); waiting for them to dial in")
        return

    if not connect_attempt_semaphore.acquire(timeout=10):
        with attempted_lock:
            attempted_connections.discard(ip)
            deferred_until.pop(ip, None)
        return

    try:
        try:
            peer = socket.socket()
            peer.settimeout(3)
            peer.connect((ip, CHAT_PORT))
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

            _send_handshake(peer, ip)
            _start_receiver(peer, (ip, CHAT_PORT))

        except OSError as e:
            if DEBUG_DISCOVERY:
                ui_print(f"[discovery] connect_to_peer({ip}) OSError: {e}")
            with attempted_lock:
                attempted_connections.discard(ip)
                deferred_until.pop(ip, None)
    finally:
        connect_attempt_semaphore.release()

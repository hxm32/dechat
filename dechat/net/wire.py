from dechat.core.state import lock, peers


def send_line(sock, text):
    sock.sendall(text.encode() + b"\n")


def drop_peer(ip):
    with lock:
        dead_peer = peers.pop(ip, None)
    if dead_peer:
        try:
            dead_peer.close()
        except OSError:
            pass


def send_line_to_peer(sock, ip, text):
    """Sends to one socket and drops the peer if the send fails."""
    try:
        send_line(sock, text)
        return True
    except (ConnectionError, OSError):
        drop_peer(ip)
        return False


def recv_lines(sock, buffer):
    """Returns (complete_lines, leftover_buffer), or (None, buffer) on close."""
    chunk = sock.recv(4096)
    if not chunk:
        return None, buffer
    buffer += chunk
    lines = buffer.split(b"\n")
    return lines[:-1], lines[-1]


def send_to_all(message, exclude_ip=None):
    """Sends to every direct neighbor, optionally skipping one."""
    with lock:
        items = list(peers.items())
    for ip, peer in items:
        if ip == exclude_ip:
            continue
        try:
            send_line(peer, message)
        except (ConnectionError, OSError):
            drop_peer(ip)

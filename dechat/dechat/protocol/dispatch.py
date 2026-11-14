from dechat.core.config import RELAY_TTL
from dechat.core.output import ui_print
from dechat.core.utils import sanitize_for_display
from dechat.net.auth import learn_key
from dechat.net.relay import mark_seen, relay_onward
from dechat.protocol.chat_handlers import CHAT_HANDLERS
from dechat.protocol.file_handlers import FILE_HANDLERS


def handle_key(parts, is_direct, ip, signed_line, tag):
    _, peer_id, session_key_hex = parts
    if len(peer_id) != 8:
        return
    learn_key(peer_id, is_direct, session_key_hex)


# packet type -> (number of fields, handler)
PACKET_HANDLERS = {"KEY": (3, handle_key), **CHAT_HANDLERS, **FILE_HANDLERS}


def _unwrap_relay(raw, ip):
    """Unwraps a RELAY envelope and forwards it on.

    Returns (inner_line, tag, is_direct), or None if the line should be dropped.
    """
    env = raw.split("|", 4)
    if len(env) != 5:
        return None
    _, packet_id, ttl_str, tag, inner = env
    if not (1 <= len(packet_id) <= 64) or len(tag) != 16:
        return None
    try:
        ttl = int(ttl_str)
    except ValueError:
        return None
    # A full ttl means the neighbor at ip originated the packet itself
    is_direct = ttl == RELAY_TTL
    if not mark_seen(packet_id):
        return None
    relay_onward(packet_id, ttl, tag, inner, from_ip=ip)
    return inner, tag, is_direct


def handle_incoming(raw, ip):
    """Parses one line off the wire and dispatches it by packet type."""
    if not raw or len(raw) > 10000:
        return

    is_direct = True
    tag = None
    if raw.startswith("RELAY|"):
        unwrapped = _unwrap_relay(raw, ip)
        if unwrapped is None:
            return
        raw, tag, is_direct = unwrapped

    # The HMAC covers this exact string, so it must not change before verification
    signed_line = raw

    packet_type = raw.split("|", 1)[0]
    expected_len, handler = PACKET_HANDLERS.get(packet_type, (None, None))
    if handler is not None:
        # Only the last field may contain further escaped '|' data
        parts = raw.split("|", expected_len - 1)
        if len(parts) == expected_len:
            handler(parts, is_direct, ip, signed_line, tag)
            return

    ui_print(sanitize_for_display(raw))
    ui_print("> ", end="", flush=True)

import time

from dechat.core import output, state
from dechat.core.colors import RESET, color_for_id, get_color_index, set_color_for_id
from dechat.core.config import MAX_GROUP_MEMBERS, MAX_GROUP_NAME_LEN
from dechat.core.identity import MY_ID
from dechat.core.output import EVENT_DM, EVENT_GROUP_CREATE, EVENT_GROUP_MSG, ui_print
from dechat.core.state import group_chats, group_lock, ignore_lock, ignored_ids, names_lock, peer_names, ping_lock, pending_pings
from dechat.core.storage import log_message, save_peer_identity
from dechat.core.utils import sanitize_for_display, timestamp, unescape_field
from dechat.features.messages import get_remembered_message, mentions_me, remember_message
from dechat.features.peers import name_of, stable_peer_ip
from dechat.net.auth import authentic, authentic_for_me
from dechat.net.relay import send_to_peer_id


def _display_identity(peer_id, name):
    return peer_id if state.show_id_instead_of_name else name


def _clean(text):
    return sanitize_for_display(unescape_field(text))


def _prompt():
    ui_print("> ", end="", flush=True)


def _set_color(peer_id, color_idx):
    try:
        set_color_for_id(peer_id, int(color_idx))
    except ValueError:
        pass


def _is_ignored(peer_id):
    with ignore_lock:
        return peer_id in ignored_ids


def handle_group_create(parts, is_direct, ip, signed_line, tag):
    _, creator_id, group_id, group_name, member_csv = parts
    if len(creator_id) != 8 or not (8 <= len(group_id) <= 32):
        return
    if not authentic(creator_id, is_direct, ip, signed_line, tag):
        return
    group_name = _clean(group_name).strip()
    if not group_name or len(group_name) > MAX_GROUP_NAME_LEN:
        return
    members = {m for m in member_csv.split(",") if len(m) == 8}
    if creator_id not in members or not (2 <= len(members) <= MAX_GROUP_MEMBERS):
        return
    # Non-members still relay the packet, they just don't keep the group
    if MY_ID not in members:
        return
    with group_lock:
        existing = group_chats.get(group_id)
        definition = {"name": group_name, "creator_id": creator_id, "members": members}
        if existing is not None:
            # Group definitions are immutable once learned
            if existing != definition:
                return
        else:
            group_chats[group_id] = definition
    if output.is_gui():
        output.emit_event(EVENT_GROUP_CREATE, {
            "group_id": group_id, "name": group_name,
            "creator_id": creator_id, "members": sorted(members),
        })
    else:
        ui_print(f"Joined group '{group_name}' ({group_id}).")


def handle_group_msg(parts, is_direct, ip, signed_line, tag):
    _, group_id, sender_id, color_idx, sender_name, text = parts
    if len(sender_id) != 8 or not (8 <= len(group_id) <= 32):
        return
    if not authentic(sender_id, is_direct, ip, signed_line, tag):
        return
    with group_lock:
        group = group_chats.get(group_id)
        if group is None or MY_ID not in group["members"] or sender_id not in group["members"]:
            return
    sender_name = _clean(sender_name)
    text = _clean(text)
    try:
        color_idx_int = int(color_idx)
        set_color_for_id(sender_id, color_idx_int)
    except ValueError:
        color_idx_int = 0
    with names_lock:
        peer_names[sender_id] = sender_name
    if _is_ignored(sender_id):
        return
    log_message(f"{timestamp()} [group {group['name']}] {sender_name}: {text}")
    if output.is_gui():
        output.emit_event(EVENT_GROUP_MSG, {
            "group_id": group_id, "sender_id": sender_id,
            "sender_name": sender_name, "color_idx": color_idx_int,
            "text": text, "outgoing": False,
        })
    else:
        c = color_for_id(sender_id)
        ui_print(f"{timestamp()} [group {group['name']}] {c}{sender_name}{RESET}: {text}")


def handle_msg(parts, is_direct, ip, signed_line, tag):
    _, peer_id, color_idx, name, rest = parts
    msg_id = ""
    reply_id = ""
    text = rest
    rest_parts = rest.split("|", 2)
    if len(rest_parts) == 3:
        text, msg_id, reply_id = rest_parts

    if not authentic(peer_id, is_direct, ip, signed_line, tag):
        return
    name = _clean(name)
    text = _clean(text)
    msg_id = unescape_field(msg_id)
    reply_id = unescape_field(reply_id)
    _set_color(peer_id, color_idx)
    with names_lock:
        peer_names[peer_id] = name
    is_ignored = _is_ignored(peer_id)

    if msg_id:
        remember_message(msg_id, name, text)

    reply_tag = ""
    pinged_by_reply = False
    if reply_id:
        replied = get_remembered_message(reply_id)
        if replied:
            reply_tag = f" (reply to {replied[0]} [{reply_id}])"
            pinged_by_reply = replied[0] == state.my_name

    id_tag = f"[{msg_id}]" if msg_id else ""
    log_message(f"{timestamp()} {id_tag}{reply_tag} {name}: {text}")
    if is_ignored:
        return

    c = color_for_id(peer_id)
    pinged = pinged_by_reply or mentions_me(text)
    shown_name = _display_identity(peer_id, name)
    display = f"{timestamp()} {id_tag}{reply_tag} {c}{shown_name}{RESET}: {text}"

    if pinged:
        if output.is_gui():
            ui_print(f"\a<<PING>>{display}<<PING>>")
        else:
            ui_print(f"\a\033[7m{display}\033[27m")
    else:
        ui_print(display)
    _prompt()


def handle_priv(parts, is_direct, ip, signed_line, tag):
    _, target_id, sender_id, color_idx, rest = parts
    if not authentic_for_me(target_id, sender_id, is_direct, ip, signed_line, tag):
        return
    if "|" not in rest:
        return
    name, text = rest.split("|", 1)
    name = _clean(name)
    text = _clean(text)
    _set_color(sender_id, color_idx)
    with names_lock:
        peer_names[sender_id] = name
    log_message(f"{timestamp()} [private] {name}: {text}")
    if _is_ignored(sender_id):
        return
    if output.is_gui():
        output.emit_event(EVENT_DM, {
            "peer_id": sender_id,
            "dm_key": stable_peer_ip(sender_id, ip if is_direct else None),
            "name": name, "text": text,
            "color_idx": get_color_index(sender_id, 0), "outgoing": False,
        })
    else:
        c = color_for_id(sender_id)
        shown_name = _display_identity(sender_id, name)
        ui_print(f"{timestamp()} [private] {c}{shown_name}{RESET}: {text}")
        _prompt()


def handle_color(parts, is_direct, ip, signed_line, tag):
    _, peer_id, color_idx = parts
    if not authentic(peer_id, is_direct, ip, signed_line, tag):
        return
    _set_color(peer_id, color_idx)


def handle_name(parts, is_direct, ip, signed_line, tag):
    _, peer_id, name = parts
    if not authentic(peer_id, is_direct, ip, signed_line, tag):
        return
    name = _clean(name)
    if is_direct:
        save_peer_identity(ip, peer_id, name)
    with names_lock:
        is_new = peer_id not in peer_names
        duplicate = any(pid != peer_id and other == name for pid, other in peer_names.items())
        peer_names[peer_id] = name
    # Colours stay keyed by peer id, never by display name, so same-named peers don't share one
    if is_new and name != peer_id:
        c = color_for_id(peer_id)
        ui_print(f"\n{c}{name}{RESET} has joined ({peer_id})")
        if duplicate:
            ui_print(
                f"Note: '{name}' is already in use by another connected "
                f"peer; use the id ({peer_id}) with /msg, /ping, etc. to "
                f"be sure you reach the right one."
            )
        _prompt()


def handle_ping(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, sent_time = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    send_to_peer_id(f"PONG|{peer_id}|{MY_ID}|{sent_time}")


def handle_pong(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, sent_time = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    try:
        sent = float(sent_time)
    except ValueError:
        return
    rtt_ms = (time.time() - sent) * 1000
    ui_print(f"\nPong from {name_of(peer_id)}: {rtt_ms:.1f} ms")
    _prompt()
    with ping_lock:
        pending_pings.pop(peer_id, None)


# packet type -> (number of fields, handler)
CHAT_HANDLERS = {
    "GROUP_CREATE": (5, handle_group_create),
    "GROUP_MSG": (6, handle_group_msg),
    "MSG": (5, handle_msg),
    "PRIV": (5, handle_priv),
    "COLOR": (3, handle_color),
    "NAME": (3, handle_name),
    "PING": (4, handle_ping),
    "PONG": (4, handle_pong),
}

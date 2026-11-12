import time

from dechat.core import output, state
from dechat.core.colors import COLORS, RESET, color_index_for_id
from dechat.core.config import MAX_MSG_HISTORY
from dechat.core.identity import MY_ID
from dechat.core.output import EVENT_DM, ui_print
from dechat.core.state import lock, msg_history, msg_history_lock, names_lock, peer_names, peers, ping_lock, pending_pings
from dechat.core.storage import log_message
from dechat.core.utils import escape_field, new_msg_id, timestamp
from dechat.features.peers import resolve_peer_id, stable_peer_ip
from dechat.net.relay import send_to_peer_id, wrap_relay
from dechat.net.wire import send_to_all


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
    return f"@{state.my_name.lower()}" in text.lower()


def send_chat_message(message, reply_id=""):
    my_color = color_index_for_id(MY_ID)
    msg_id = new_msg_id()
    remember_message(msg_id, state.my_name, message)

    reply_tag = ""
    if reply_id:
        replied = get_remembered_message(reply_id)
        if replied:
            reply_tag = f" (reply to {replied[0]} [{reply_id}])"

    id_tag = f"[{msg_id}]"
    display_line = f"{id_tag}{reply_tag} {COLORS[my_color]}{state.my_name}{RESET}: {message}"

    if output.is_gui():
        ui_print(f"{timestamp()} {display_line}")
    else:
        # Overwrite the line the user just typed
        print(f"\033[1A\033[2K{timestamp()} {display_line}")
    log_message(f"{timestamp()} [{msg_id}]{reply_tag} {state.my_name}: {message}")
    with lock:
        no_peers = len(peers) == 0
    if no_peers:
        ui_print("(No peers connected yet - message saved locally but not delivered to anyone.)")
    inner = (
        f"MSG|{MY_ID}|{my_color}|{escape_field(state.my_name)}|{escape_field(message)}"
        f"|{escape_field(msg_id)}|{escape_field(reply_id)}"
    )
    send_to_all(wrap_relay(inner))


def send_private_message(target, text):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        return False

    my_color = color_index_for_id(MY_ID)
    with names_lock:
        resolved_name = peer_names.get(peer_id, target)
    log_message(f"{timestamp()} [private to {resolved_name}] {state.my_name}: {text}")

    ok = send_to_peer_id(
        f"PRIV|{peer_id}|{MY_ID}|{my_color}|{escape_field(state.my_name)}|{escape_field(text)}"
    )
    if not ok:
        ui_print("Failed to send private message (no peers connected).")
        return False
    if output.is_gui():
        output.emit_event(EVENT_DM, {
            "peer_id": peer_id, "dm_key": stable_peer_ip(peer_id), "name": resolved_name,
            "text": text, "color_idx": my_color, "outgoing": True,
        })
    else:
        ui_print(f"{timestamp()} [private to {resolved_name}] {COLORS[my_color]}{state.my_name}{RESET}: {text}")
    return True


def send_ping(target):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        return

    sent_time = time.time()
    with ping_lock:
        pending_pings[peer_id] = sent_time

    if send_to_peer_id(f"PING|{peer_id}|{MY_ID}|{sent_time}"):
        ui_print(f"Pinging {target}...")
    else:
        with ping_lock:
            pending_pings.pop(peer_id, None)
        ui_print("Failed to send ping (no peers connected).")

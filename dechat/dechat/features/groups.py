import uuid

from dechat.core import output, state
from dechat.core.colors import COLORS, RESET, color_index_for_id
from dechat.core.config import MAX_GROUP_MEMBERS, MAX_GROUP_NAME_LEN
from dechat.core.identity import MY_ID
from dechat.core.crypto import encrypt_text
from dechat.core.output import EVENT_GROUP_CREATE, EVENT_GROUP_MSG, ui_print
from dechat.core.state import group_chats, group_lock, lock, names_lock, peer_names, peers
from dechat.core.storage import log_message
from dechat.core.utils import escape_field, timestamp
from dechat.net.relay import wrap_relay
from dechat.net.wire import send_to_all


def create_group_chat(name, member_ids):
    name = name.strip()
    members = set(member_ids)
    members.add(MY_ID)
    if not name or len(name) > MAX_GROUP_NAME_LEN:
        ui_print(f"Group name must be 1-{MAX_GROUP_NAME_LEN} characters.")
        return None
    if not (2 <= len(members) <= MAX_GROUP_MEMBERS):
        ui_print(f"A group must contain 2-{MAX_GROUP_MEMBERS} members including you.")
        return None
    with names_lock:
        known_ids = set(peer_names) | {MY_ID}
    if not members.issubset(known_ids):
        ui_print("Could not create group because one or more selected peers are no longer known.")
        return None

    group_id = uuid.uuid4().hex[:12]
    with group_lock:
        group_chats[group_id] = {"name": name, "creator_id": MY_ID, "members": members}
    member_csv = ",".join(sorted(members))
    send_to_all(wrap_relay(
        f"GROUP_CREATE|{MY_ID}|{group_id}|{escape_field(name)}|{member_csv}"
    ))
    if output.is_gui():
        output.emit_event(EVENT_GROUP_CREATE, {
            "group_id": group_id, "name": name, "creator_id": MY_ID,
            "members": sorted(members),
        })
    return group_id


def send_group_message(group_id, text):
    with group_lock:
        group = group_chats.get(group_id)
        if group is None or MY_ID not in group["members"]:
            ui_print("That group is no longer available.")
            return False
        group_name = group["name"]
    my_color = color_index_for_id(MY_ID)
    inner = (
        f"GROUP_MSG|{group_id}|{MY_ID}|{my_color}|"
        f"{escape_field(state.my_name)}|{escape_field(encrypt_text(text))}"
    )
    with lock:
        has_peers = bool(peers)
    if not has_peers:
        ui_print("Failed to send group message (no peers connected).")
        return False
    send_to_all(wrap_relay(inner))
    log_message(f"{timestamp()} [group {group_name}] {state.my_name}: {text}")
    if output.is_gui():
        output.emit_event(EVENT_GROUP_MSG, {
            "group_id": group_id, "sender_id": MY_ID,
            "sender_name": state.my_name, "color_idx": my_color,
            "text": text, "outgoing": True,
        })
    else:
        ui_print(f"{timestamp()} [group {group_name}] {COLORS[my_color]}{state.my_name}{RESET}: {text}")
    return True

import shlex

from dechat.core import output, state
from dechat.core.config import CURRENT_VERSION, PROTOCOL_VERSION
from dechat.core.identity import MY_ID
from dechat.core.output import ui_print
from dechat.core.state import group_chats, group_lock, names_lock, peer_names
from dechat.core.updates import fetch_update_status
from dechat.features.groups import create_group_chat, send_group_message
from dechat.features.messages import send_chat_message, send_private_message
from dechat.features.peers import name_of, resolve_peer_id
from dechat.frontends.commands import process_command
from dechat.net.lifecycle import shutdown_networking, start_networking

GROUP_USAGE = 'Usage: /groupcreate "group name" <member1> [member2 ...]'


def print_update_lock():
    print("=" * 60)
    print("Different versions have different chat protocols.")
    print("Your version will not work unless you update")
    print("=" * 60)
    print("Please update to the latest version.")


def print_outdated_notice():
    print("=" * 60)
    print("You are not on the latest version of dechat.")
    print("The app will still work, but you may want to update when you can.")
    print("=" * 60)
    input("Press Enter to continue... ")


class Conversation:
    """Where plain text goes: the public chat, one DM, or one group."""

    def __init__(self):
        self.mode = "chat"
        self.target = None

    def switch(self, mode, target=None):
        self.mode = mode
        self.target = target

    def prompt(self):
        if self.mode == "dm" and self.target:
            return f"DM @{name_of(self.target)}> "
        if self.mode == "group" and self.target:
            with group_lock:
                group = group_chats.get(self.target)
                name = group["name"] if group else self.target
            return f"#{name}> "
        return "> "


def _member_groups():
    with group_lock:
        return {
            gid: {"name": group["name"], "members": set(group["members"])}
            for gid, group in group_chats.items()
            if MY_ID in group.get("members", set())
        }


def _resolve_group(target):
    target = target.strip()
    if not target:
        return None
    groups = _member_groups()
    if target in groups:
        return target
    folded = target.casefold()
    matches = [gid for gid, group in groups.items() if group["name"].casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ui_print(f"More than one group is named '{target}'. Use the group id from /groups.")
    else:
        ui_print(f"No group named or identified as '{target}'. Use /groups to list groups.")
    return None


def _show_groups():
    groups = _member_groups()
    if not groups:
        ui_print("You are not in any group chats.")
        return
    with names_lock:
        names = dict(peer_names)
    ui_print("Groups:")
    for gid, group in sorted(groups.items(), key=lambda item: item[1]["name"].casefold()):
        members = [state.my_name if pid == MY_ID else names.get(pid, pid) for pid in sorted(group["members"])]
        ui_print(f"  {group['name']} [{gid}]  members: {', '.join(members)}")


def _cmd_dm(conv, message):
    target = message[len("/dm"):].strip()
    if not target:
        ui_print("Usage: /dm <name|id>")
        return
    peer_id = resolve_peer_id(target)
    if peer_id:
        conv.switch("dm", peer_id)
        ui_print(f"DM with {name_of(peer_id)}. Type /chat to return to public chat.")


def _cmd_group(conv, message):
    target = message[len("/group"):].strip()
    if not target:
        ui_print("Usage: /group <name|group_id>")
        return
    group_id = _resolve_group(target)
    if group_id:
        conv.switch("group", group_id)
        with group_lock:
            group_name = group_chats[group_id]["name"]
        ui_print(f"Group chat '{group_name}'. Type /chat to return to public chat.")


def _cmd_groupcreate(conv, message):
    try:
        args = shlex.split(message[len("/groupcreate"):].strip())
    except ValueError as exc:
        ui_print(f"Could not parse group command: {exc}")
        ui_print(GROUP_USAGE)
        return
    if len(args) < 2:
        ui_print(GROUP_USAGE)
        return
    group_name, raw_members = args[0], args[1:]
    member_ids = []
    for raw_member in raw_members:
        peer_id = resolve_peer_id(raw_member)
        if not peer_id:
            return
        if peer_id != MY_ID and peer_id not in member_ids:
            member_ids.append(peer_id)
    if not member_ids:
        ui_print("Select at least one other member.")
        return
    group_id = create_group_chat(group_name, member_ids)
    if group_id:
        conv.switch("group", group_id)
        ui_print(f"Created group '{group_name}' [{group_id}] and switched to it.")


def _send_in_conversation(conv, message):
    if conv.mode == "dm" and conv.target:
        send_private_message(conv.target, message)
    elif conv.mode == "group" and conv.target:
        if not send_group_message(conv.target, message):
            with group_lock:
                still_exists = conv.target in group_chats
            if not still_exists:
                conv.switch("chat")
    else:
        send_chat_message(message)


def process_tui_input(conv, message):
    """Returns False when the user quits."""
    if message == "/chat":
        conv.switch("chat")
        ui_print("Switched to public chat.")
    elif message == "/dm" or message.startswith("/dm "):
        _cmd_dm(conv, message)
    elif message == "/groups":
        _show_groups()
    elif message == "/group" or message.startswith("/group "):
        _cmd_group(conv, message)
    elif message == "/groupcreate" or message.startswith("/groupcreate "):
        _cmd_groupcreate(conv, message)
    elif message.startswith("/"):
        return process_command(message)
    elif message:
        _send_in_conversation(conv, message)
    return True


def run_tui():
    output.set_mode("tui")

    status = fetch_update_status()
    if status is not None:
        protocol = status.get("protocolVersion")
        if protocol is not None and protocol != PROTOCOL_VERSION:
            print_update_lock()
            return

        latest = status.get("latestVersion")
        if latest is not None and latest != CURRENT_VERSION:
            print_outdated_notice()

    if not start_networking():
        return

    ui_print('TUI conversations: /dm <name|id>, /groups, /group <name|id>, /groupcreate "name" <member...>, /chat')

    conv = Conversation()
    while True:
        try:
            try:
                message = input(conv.prompt())
            except EOFError:
                message = "/quit"

            if not process_tui_input(conv, message):
                break
        except KeyboardInterrupt:
            break

    shutdown_networking()

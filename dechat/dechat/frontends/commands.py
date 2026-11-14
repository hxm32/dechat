import random
import threading

from dechat.core import output, state
from dechat.core.colors import (
    COLORS,
    RESET,
    claim_color_for_id,
    get_color_index,
    remember_color_for_name,
)
from dechat.core.config import MAX_MSG_HISTORY
from dechat.core.identity import MY_ID, save_name
from dechat.core.output import ui_print
from dechat.core.utils import escape_field
from dechat.features.file_offers import accept_offer, list_pending_offers, reject_offer
from dechat.features.file_send import send_file
from dechat.features.messages import (
    get_remembered_message,
    list_recent_messages,
    send_chat_message,
    send_ping,
    send_private_message,
)
from dechat.features.peers import ignore_peer, list_ignored, list_peers, unignore_peer
from dechat.net.connections import connect_to_peer
from dechat.net.relay import wrap_relay
from dechat.net.wire import send_to_all

HELP_LINES = (
    "Commands:",
    "  /quit                     exit the chat",
    "  /who                      list connected peers with id, ip, and color",
    "  /connect <ip>             manually connect to a peer by IP",
    "  /recent [count]           show recent messages with their ids (default 10)",
    "  /reply <msg_id> <text>    reply to a specific message (pings its sender)",
    "  Tip: typing @name in a message pings that user",
    "  /name <newname>           change your display name (saved for next time)",
    "  /colors                   list available color numbers",
    "  /color <n>                change your color to number n",
    "  /color random             pick a random color",
    "  /showid                   toggle showing peer IDs instead of names",
    "  /clear                    clear the screen",
    "  /ping <name|id>           measure round-trip latency to a peer",
    "  /sendfile <name|id> <path> offer to send a file to a peer",
    "  /accept <n>               accept a pending file offer by number",
    "  /reject <n>               decline a pending file offer by number",
    "  /offers                   list pending incoming file offers",
    "  /msg <name|id> <text>     send a one-off private message to one peer",
    "  /dm <name|id>             enter a DM conversation; normal text goes there",
    "  /groups                   list group chats you belong to",
    "  /group <name|id>          enter a group conversation; normal text goes there",
    '  /groupcreate "name" <member...> create a fixed-membership group and enter it',
    "  /chat                     return to the public chat",
    "  /ignore <name|id>         stop showing messages from a peer",
    "  /unignore <name|id>       resume showing messages from a peer",
    "  /ignored                  list currently ignored peers",
    "  /help                     show this message",
)


def print_help():
    for line in HELP_LINES:
        ui_print(line)


def print_colors():
    mine = get_color_index(MY_ID)
    ui_print(f"You have {len(COLORS)} colors to choose from (0-{len(COLORS) - 1}).")
    if mine is not None:
        ui_print(f"Your current color: {COLORS[mine]}{mine}{RESET}")
    ui_print("Use /color <n> to change, or /color random for a random one.")


def _set_my_color(chosen):
    claim_color_for_id(MY_ID, chosen)
    remember_color_for_name(state.my_name, chosen)
    send_to_all(wrap_relay(f"COLOR|{MY_ID}|{chosen}"))
    ui_print(f"Color set to {COLORS[chosen]}{chosen}{RESET}")


def _cmd_color(arg):
    if arg == "random":
        current = get_color_index(MY_ID)
        choices = [i for i in range(len(COLORS)) if i != current]
        _set_my_color(random.choice(choices) if choices else 0)
    elif arg.isdigit():
        chosen = int(arg)
        if chosen >= len(COLORS):
            ui_print(f"Pick a number from 0 to {len(COLORS) - 1}.")
        else:
            _set_my_color(chosen)
    else:
        ui_print("Usage: /color <n> or /color random")


def _cmd_showid():
    if output.is_gui():
        ui_print("Use the settings panel to toggle showing IDs instead of names.")
    else:
        state.show_id_instead_of_name = not state.show_id_instead_of_name
        label = "on" if state.show_id_instead_of_name else "off"
        ui_print(f"Show IDs instead of names: {label}")


def _cmd_clear():
    if not output.is_gui():
        print("\033[2J\033[H", end="")


def _cmd_name(raw_name):
    state.my_name = raw_name.strip() or state.my_name
    save_name(state.my_name)
    send_to_all(wrap_relay(f"NAME|{MY_ID}|{escape_field(state.my_name)}"))
    ui_print(f"Name set to: {state.my_name}")


def _cmd_ping(target):
    if target:
        send_ping(target)
    else:
        ui_print("Usage: /ping <name|id>")


def _cmd_sendfile(rest):
    args = rest.split(" ", 1)
    if len(args) == 2:
        send_file(args[0], args[1])
    else:
        ui_print("Usage: /sendfile <name|id> <path>")


def _cmd_msg(rest):
    args = rest.split(" ", 1)
    if len(args) == 2:
        send_private_message(args[0], args[1])
    else:
        ui_print("Usage: /msg <name|id> <text>")


def _cmd_ignore(target):
    if target:
        ignore_peer(target)
    else:
        ui_print("Usage: /ignore <name|id>")


def _cmd_unignore(target):
    if target:
        unignore_peer(target)
    else:
        ui_print("Usage: /unignore <name|id>")


def _cmd_accept(ref):
    if ref:
        accept_offer(ref)
    else:
        ui_print("Usage: /accept <n>")


def _cmd_reject(ref):
    if ref:
        reject_offer(ref)
    else:
        ui_print("Usage: /reject <n>")


def _cmd_connect(target_ip):
    if target_ip:
        ui_print(f"Connecting to {target_ip}...")
        threading.Thread(target=connect_to_peer, args=(target_ip,), daemon=True).start()
    else:
        ui_print("Usage: /connect <ip>")


def _cmd_recent(arg):
    count = 10
    if arg:
        try:
            count = max(1, min(int(arg), MAX_MSG_HISTORY))
        except ValueError:
            ui_print("Usage: /recent [count]")
            return
    list_recent_messages(count)


def _cmd_reply(rest):
    args = rest.split(" ", 1)
    if len(args) == 2 and args[0]:
        reply_id, text = args
        reply_id = reply_id.lstrip("[").rstrip("]")
        if not get_remembered_message(reply_id):
            ui_print(f"No known message with id '{reply_id}'. It may be too old or invalid.")
        else:
            send_chat_message(text, reply_id=reply_id)
    else:
        ui_print("Usage: /reply <msg_id> <text>")


_NO_ARG_COMMANDS = {
    "/who": list_peers,
    "/help": print_help,
    "/colors": print_colors,
    "/showid": _cmd_showid,
    "/clear": _cmd_clear,
    "/ignored": list_ignored,
    "/offers": list_pending_offers,
}

_ARG_COMMANDS = {
    "/name": _cmd_name,
    "/ping": _cmd_ping,
    "/sendfile": _cmd_sendfile,
    "/msg": _cmd_msg,
    "/ignore": _cmd_ignore,
    "/unignore": _cmd_unignore,
    "/accept": _cmd_accept,
    "/reject": _cmd_reject,
    "/connect": _cmd_connect,
    "/reply": _cmd_reply,
}


def process_command(message):
    """Handles one line of input. Returns False if the app should quit."""
    if message == "/quit":
        return False

    if message in _NO_ARG_COMMANDS:
        _NO_ARG_COMMANDS[message]()
    elif message == "/color" or message.startswith("/color "):
        _cmd_color(message[len("/color"):].strip())
    elif message == "/recent" or message.startswith("/recent "):
        _cmd_recent(message[len("/recent"):].strip())
    elif message.startswith("/"):
        command, has_arg, rest = message.partition(" ")
        handler = _ARG_COMMANDS.get(command) if has_arg else None
        if handler:
            handler(rest.strip())
        else:
            ui_print(f"Unknown command: {message}. Type /help for a list of commands.")
    elif message:
        send_chat_message(message)

    return True

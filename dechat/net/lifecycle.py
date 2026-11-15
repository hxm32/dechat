import socket
import threading

from dechat.core import state
from dechat.core.colors import color_for_id, preferred_color_for_name, set_color_for_id
from dechat.core.config import CHAT_PORT, CURRENT_VERSION
from dechat.core.identity import MY_ID
from dechat.core.output import ui_print
from dechat.core.state import lock, peers
from dechat.core.storage import show_recent_history
from dechat.net.connections import accept_connections
from dechat.net.discovery import discovery
from dechat.net.reaper import reaper

BANNER = (
    r"  _____        _____ _           _   ",
    r" |  __ \      / ____| |         | |  ",
    r" | |  | | ___| |    | |__   __ _| |_ ",
    r" | |  | |/ _ \ |    | '_ \ / _` | __|",
    r" | |__| |  __/ |____| | | | (_| | |_ ",
    r" |_____/ \___|\_____|_| |_|\__,_|__|",
)


def start_networking():
    """Binds the chat port, shows the banner and starts the background threads.

    Returns False if the port is already in use.
    """
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("", CHAT_PORT))
    except OSError:
        ui_print(
            f"Could not start dechat: port {CHAT_PORT} is already in use.\n"
            f"Another instance of dechat may already be running on this machine."
        )
        server.close()
        return False
    server.listen()
    state.server = server

    for line in BANNER:
        ui_print(line)
    ui_print(" ")
    ui_print(f"Welcome to DeChat version {CURRENT_VERSION}!")
    ui_print("Your ID:", MY_ID)
    ui_print("Quick start: /who (list peers)  /name <n>  /msg <n> <text>  /sendfile <n> <path>  /help (full list)")
    ui_print("Looking for other users...")

    show_recent_history()

    preferred = preferred_color_for_name(state.my_name)
    if preferred is not None:
        set_color_for_id(MY_ID, preferred)
    else:
        color_for_id(MY_ID)

    threading.Thread(target=accept_connections, args=(server,), daemon=True).start()
    threading.Thread(target=discovery, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    return True


def shutdown_networking():
    state.running = False

    if state.discovery_socket:
        try:
            state.discovery_socket.close()
        except OSError:
            pass

    with lock:
        for peer in peers.values():
            try:
                peer.shutdown(socket.SHUT_RDWR)
                peer.close()
            except OSError:
                pass

    if state.server:
        try:
            state.server.close()
        except OSError:
            pass

    ui_print("\nGoodbye!")

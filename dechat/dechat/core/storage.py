import json
import threading

from dechat.core.config import HISTORY_LINES_TO_SHOW, LOG_FILE, PEER_LOG_FILE
from dechat.core.output import ui_print
from dechat.core.utils import sanitize_for_display

log_lock = threading.Lock()
peer_log_lock = threading.Lock()


def log_message(line):
    with log_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def show_recent_history():
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
    except OSError:
        return
    if not lines:
        return
    recent = lines[-HISTORY_LINES_TO_SHOW:]
    ui_print(f"--- Last {len(recent)} message(s) ---")
    for line in recent:
        ui_print(sanitize_for_display(line.rstrip("\n")))
    ui_print("--- End of history ---")


def load_peer_log():
    try:
        with open(PEER_LOG_FILE, "r") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ip: entry for ip, entry in data.items()
        if isinstance(ip, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("username"), str)
        and entry.get("username")
    }


# ip -> {"peer_id": ..., "username": ...}, the last verified identity seen at each ip
peer_identity_log = load_peer_log()


def save_peer_identity(ip, peer_id, username):
    if not ip or not peer_id or not username:
        return
    with peer_log_lock:
        peer_identity_log[ip] = {"peer_id": peer_id, "username": username}
        snapshot = dict(peer_identity_log)
    try:
        with open(PEER_LOG_FILE, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
    except OSError:
        pass


def cached_peer_name(ip):
    with peer_log_lock:
        entry = peer_identity_log.get(ip)
    return entry.get("username") if entry else None


def remember_peer_session(ip, peer_id):
    """Links the latest session id to an ip, only used to group GUI DMs."""
    if not ip or not peer_id:
        return
    with peer_log_lock:
        entry = peer_identity_log.setdefault(ip, {})
        entry["peer_id"] = peer_id


def ip_for_logged_peer(peer_id):
    with peer_log_lock:
        for ip, entry in peer_identity_log.items():
            if entry.get("peer_id") == peer_id:
                return ip
    return None

import threading

from dechat.core.config import COLOR_FILE

COLORS = [
    "\033[31m", "\033[32m", "\033[33m", "\033[34m",
    "\033[35m", "\033[36m", "\033[91m", "\033[92m",
    "\033[93m", "\033[94m", "\033[95m", "\033[96m",
]
RESET = "\033[0m"

color_lock = threading.Lock()
id_colors = {}
name_color_lock = threading.Lock()


def load_saved_colors():
    result = {}
    try:
        with open(COLOR_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                name, idx = line.split("=", 1)
                try:
                    result[name] = int(idx)
                except ValueError:
                    pass
    except OSError:
        pass
    return result


# Remembers only our own preferred colour per display name
name_to_color = load_saved_colors()


def save_colors():
    with name_color_lock:
        items = list(name_to_color.items())
    try:
        with open(COLOR_FILE, "w") as f:
            for name, idx in items:
                f.write(f"{name}={idx}\n")
    except OSError:
        pass


def remember_color_for_name(name, idx):
    with name_color_lock:
        name_to_color[name] = idx
    save_colors()


def preferred_color_for_name(name):
    with name_color_lock:
        return name_to_color.get(name)


def color_for_id(peer_id):
    with color_lock:
        if peer_id not in id_colors:
            used = set(id_colors.values())
            for idx in range(len(COLORS)):
                if idx not in used:
                    id_colors[peer_id] = idx
                    break
            else:
                id_colors[peer_id] = sum(ord(c) for c in peer_id) % len(COLORS)
        return COLORS[id_colors[peer_id]]


def set_color_for_id(peer_id, idx):
    if not isinstance(idx, int) or idx < 0 or idx >= len(COLORS):
        return
    with color_lock:
        id_colors[peer_id] = idx


def color_index_for_id(peer_id):
    color_for_id(peer_id)
    with color_lock:
        return id_colors[peer_id]


def claim_color_for_id(peer_id, chosen):
    """Colours are display preferences, so duplicates are allowed."""
    if not isinstance(chosen, int) or not (0 <= chosen < len(COLORS)):
        return False
    with color_lock:
        id_colors[peer_id] = chosen
    return True


def get_color_index(peer_id, default=None):
    with color_lock:
        return id_colors.get(peer_id, default)


def forget_color(peer_id):
    with color_lock:
        id_colors.pop(peer_id, None)


def colors_snapshot():
    with color_lock:
        return dict(id_colors)

import time
import tkinter as tk

from dechat.core import state
from dechat.core.colors import colors_snapshot, get_color_index
from dechat.core.config import ROUTE_STALE_AFTER
from dechat.core.identity import MY_ID
from dechat.core.state import ip_to_id, lock, names_lock, next_hop_for_peer, peer_names, peers, route_lock
from dechat.features.groups import create_group_chat
from dechat.frontends.gui.themes import ANSI_TAG_COLORS
from dechat.net.discovery import get_local_ip


def reachable_people():
    """Returns (peer_id, name, color_idx, ip_or_route) rows, starting with ourselves."""
    rows = [(MY_ID, state.my_name, get_color_index(MY_ID, 0), get_local_ip() or "local")]
    with lock:
        direct_ips = list(peers.keys())
    direct_by_id = {}
    with names_lock:
        for ip in direct_ips:
            pid = ip_to_id.get(ip)
            if pid:
                direct_by_id[pid] = ip
        names_snapshot = dict(peer_names)
    with route_lock:
        route_snapshot = dict(next_hop_for_peer)
    now = time.time()
    reachable = set(direct_by_id)
    reachable.update(pid for pid, (_, learned) in route_snapshot.items() if now - learned <= ROUTE_STALE_AFTER)
    reachable.discard(MY_ID)
    colors = colors_snapshot()
    for pid in sorted(reachable, key=lambda x: names_snapshot.get(x, x).lower()):
        rows.append((pid, names_snapshot.get(pid, "(unknown)"), colors.get(pid, 0), direct_by_id.get(pid, "via mesh")))
    return rows


def show_people_window(root, theme, small_font, small_bold, open_dm):
    win = tk.Toplevel(root)
    win.title("People")
    win.configure(bg=theme["bg"])
    win.geometry("620x360")
    win.minsize(520, 260)
    heading = tk.Frame(win, bg=theme["bg"])
    heading.pack(fill="x", padx=12, pady=(12, 6))
    for text, width in (("Name", 18), ("Colour", 8), ("ID", 12), ("IP / route", 18)):
        tk.Label(heading, text=text, width=width, anchor="w", bg=theme["bg"], fg=theme["dim"], font=small_bold).pack(side="left")
    body = tk.Frame(win, bg=theme["bg"])
    body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    swatch_colors = list(ANSI_TAG_COLORS.values())
    for pid, name, color_idx, ip_text in reachable_people():
        row = tk.Frame(body, bg=theme["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=name, width=18, anchor="w", bg=theme["bg"], fg=theme["fg"], font=small_font).pack(side="left")
        tk.Label(
            row, text=str(color_idx), width=8, bg=swatch_colors[color_idx % len(swatch_colors)],
            fg="#000000", font=small_font,
        ).pack(side="left")
        tk.Label(row, text=pid, width=12, anchor="w", bg=theme["bg"], fg=theme["fg"], font=small_font).pack(side="left")
        tk.Label(row, text=ip_text, width=18, anchor="w", bg=theme["bg"], fg=theme["fg"], font=small_font).pack(side="left")
        if pid != MY_ID:
            tk.Button(
                row, text="DM",
                command=lambda p=pid, n=name, i=ip_text, w=win: (w.destroy(), open_dm(p, n, i if i != "via mesh" else None)),
                bg=theme["panel"], fg=theme["accent"], activebackground=theme["dim"],
                activeforeground=theme["bg"], relief="flat", font=small_font,
            ).pack(side="right")


def open_new_group_dialog(root, theme, small_font, small_bold, on_created, on_error):
    """Asks for a name and members, then calls on_created(group_id)."""
    rows = [r for r in reachable_people() if r[0] != MY_ID]
    if not rows:
        on_error("No reachable peers are available for a group.\n")
        return

    win = tk.Toplevel(root)
    win.title("New group")
    win.configure(bg=theme["bg"])
    win.geometry("420x480")
    tk.Label(win, text="Group name", bg=theme["bg"], fg=theme["dim"], font=small_font, anchor="w").pack(fill="x", padx=14, pady=(14, 4))
    name_field = tk.Entry(win, bg=theme["bg"], fg=theme["fg"], insertbackground=theme["fg"], font=small_font)
    name_field.pack(fill="x", padx=14, ipady=5)
    tk.Label(win, text="Members", bg=theme["bg"], fg=theme["dim"], font=small_font, anchor="w").pack(fill="x", padx=14, pady=(14, 4))

    choices = []
    list_frame = tk.Frame(win, bg=theme["panel"])
    list_frame.pack(fill="both", expand=True, padx=14)
    for pid, pname, _, _ in rows:
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            list_frame, text=f"{pname} ({pid})", variable=var, bg=theme["panel"], fg=theme["fg"],
            activebackground=theme["panel"], activeforeground=theme["fg"], selectcolor=theme["panel"],
            font=small_font, anchor="w",
        ).pack(fill="x", padx=8, pady=3)
        choices.append((pid, var))

    status = tk.StringVar(value="")
    tk.Label(win, textvariable=status, bg=theme["bg"], fg=theme["error"], font=small_font, anchor="w").pack(fill="x", padx=14, pady=(8, 0))

    def create_now():
        selected = [pid for pid, var in choices if var.get()]
        group_name = name_field.get().strip()
        if not group_name:
            status.set("Enter a group name.")
            return
        if not selected:
            status.set("Select at least one other member.")
            return
        gid = create_group_chat(group_name, selected)
        if gid:
            win.destroy()
            on_created(gid)

    tk.Button(
        win, text="Create group", command=create_now, bg=theme["panel"], fg=theme["accent"],
        activebackground=theme["dim"], activeforeground=theme["bg"], font=small_bold, relief="flat",
    ).pack(fill="x", padx=14, pady=14)

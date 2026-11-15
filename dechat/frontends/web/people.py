import time

from dechat.core import state
from dechat.core.colors import colors_snapshot, get_color_index
from dechat.core.config import ROUTE_STALE_AFTER
from dechat.core.identity import MY_ID
from dechat.core.state import ip_to_id, lock, names_lock, next_hop_for_peer, peer_names, peers, route_lock
from dechat.features.peers import stable_peer_ip
from dechat.net.discovery import get_local_ip


def reachable_people():
    """Returns dicts for everyone reachable (direct or via mesh), starting with ourselves."""
    rows = [{
        "id": MY_ID, "name": state.my_name, "color": get_color_index(MY_ID, 0),
        "ip": get_local_ip() or "local", "dm_key": None,
    }]
    with lock:
        direct_ips = list(peers.keys())
    direct_by_id = {}
    with names_lock:
        for ip in direct_ips:
            pid = ip_to_id.get(ip)
            if pid:
                direct_by_id[pid] = ip
        names = dict(peer_names)
    with route_lock:
        routes = dict(next_hop_for_peer)
    now = time.time()
    reachable = set(direct_by_id)
    reachable.update(pid for pid, (_, learned) in routes.items() if now - learned <= ROUTE_STALE_AFTER)
    reachable.discard(MY_ID)
    colors = colors_snapshot()
    for pid in sorted(reachable, key=lambda x: names.get(x, x).lower()):
        direct_ip = direct_by_id.get(pid)
        rows.append({
            "id": pid, "name": names.get(pid, "(unknown)"), "color": colors.get(pid, 0),
            "ip": direct_ip or "via mesh",
            # DMs are grouped by ip because peer ids change every session
            "dm_key": direct_ip or stable_peer_ip(pid) or pid,
        })
    return rows

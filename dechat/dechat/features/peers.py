import time

from dechat.core.colors import RESET, color_for_id, get_color_index
from dechat.core.config import ROUTE_STALE_AFTER
from dechat.core.output import ui_print
from dechat.core.state import (
    announced_peer_ids,
    ignore_lock,
    ignored_ids,
    ip_to_id,
    lock,
    names_lock,
    next_hop_for_peer,
    peer_names,
    peers,
    route_lock,
)
from dechat.core.storage import ip_for_logged_peer


def name_of(peer_id):
    with names_lock:
        return peer_names.get(peer_id, peer_id)


def stable_peer_ip(peer_id, direct_ip=None):
    """Best known ip for a peer id, used to group GUI DM history."""
    if direct_ip:
        return direct_ip
    with names_lock:
        for ip, known_id in ip_to_id.items():
            if known_id == peer_id:
                return ip
        for ip, known_id in announced_peer_ids.items():
            if known_id == peer_id:
                return ip
    return ip_for_logged_peer(peer_id)


def list_peers():
    with lock:
        ips = list(peers.keys())

    if not ips:
        ui_print("No peers connected.")
        return

    ui_print("Connected peers:")
    for ip in ips:
        with names_lock:
            peer_id = ip_to_id.get(ip)
            name = peer_names.get(peer_id) if peer_id else None
        if peer_id:
            c = color_for_id(peer_id)
            color_idx = get_color_index(peer_id, "?")
            ui_print(f"  {c}{name or '(unknown)'}{RESET}  id={peer_id}  ip={ip}  color={color_idx}")
        else:
            ui_print(f"  (unidentified)  ip={ip}")


def resolve_peer_id(target):
    """Resolves a typed name or 8-char id to one peer id.

    Prints an error and returns None if nothing matches or the name is ambiguous.
    """
    with names_lock:
        direct_ids = set(ip_to_id.values())
        if len(target) == 8 and (target in peer_names or target in direct_ids):
            return target
        matches = [pid for pid, name in peer_names.items() if name == target]
    if len(target) == 8:
        # A peer reachable only through the mesh has no direct binding but may have a fresh route
        with route_lock:
            route = next_hop_for_peer.get(target)
        if route is not None and time.time() - route[1] <= ROUTE_STALE_AFTER:
            return target

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(matches)
        ui_print(
            f"'{target}' matches more than one connected peer ({ids}). "
            f"Use the exact id instead of the name to be sure you reach "
            f"the right one."
        )
        return None
    ui_print(f"No known peer named or identified as '{target}'.")
    return None


def ignore_peer(target):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        return
    with ignore_lock:
        ignored_ids.add(peer_id)
    ui_print(f"Ignoring messages from {target}.")


def unignore_peer(target):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        # The raw id may belong to a peer that has already been purged from peer_names
        with ignore_lock:
            was_ignored = target in ignored_ids
            ignored_ids.discard(target)
        if was_ignored:
            ui_print(f"No longer ignoring '{target}'.")
        else:
            ui_print(f"'{target}' was not being ignored.")
        return
    with ignore_lock:
        ignored_ids.discard(peer_id)
    ui_print(f"No longer ignoring {target}.")


def list_ignored():
    with ignore_lock:
        ids = list(ignored_ids)
    if not ids:
        ui_print("Not ignoring anyone.")
        return
    ui_print("Ignored peers:")
    for pid in ids:
        ui_print(f"  {name_of(pid)} ({pid})")

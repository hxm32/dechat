import time

from dechat.core import state
from dechat.core.output import ui_print
from dechat.core.state import offer_lock, offer_num_to_id, pending_offers
from dechat.core.utils import human_size
from dechat.core.identity import MY_ID
from dechat.features.peers import name_of
from dechat.net.relay import send_to_peer_id


def register_offer(sender_id, offer_id, filename, filesize, file_hash=None):
    """Stores an incoming offer and returns its /accept number. Caller holds offer_lock."""
    num = state.next_offer_num
    state.next_offer_num += 1
    offer_num_to_id[num] = offer_id
    pending_offers[offer_id] = {
        "sender_id": sender_id,
        "filename": filename,
        "filesize": filesize,
        "file_hash": file_hash,
        "started": time.time(),
        "accepted": None,
        "num": num,
    }
    return num


def resolve_offer(ref):
    """Resolves an offer number or raw offer id to (offer_id, offer)."""
    with offer_lock:
        if ref.isdigit():
            offer_id = offer_num_to_id.get(int(ref))
        else:
            offer_id = ref if ref in pending_offers else None
        if offer_id is None:
            return None, None
        return offer_id, pending_offers.get(offer_id)


def accept_offer(ref):
    offer_id, offer = resolve_offer(ref)
    if not offer:
        ui_print(f"No pending file offer '{ref}'.")
        return
    with offer_lock:
        offer["accepted"] = True
    if not send_to_peer_id(f"FILE_ACCEPT|{offer['sender_id']}|{MY_ID}|{offer_id}"):
        ui_print("No peers connected; can't reach that sender right now.")
        with offer_lock:
            pending_offers.pop(offer_id, None)
        return
    ui_print(f"Accepted '{offer['filename']}'. Receiving...")


def reject_offer(ref):
    offer_id, offer = resolve_offer(ref)
    if not offer:
        ui_print(f"No pending file offer '{ref}'.")
        return
    with offer_lock:
        pending_offers.pop(offer_id, None)
    send_to_peer_id(f"FILE_DECLINE|{offer['sender_id']}|{MY_ID}|{offer_id}")
    ui_print(f"Declined '{offer['filename']}'.")


def list_pending_offers():
    with offer_lock:
        items = sorted(pending_offers.items(), key=lambda kv: kv[1]["num"])
    if not items:
        ui_print("No pending file offers.")
        return
    ui_print("Pending file offers:")
    for offer_id, offer in items:
        name = name_of(offer["sender_id"])
        ui_print(f"  [{offer['num']}] '{offer['filename']}' ({human_size(offer['filesize'])}) from {name}")

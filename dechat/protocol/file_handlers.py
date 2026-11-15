import threading

from dechat.core.output import ui_print
from dechat.core.state import offer_lock, pending_offers, pending_sends, pending_sends_lock
from dechat.core.config import MAX_PENDING_OFFERS
from dechat.core.utils import human_size, sanitize_for_display, unescape_field
from dechat.features.file_offers import register_offer
from dechat.features.file_receive import process_file_chunk
from dechat.features.file_send import record_chunk_ack, stream_file
from dechat.features.peers import name_of
from dechat.net.auth import authentic_for_me


def _prompt():
    ui_print("> ", end="", flush=True)


def handle_file_offer(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id, filename, filesize, file_hash = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    filename = sanitize_for_display(unescape_field(filename))
    try:
        filesize = int(filesize)
    except ValueError:
        return
    # A malformed hash just means the file can't be verified
    if len(file_hash) != 64 or any(c not in "0123456789abcdef" for c in file_hash.lower()):
        file_hash = None

    with offer_lock:
        if len(pending_offers) >= MAX_PENDING_OFFERS:
            return
        num = register_offer(peer_id, offer_id, filename, filesize, file_hash)

    ui_print(f"\n{name_of(peer_id)} wants to send you a file: '{filename}' ({human_size(filesize)})")
    ui_print(f"Type /accept {num} to receive it, or /reject {num} to decline.")
    _prompt()


def handle_file_decline(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    with pending_sends_lock:
        pending_sends.pop(offer_id, None)
    ui_print(f"\n{name_of(peer_id)} declined your file offer.")
    _prompt()


def handle_file_accept(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    with pending_sends_lock:
        filepath = pending_sends.pop(offer_id, None)
    if filepath:
        threading.Thread(target=stream_file, args=(peer_id, offer_id, filepath), daemon=True).start()


def handle_file_chunk(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id, chunk_info, hex_data = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return

    with offer_lock:
        offer = pending_offers.get(offer_id)
    # Only take data for an offer this peer sent and we accepted
    if not offer or offer["sender_id"] != peer_id or offer.get("accepted") is not True:
        return

    process_file_chunk(peer_id, offer_id, offer, chunk_info, hex_data)


def handle_file_ack(parts, is_direct, ip, signed_line, tag):
    _, target_id, peer_id, offer_id, chunk_index = parts
    if not authentic_for_me(target_id, peer_id, is_direct, ip, signed_line, tag):
        return
    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return
    record_chunk_ack(offer_id, chunk_index)


FILE_HANDLERS = {
    "FILE_OFFER": (7, handle_file_offer),
    "FILE_DECLINE": (4, handle_file_decline),
    "FILE_ACCEPT": (4, handle_file_accept),
    "FILE": (6, handle_file_chunk),
    "FILE_ACK": (5, handle_file_ack),
}

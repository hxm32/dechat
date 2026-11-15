import hashlib
import hmac
import os
import time

from dechat.core import state
from dechat.core.config import (
    MAX_CONCURRENT_TRANSFERS,
    MAX_FILE_CHUNKS,
    MAX_TOTAL_INCOMING_BYTES,
)
from dechat.core.identity import MY_ID
from dechat.core.output import ui_print
from dechat.core.state import incoming_transfers, offer_lock, pending_offers, transfer_lock
from dechat.core.utils import sanitize_for_display
from dechat.features.peers import name_of
from dechat.net.routing import send_routed_to_peer_id


def get_downloads_dir():
    candidate = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        os.makedirs(candidate, exist_ok=True)
        return candidate
    except OSError:
        return os.getcwd()


def process_file_chunk(peer_id, offer_id, offer, chunk_info, hex_data):
    """Buffers one chunk of an accepted offer, acks it, and saves the file once complete."""
    try:
        chunk_index, total_chunks = chunk_info.split("/")
        chunk_index = int(chunk_index)
        total_chunks = int(total_chunks)
        chunk_bytes = bytes.fromhex(hex_data)
    except ValueError:
        return

    if total_chunks <= 0 or not (0 <= chunk_index < total_chunks):
        return
    if total_chunks > MAX_FILE_CHUNKS:
        return

    complete_entry = None
    with transfer_lock:
        entry = incoming_transfers.get(offer_id)
        if entry is None:
            if len(incoming_transfers) >= MAX_CONCURRENT_TRANSFERS:
                return
            # Total is pinned to what the first chunk declared
            entry = {"chunks": {}, "started": time.time(), "total": total_chunks, "bytes": 0}
            incoming_transfers[offer_id] = entry
        if total_chunks != entry["total"] or chunk_index >= entry["total"]:
            return
        if chunk_index not in entry["chunks"]:
            if state.incoming_bytes_buffered + len(chunk_bytes) > MAX_TOTAL_INCOMING_BYTES:
                return
            state.incoming_bytes_buffered += len(chunk_bytes)
            entry["bytes"] += len(chunk_bytes)
        entry["chunks"][chunk_index] = chunk_bytes
        entry["started"] = time.time()
        if len(entry["chunks"]) == entry["total"]:
            complete_entry = incoming_transfers.pop(offer_id)
            state.incoming_bytes_buffered -= complete_entry["bytes"]

    send_routed_to_peer_id(peer_id, f"FILE_ACK|{peer_id}|{MY_ID}|{offer_id}|{chunk_index}")

    if complete_entry is not None:
        _finish_transfer(peer_id, offer_id, offer, complete_entry)


def _finish_transfer(peer_id, offer_id, offer, entry):
    filename = offer["filename"]
    data = b"".join(entry["chunks"].get(i, b"") for i in range(entry["total"]))
    name = name_of(peer_id)

    expected_hash = offer.get("file_hash")
    if expected_hash and not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_hash):
        with offer_lock:
            pending_offers.pop(offer_id, None)
        ui_print(
            f"\nReceived '{sanitize_for_display(filename)}' from {name}, "
            f"but it FAILED the integrity check (hash mismatch) -- "
            f"discarding it rather than saving a possibly-corrupted "
            f"or tampered file. Ask them to resend."
        )
        ui_print("> ", end="", flush=True)
        return

    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "received_file"
    downloads_dir = get_downloads_dir()
    out_name = safe_name
    counter = 1
    while True:
        try:
            out_path = os.path.join(downloads_dir, out_name)
            with open(out_path, "xb") as f:
                f.write(data)
            break
        except FileExistsError:
            out_name = f"{counter}_{safe_name}"
            counter += 1

    with offer_lock:
        pending_offers.pop(offer_id, None)
    verified_note = " (integrity verified)" if expected_hash else ""
    ui_print(f"\nReceived file '{sanitize_for_display(filename)}' from {name}, saved to {out_path}{verified_note}")
    ui_print("> ", end="", flush=True)

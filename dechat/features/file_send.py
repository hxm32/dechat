import hashlib
import os
import threading
import time
import uuid

from dechat.core.config import (
    ACK_WAIT_SECONDS,
    FILE_CHUNK_SIZE,
    MAX_FILE_CHUNKS,
    MAX_RETRIES_PER_CHUNK,
    SEND_WINDOW_SIZE,
)
from dechat.core.identity import MY_ID
from dechat.core.output import ui_print
from dechat.core.state import outgoing_transfers, outgoing_transfers_lock, pending_sends, pending_sends_lock
from dechat.core.utils import escape_field, human_size, safe_display_filename
from dechat.features.peers import resolve_peer_id
from dechat.net.relay import send_to_peer_id
from dechat.net.routing import send_routed_to_peer_id


def _prompt():
    ui_print("> ", end="", flush=True)


def hash_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def record_chunk_ack(offer_id, chunk_index):
    """Marks a chunk as acknowledged and wakes the sender. Unknown offers are ignored."""
    with outgoing_transfers_lock:
        entry = outgoing_transfers.get(offer_id)
        if entry is None:
            return
        entry["acked"].add(chunk_index)
        entry["event"].set()


def send_file(target, filepath):
    peer_id = resolve_peer_id(target)
    if not peer_id:
        return

    try:
        file_size = os.path.getsize(filepath)
        file_hash = hash_file(filepath)
    except OSError as e:
        ui_print(f"Could not read file: {e}")
        return

    filename = safe_display_filename(filepath)
    offer_id = uuid.uuid4().hex[:8]

    with pending_sends_lock:
        pending_sends[offer_id] = filepath

    ok = send_to_peer_id(
        f"FILE_OFFER|{peer_id}|{MY_ID}|{offer_id}|{escape_field(filename)}|{file_size}|{file_hash}"
    )
    if ok:
        ui_print(f"Offered '{filename}' ({human_size(file_size)}) to {target}. Waiting for them to accept...")
    else:
        with pending_sends_lock:
            pending_sends.pop(offer_id, None)
        ui_print("Failed to send file offer (no peers connected).")


def stream_file(peer_id, offer_id, filepath):
    """Sends an accepted file in acknowledged chunks, resending any that get lost."""
    try:
        file_size = os.path.getsize(filepath)
    except OSError as e:
        ui_print(f"\nCould not read file: {e}")
        _prompt()
        return

    total = max(1, (file_size + FILE_CHUNK_SIZE - 1) // FILE_CHUNK_SIZE)
    filename = safe_display_filename(filepath)

    if total > MAX_FILE_CHUNKS:
        ui_print(
            f"\n'{filename}' is too large to send ({human_size(file_size)}); "
            f"the limit is {human_size(MAX_FILE_CHUNKS * FILE_CHUNK_SIZE)}."
        )
        _prompt()
        return

    try:
        with open(filepath, "rb") as f:
            chunks = [f.read(FILE_CHUNK_SIZE) for _ in range(total)]
    except OSError as e:
        ui_print(f"\nCould not read file: {e}")
        _prompt()
        return

    event = threading.Event()
    with outgoing_transfers_lock:
        outgoing_transfers[offer_id] = {"acked": set(), "event": event}

    try:
        _send_chunks(peer_id, offer_id, filename, chunks, total, event)
    except Exception as e:
        ui_print(f"\nSending '{filename}' failed unexpectedly: {e}")
        _prompt()
    finally:
        with outgoing_transfers_lock:
            outgoing_transfers.pop(offer_id, None)


def _send_chunks(peer_id, offer_id, filename, chunks, total, event):
    """Sliding-window send loop. Each chunk is resent until acked or out of retries."""
    retries = [0] * total
    next_to_send = 0
    in_flight = {}  # chunk_index -> time sent

    def send_chunk(idx):
        ok = send_routed_to_peer_id(
            peer_id, f"FILE|{peer_id}|{MY_ID}|{offer_id}|{idx}/{total}|{chunks[idx].hex()}"
        )
        in_flight[idx] = time.time()
        return ok

    def fail_no_peers():
        ui_print(f"\nFailed to send '{filename}' (no peers connected).")
        _prompt()

    while True:
        with outgoing_transfers_lock:
            acked = outgoing_transfers[offer_id]["acked"]
            acked_count = len(acked)

        if acked_count >= total:
            break

        while next_to_send < total and len(in_flight) < SEND_WINDOW_SIZE:
            if next_to_send not in acked:
                if not send_chunk(next_to_send):
                    fail_no_peers()
                    return
            next_to_send += 1

        # Short poll so each chunk's own age is checked, not the whole window at once
        event.wait(timeout=0.2)
        event.clear()

        with outgoing_transfers_lock:
            acked = set(outgoing_transfers[offer_id]["acked"])

        now = time.time()
        for idx in [i for i in in_flight if i in acked]:
            in_flight.pop(idx, None)

        expired = [idx for idx, sent_at in in_flight.items() if now - sent_at >= ACK_WAIT_SECONDS]
        for idx in expired:
            in_flight.pop(idx, None)
            retries[idx] += 1
            if retries[idx] > MAX_RETRIES_PER_CHUNK:
                ui_print(
                    f"\nGiving up sending '{filename}': chunk {idx} was never "
                    f"acknowledged after {MAX_RETRIES_PER_CHUNK} attempts. "
                    f"The peer may have disconnected or be unreachable."
                )
                _prompt()
                return
            if not send_chunk(idx):
                fail_no_peers()
                return

    ui_print(f"\nSent '{filename}' ({total} chunk(s)), all acknowledged.")
    _prompt()

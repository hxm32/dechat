import time

from dechat.core import state
from dechat.core.config import (
    OFFER_TIMEOUT,
    PING_TIMEOUT,
    ROUTE_STALE_AFTER,
    SEEN_ID_MAX_AGE,
    TRANSFER_TIMEOUT,
)
from dechat.core.state import (
    incoming_transfers,
    next_hop_for_peer,
    offer_lock,
    pending_offers,
    pending_pings,
    ping_lock,
    route_lock,
    seen_lock,
    seen_msg_ids,
    transfer_lock,
)


def reaper():
    """Periodically clears abandoned transfers, pings, offers, routes and dedup ids."""
    while state.running:
        time.sleep(10)
        now = time.time()

        with transfer_lock:
            stale_keys = [
                key for key, entry in incoming_transfers.items()
                if now - entry["started"] > TRANSFER_TIMEOUT
            ]
            for key in stale_keys:
                stale_entry = incoming_transfers.pop(key, None)
                if stale_entry is not None:
                    state.incoming_bytes_buffered -= stale_entry.get("bytes", 0)

        with ping_lock:
            for pid in [p for p, sent in pending_pings.items() if now - sent > PING_TIMEOUT]:
                pending_pings.pop(pid, None)

        with offer_lock:
            for oid in [o for o, offer in pending_offers.items() if now - offer["started"] > OFFER_TIMEOUT]:
                pending_offers.pop(oid, None)

        with seen_lock:
            for pid in [p for p, seen_at in seen_msg_ids.items() if now - seen_at > SEEN_ID_MAX_AGE]:
                seen_msg_ids.pop(pid, None)

        with route_lock:
            stale_routes = [
                pid for pid, (_, learned_at) in next_hop_for_peer.items()
                if now - learned_at > ROUTE_STALE_AFTER
            ]
            for pid in stale_routes:
                next_hop_for_peer.pop(pid, None)

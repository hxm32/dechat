import time

from dechat.core.config import (
    FILE_RATE_LIMIT_MAX_TOKENS,
    FILE_RATE_LIMIT_REFILL_PER_SEC,
    RATE_LIMIT_MAX_TOKENS,
    RATE_LIMIT_REFILL_PER_SEC,
)
from dechat.core.state import file_rate_buckets, file_rate_lock, rate_buckets, rate_lock


def _limited(bucket_lock, buckets, ip, max_tokens, refill_per_sec):
    now = time.time()
    with bucket_lock:
        tokens, last = buckets.get(ip, (max_tokens, now))
        tokens = min(max_tokens, tokens + (now - last) * refill_per_sec)
        if tokens < 1:
            buckets[ip] = (tokens, now)
            return True
        buckets[ip] = (tokens - 1, now)
        return False


def rate_limited(ip):
    return _limited(rate_lock, rate_buckets, ip, RATE_LIMIT_MAX_TOKENS, RATE_LIMIT_REFILL_PER_SEC)


def file_rate_limited(ip):
    """Separate, larger bucket so file chunk streams aren't throttled as chat."""
    return _limited(
        file_rate_lock, file_rate_buckets, ip,
        FILE_RATE_LIMIT_MAX_TOKENS, FILE_RATE_LIMIT_REFILL_PER_SEC,
    )

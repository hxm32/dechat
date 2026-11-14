import json
import urllib.error
import urllib.request

from dechat.core.config import UPDATE_CHECK_TIMEOUT, UPDATE_CHECK_URL


def fetch_update_status():
    """Returns the status API's dict, or None on any failure."""
    try:
        req = urllib.request.Request(UPDATE_CHECK_URL, headers={"User-Agent": "dechat"})
        with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

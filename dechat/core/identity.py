import hashlib
import hmac
import secrets
import uuid

from dechat.core.config import NAME_FILE

MY_ID = str(uuid.uuid4())[:8]

# Per-run secret used to sign every packet we originate. Peers learn it
# from a direct KEY packet and then verify all packets claiming our id.
MY_SESSION_KEY = secrets.token_bytes(32)


def make_tag(key, line):
    return hmac.new(key, line.encode(), hashlib.sha256).hexdigest()[:16]


def sign_packet(inner_line):
    return make_tag(MY_SESSION_KEY, inner_line)


def load_saved_name():
    try:
        with open(NAME_FILE, "r") as f:
            saved = f.read().strip()
            if saved:
                return saved
    except OSError:
        pass
    return MY_ID


def save_name(name):
    try:
        with open(NAME_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass

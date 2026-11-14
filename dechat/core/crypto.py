import base64
import hashlib
import hmac
import secrets
import time

from dechat.core.config import ENCRYPTION_KEY, ENCRYPTION_TOLERANCE, ENCRYPTION_WINDOW

NONCE_LEN = 8
MAC_LEN = 16

# Slow hash done once; per-window keys are cheap HMACs of it
_BASE_KEY = hashlib.pbkdf2_hmac("sha256", ENCRYPTION_KEY.encode(), b"dechat-salt", 100_000)


def _window(now=None):
    return int((time.time() if now is None else now) // ENCRYPTION_WINDOW)


def _window_key(window):
    return hmac.new(_BASE_KEY, b"window|" + str(window).encode(), hashlib.sha256).digest()


def _keystream(key, nonce, length):
    """HMAC-SHA256 in counter mode."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _mac(key, nonce, ciphertext):
    return hmac.new(key, b"mac|" + nonce + ciphertext, hashlib.sha256).digest()[:MAC_LEN]


def encrypt_text(text):
    """Encrypts with the key for the current time window. Returns a '|'-free token."""
    key = _window_key(_window())
    nonce = secrets.token_bytes(NONCE_LEN)
    data = text.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    return base64.urlsafe_b64encode(nonce + ct + _mac(key, nonce, ct)).decode()


def decrypt_text(token):
    """Returns the plaintext, or None if the token is invalid or outside the time window."""
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except (ValueError, UnicodeEncodeError):
        return None
    if len(raw) < NONCE_LEN + MAC_LEN:
        return None
    nonce, ct, mac = raw[:NONCE_LEN], raw[NONCE_LEN:-MAC_LEN], raw[-MAC_LEN:]
    current = _window()
    # Tolerance absorbs clock drift and relay delay
    for window in range(current - ENCRYPTION_TOLERANCE, current + ENCRYPTION_TOLERANCE + 1):
        key = _window_key(window)
        if hmac.compare_digest(_mac(key, nonce, ct), mac):
            data = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
            try:
                return data.decode()
            except UnicodeDecodeError:
                return None
    return None

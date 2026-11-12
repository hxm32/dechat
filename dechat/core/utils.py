import os
import time
import uuid

_CONTROL_CHARS = {i: None for i in [*range(0x20), 0x7F, *range(0x80, 0xA0)]}


def timestamp():
    return time.strftime("%H:%M")


def new_packet_id():
    return uuid.uuid4().hex[:12]


def new_msg_id():
    return uuid.uuid4().hex[:4]


def sanitize_for_display(text):
    """Strips control characters from untrusted peer text."""
    if text is None:
        return text
    return text.translate(_CONTROL_CHARS)


def escape_field(text):
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\p")
        .replace("\n", "\\n")
    )


def unescape_field(text):
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "p":
                out.append("|")
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def human_size(num_bytes):
    size = float(num_bytes)
    units = ("B", "KB", "MB", "GB")
    for i, unit in enumerate(units):
        if size < 1024 or i == len(units) - 1:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def safe_display_filename(filepath):
    """Filename only, handling both Windows and POSIX separators."""
    return os.path.basename(filepath.replace("\\", "/")) or "file"

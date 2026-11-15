"""Turns core output lines into escaped HTML plus a kind used for styling."""
import html
import re

from dechat.core import state
from dechat.core.colors import COLORS
from dechat.core.utils import timestamp
from dechat.frontends.web.markdown import render_markdown

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_COLOR_CODES = {"31", "32", "33", "34", "35", "36", "91", "92", "93", "94", "95", "96"}
COLOR_CLASSES = [ANSI_RE.search(c).group(1) for c in COLORS]

_TS_RE = re.compile(r"^\d\d:\d\d ")
_MSG_PREFIX_RE = re.compile(r"^(?:\d\d:\d\d )?[^:\n]+: ")
_PING_OPEN = "\a<<PING>>"
_PING_CLOSE = "<<PING>>"

PRESENCE_MARKS = ("Connected:", "Disconnected:", "has joined")
SYSTEM_MARKS = ("Pong from", "wants to send you a file", "Type /accept")
ERROR_MARKS = ("Unknown command", "Usage:", "Failed", "Could not")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def ansi_to_html(text):
    """Escapes text and maps the core's colour codes to <span class="aNN">."""
    out = []
    color = None
    pos = 0

    def emit(chunk):
        if chunk:
            escaped = html.escape(chunk, quote=False)
            out.append(f'<span class="a{color}">{escaped}</span>' if color else escaped)

    for match in ANSI_RE.finditer(text):
        emit(text[pos:match.start()])
        code = match.group(1)
        if code in ANSI_COLOR_CODES:
            color = code
        elif code in ("", "0"):
            color = None
        pos = match.end()
    emit(text[pos:])
    return "".join(out)


def _take_timestamp(raw):
    """Splits a leading 'HH:MM ' into its own span so the UI can hide it."""
    match = _TS_RE.match(raw)
    if not match:
        return "", raw
    return f'<span class="ts">{match.group()}</span>', raw[match.end():]


def _split_prefix(text):
    """Splits 'name: body' at the end of the visible prefix, colour codes kept."""
    match = _MSG_PREFIX_RE.match(strip_ansi(text))
    if not match:
        return None
    visible = 0
    i = 0
    while i < len(text) and visible < match.end():
        esc = ANSI_RE.match(text, i)
        if esc:
            i = esc.end()
        else:
            i += 1
            visible += 1
    return text[:i], text[i:]


def message_html(raw, prefix_class=None):
    """Renders 'HH:MM name: body' with the body as markdown."""
    ts, raw = _take_timestamp(raw)
    parts = _split_prefix(raw)
    if parts is None:
        return ts + ansi_to_html(raw)
    prefix, body = parts
    prefix_html = ansi_to_html(prefix)
    if prefix_class:
        # Only the name is styled so bold does not leak into the body
        prefix_html = f'<span class="{prefix_class}">{prefix_html}</span>'
    return ts + prefix_html + render_markdown(strip_ansi(body))


def render_line(raw):
    """Returns (kind, html) for one line of core output."""
    plain = strip_ansi(raw)
    if raw.startswith(_PING_OPEN) and raw.endswith(_PING_CLOSE):
        return "ping", message_html(raw[len(_PING_OPEN):-len(_PING_CLOSE)])
    if plain.startswith("*") and plain.endswith("*"):
        ts, rest = _take_timestamp(plain)
        return "system", ts + html.escape(rest, quote=False)
    if any(mark in plain for mark in PRESENCE_MARKS):
        ts, rest = _take_timestamp(raw)
        return "presence", ts + ansi_to_html(rest)
    if any(mark in plain for mark in SYSTEM_MARKS):
        ts, rest = _take_timestamp(raw)
        return "system", ts + ansi_to_html(rest)
    if plain.startswith(f"{timestamp()} {state.my_name}:") or f" {state.my_name}: " in plain[:40]:
        return "me", message_html(raw, "me")
    if any(mark in plain for mark in ERROR_MARKS):
        ts, rest = _take_timestamp(plain)
        return "error", ts + html.escape(rest, quote=False)
    if plain == "":
        return "blank", ""
    return "msg", message_html(raw)


def transcript_html(outgoing, sender_name, text, color_idx, ts):
    """One DM or group message: 'HH:MM name: body'."""
    name = html.escape(sender_name, quote=False)
    if outgoing:
        prefix = f'<span class="me">{name}</span>'
    elif isinstance(color_idx, int) and 0 <= color_idx < len(COLOR_CLASSES):
        prefix = f'<span class="a{COLOR_CLASSES[color_idx]}">{name}</span>'
    else:
        prefix = name
    return f'<span class="ts">{ts} </span>{prefix}: {render_markdown(text)}'

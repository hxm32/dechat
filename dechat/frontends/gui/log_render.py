"""Renders core output lines and transcripts into Tk Text widgets."""
import re

from dechat.core import state
from dechat.core.colors import COLORS
from dechat.core.utils import timestamp
from dechat.frontends.gui.markdown import render_markdown_into
from dechat.frontends.gui.themes import ANSI_TO_TAG

ANSI_RE = re.compile(r"(\x1b\[[0-9;]*m)")
_MSG_PREFIX_RE = re.compile(r"^(?:\d\d:\d\d )?[^:\n]+: ")
_RESET = "\033[0m"


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def append_log(widget, text, tag=None):
    widget.configure(state="normal")
    if tag:
        widget.insert("end", text, tag)
    else:
        widget.insert("end", text)
    widget.configure(state="disabled")
    widget.see("end")


def _insert_ansi(widget, text, extra_tag=None):
    """Inserts text, turning ANSI colour codes into Tk tags. Widget must be editable."""
    current_tag = None
    for part in ANSI_RE.split(text):
        if not part:
            continue
        if part in ANSI_TO_TAG:
            current_tag = ANSI_TO_TAG[part]
            continue
        if part == _RESET:
            current_tag = None
            continue
        widget.insert("end", part, tuple(t for t in (current_tag, extra_tag) if t))


def append_log_ansi(widget, text, extra_tag=None):
    widget.configure(state="normal")
    _insert_ansi(widget, text, extra_tag)
    widget.configure(state="disabled")
    widget.see("end")


def append_log_markdown(widget, text, extra_tag=None):
    """Renders 'HH:MM name: body' lines with the body as markdown.

    Lines without that shape fall back to plain ANSI rendering.
    """
    prefix_match = _MSG_PREFIX_RE.match(strip_ansi(text))
    if not prefix_match:
        append_log_ansi(widget, text, extra_tag)
        return

    # Find where the visible prefix ends in the original, colour codes included
    prefix_len = prefix_match.end()
    visible = 0
    cut = 0
    for part in ANSI_RE.split(text):
        if part in ANSI_TO_TAG or part == _RESET:
            cut += len(part)
            continue
        remaining = prefix_len - visible
        if remaining <= 0:
            break
        if len(part) <= remaining:
            visible += len(part)
            cut += len(part)
        else:
            cut += remaining
            break

    prefix_part, body_part = text[:cut], text[cut:]
    if body_part.endswith("\n"):
        body_part = body_part[:-1]

    append_log_ansi(widget, prefix_part, extra_tag)
    widget.configure(state="normal")
    # extra_tag only styles the name prefix, so bold doesn't leak into the body
    render_markdown_into(widget, body_part)
    widget.configure(state="disabled")
    widget.see("end")


def render_transcript(widget, messages, show_timestamps):
    """Redraws a DM or group transcript from (outgoing, name, text, color_idx) tuples."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    prefix = (timestamp() + " ") if show_timestamps else ""
    for outgoing, sender_name, text, color_idx in messages:
        if outgoing:
            widget.insert("end", f"{prefix}{state.my_name}: ", "me")
        else:
            valid = isinstance(color_idx, int) and 0 <= color_idx < len(COLORS)
            widget.insert("end", f"{prefix}{sender_name}: ", f"peer{color_idx}" if valid else ())
        render_markdown_into(widget, text)
        widget.insert("end", "\n")
    widget.configure(state="disabled")
    widget.see("end")

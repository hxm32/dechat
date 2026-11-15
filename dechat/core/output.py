import json

mode = "tui"
_web_sink = None

# Web mode embeds structured events in the output as tagged JSON lines,
# which the web frontend turns into DM and group updates.
EVENT_DM = "@@DECHAT_DM@@"
EVENT_GROUP_CREATE = "@@DECHAT_GROUP_CREATE@@"
EVENT_GROUP_MSG = "@@DECHAT_GROUP_MSG@@"


def set_mode(new_mode):
    global mode
    mode = new_mode


def is_web():
    return mode == "web"


def set_web_sink(sink):
    """Registers the function that receives all printed text in web mode."""
    global _web_sink
    _web_sink = sink


def ui_print(*args, sep=" ", end="\n", flush=False):
    text = sep.join(str(a) for a in args) + end
    if mode == "web":
        # The "> " prompt redraw is TUI-only
        if text != "> " and _web_sink is not None:
            _web_sink(text)
    else:
        print(text, end="", flush=flush)


def emit_event(tag, data):
    ui_print(tag + json.dumps(data, separators=(",", ":")))

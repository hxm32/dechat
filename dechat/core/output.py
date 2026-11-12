import json
import queue

# GUI mode routes output through this queue so only the Tk main thread
# touches widgets.
ui_output_queue = queue.Queue()
mode = "tui"

EVENT_DM = "@@DECHAT_DM@@"
EVENT_GROUP_CREATE = "@@DECHAT_GROUP_CREATE@@"
EVENT_GROUP_MSG = "@@DECHAT_GROUP_MSG@@"


def set_mode(new_mode):
    global mode
    mode = new_mode


def is_gui():
    return mode == "gui"


def ui_print(*args, sep=" ", end="\n", flush=False):
    text = sep.join(str(a) for a in args) + end
    if mode == "gui":
        # The "> " prompt redraw is TUI-only
        if text == "> ":
            return
        ui_output_queue.put(text)
    else:
        print(text, end="", flush=flush)


def emit_event(tag, data):
    ui_print(tag + json.dumps(data, separators=(",", ":")))

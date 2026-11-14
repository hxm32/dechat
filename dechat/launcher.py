def choose_mode():
    while True:
        try:
            choice = input(
                "Do you want to use TUI (runs in terminal) or GUI (opens an app)? [t/g]: "
            ).strip().lower()
        except EOFError:
            return "t"
        if choice in ("g", "gui"):
            return "g"
        if choice in ("t", "tui", ""):
            return "t"
        print("Please type 't' for TUI or 'g' for GUI.")


def main():
    from dechat.frontends.tui import run_tui

    if choose_mode() == "g":
        try:
            from dechat.frontends.gui.app import run_gui
        except ImportError:
            print("tkinter isn't available on this system, falling back to TUI.")
            run_tui()
        else:
            run_gui()
    else:
        run_tui()

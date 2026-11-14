import tkinter as tk

from dechat.core.config import CURRENT_VERSION


def show_update_lock(root, bg, panel_bg, fg, dim_fg, accent, font_family):
    """Shows a 'please update' window on the existing root. Networking is never started."""
    root.title("dechat - update required")
    root.configure(bg=bg)
    root.geometry("520x360")
    root.minsize(420, 300)
    root.deiconify()

    title_font = (font_family, 20, "bold")
    sub_font = (font_family, 13, "bold")
    body_font = (font_family, 12)

    wrap = tk.Frame(root, bg=bg)
    wrap.pack(fill="both", expand=True, padx=28, pady=28)

    tk.Label(
        wrap, text="Please update to the latest version",
        fg=accent, bg=bg, font=title_font, wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 14))

    tk.Label(
        wrap,
        text=(
            "Different versions have different chat protocols. "
            "Your version will not work unless you update"
        ),
        fg=fg, bg=bg, font=sub_font, wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 18))

    tk.Label(
        wrap, text=f"Your version: {CURRENT_VERSION}",
        fg=dim_fg, bg=bg, font=body_font, justify="left",
    ).pack(anchor="w")

    tk.Button(
        wrap, text="Quit", command=root.destroy,
        bg=panel_bg, fg=accent, activebackground=dim_fg, activeforeground=bg,
        font=sub_font, relief="flat", padx=16, pady=6,
    ).pack(anchor="w", pady=(24, 0))

    root.mainloop()

import json
import queue
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, scrolledtext

from dechat.core import output, state
from dechat.core.colors import COLORS, color_index_for_id, get_color_index, remember_color_for_name
from dechat.core.config import CHAT_PORT, CURRENT_VERSION, PROTOCOL_VERSION
from dechat.core.identity import MY_ID
from dechat.core.output import EVENT_DM, EVENT_GROUP_CREATE, EVENT_GROUP_MSG
from dechat.core.state import group_chats, group_lock, lock, names_lock, peer_names, peers
from dechat.core.updates import fetch_update_status
from dechat.core.utils import timestamp
from dechat.features.groups import send_group_message
from dechat.features.messages import send_private_message
from dechat.features.peers import stable_peer_ip
from dechat.frontends.commands import process_command
from dechat.frontends.gui import log_render, people
from dechat.frontends.gui.lock_screen import show_update_lock
from dechat.frontends.gui.log_render import render_transcript, strip_ansi
from dechat.frontends.gui.markdown import configure_markdown_tags
from dechat.frontends.gui.people import reachable_people
from dechat.frontends.gui.themes import ANSI_TAG_COLORS, GUI_THEMES
from dechat.net.lifecycle import shutdown_networking, start_networking

HELP_COMMANDS = [
    ("/connect <ip>", "manually connect to a peer by IP"),
    ("/recent [count]", "show recent messages with their ids"),
    ("/reply <msg_id> <text>", "reply to a message (pings its sender)"),
    ("/name <newname>", "change your display name"),
    ("/ping <name>", "measure round-trip latency"),
    ("/sendfile <name> <path>", "offer to send a file to a peer"),
    ("/accept <n>", "accept a pending file offer"),
    ("/reject <n>", "decline a pending file offer"),
    ("/offers", "list pending incoming file offers"),
    ("/ignore <name>", "stop showing a peer's messages"),
    ("/unignore <name>", "resume showing a peer's messages"),
    ("/clear", "clear the chat window"),
    ("/quit", "exit dechat"),
]


def run_gui():
    output.set_mode("gui")

    THEMES = GUI_THEMES
    current_theme = "classic"
    _initial_theme = THEMES[current_theme]
    BG = _initial_theme["bg"]
    PANEL_BG = _initial_theme["panel"]
    FG = _initial_theme["fg"]
    DIM_FG = _initial_theme["dim"]
    ACCENT = _initial_theme["accent"]

    root = tk.Tk()
    root.withdraw()  # hidden until we know which screen (lock vs chat) to show

    _mono_candidates = [
        "Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "Cascadia Mono",
        "Courier New", "Courier",
    ]
    _available = set(tkfont.families(root))
    FONT_FAMILY = next((f for f in _mono_candidates if f in _available), "Courier")

    update_status = fetch_update_status()
    protocol_version = update_status.get("protocolVersion") if update_status else None
    if protocol_version is not None and protocol_version != PROTOCOL_VERSION:
        show_update_lock(root, BG, PANEL_BG, FG, DIM_FG, ACCENT, FONT_FAMILY)
        return

    latest_version = update_status.get("latestVersion") if update_status else None
    is_outdated = latest_version is not None and latest_version != CURRENT_VERSION

    root.title(f"dechat* @{state.my_name}")
    root.configure(bg=BG)
    root.geometry("640x760")
    root.minsize(480, 420)
    root.deiconify()

    gui_settings = {
        "sound_on_ping": True,
        "show_timestamps": True,
        "show_presence": True,
        "show_id": False,
    }

    FONT = (FONT_FAMILY, 12)
    MONO_BOLD = (FONT_FAMILY, 12, "bold")
    TITLE_FONT = (FONT_FAMILY, 18, "bold")
    NAME_FONT = (FONT_FAMILY, 18)
    SMALL_BOLD = (FONT_FAMILY, 14, "bold")
    SMALL_FONT = (FONT_FAMILY, 12)

    header = tk.Frame(root, bg=BG)
    header.pack(side="top", fill="x", padx=14, pady=(12, 6))

    title_label = tk.Label(
        header, text="dechat", fg=ACCENT, bg=BG, font=TITLE_FONT
    )
    title_label.pack(side="left")

    name_label = tk.Label(
        header, text=f" @{state.my_name}", fg=FG, bg=BG, font=NAME_FONT
    )
    name_label.pack(side="left")

    peer_count_var = tk.StringVar(value="0 peers")
    people_btn = tk.Button(
        header, textvariable=peer_count_var, bg=BG, fg=FG,
        activebackground=BG, activeforeground=ACCENT, font=SMALL_BOLD,
        relief="flat", padx=6, bd=0, command=lambda: show_people_window(),
    )
    people_btn.pack(side="right")

    news_toggle_btn = tk.Button(
        header, text="news", bg=BG, fg=DIM_FG, activebackground=BG, activeforeground=ACCENT,
        font=SMALL_FONT, relief="flat", padx=6, bd=0,
        command=lambda: toggle_news_panel(),
    )
    news_toggle_btn.pack(side="right", padx=(0, 8))

    settings_toggle_btn = tk.Button(
        header, text="settings", bg=BG, fg=DIM_FG, activebackground=BG, activeforeground=ACCENT,
        font=SMALL_FONT, relief="flat", padx=6, bd=0,
    )
    settings_toggle_btn.pack(side="right", padx=(0, 14))

    divider = tk.Frame(root, bg=DIM_FG, height=1)
    divider.pack(side="top", fill="x", padx=0)

    # --- dismissable "not on latest version" notice ---
    if is_outdated:
        outdated_bar = tk.Frame(root, bg=PANEL_BG)
        outdated_bar.pack(side="top", fill="x")

        outdated_inner = tk.Frame(outdated_bar, bg=PANEL_BG)
        outdated_inner.pack(fill="x", padx=14, pady=8)

        tk.Label(
            outdated_inner,
            text="You're not on the latest version of dechat. The app will still work as normal.",
            fg=FG, bg=PANEL_BG, font=SMALL_FONT, anchor="w", justify="left",
        ).pack(side="left", fill="x", expand=True)

        tk.Button(
            outdated_inner, text="Dismiss", command=lambda: outdated_bar.pack_forget(),
            bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
            font=SMALL_FONT, relief="flat", padx=10, pady=2,
        ).pack(side="right", padx=(10, 0))

    # --- input bar, styled like bitchat ---
    input_bar = tk.Frame(root, bg=BG)
    input_bar.pack(side="bottom", fill="x", padx=10, pady=(0, 12))

    # --- command helper panel, styled like bitchat's command list ---
    help_visible = tk.BooleanVar(value=False)
    help_frame = tk.Frame(root, bg=PANEL_BG, highlightbackground=DIM_FG, highlightthickness=1)

    # --- collapsible news panel ---
    NEWS_PANEL_WIDTH = 240
    news_visible = tk.BooleanVar(value=False)
    news_frame = tk.Frame(
        root, bg=PANEL_BG, width=NEWS_PANEL_WIDTH,
        highlightbackground=DIM_FG, highlightthickness=1,
    )
    news_frame.pack_propagate(False)

    news_inner = tk.Frame(news_frame, bg=PANEL_BG)
    news_inner.pack(fill="both", expand=True, padx=14, pady=14)

    news_heading_label = tk.Label(
        news_inner, text="WHAT'S NEW", fg=DIM_FG, bg=PANEL_BG,
        font=SMALL_FONT, anchor="w",
    )
    news_heading_label.pack(fill="x", pady=(0, 8))

    recent_news = (update_status or {}).get("recentNews") or "No news right now."
    news_details = (update_status or {}).get("news_details") or ""

    recent_news_label = tk.Label(
        news_inner, text=recent_news, fg=ACCENT, bg=PANEL_BG,
        font=MONO_BOLD, anchor="w", justify="left", wraplength=NEWS_PANEL_WIDTH - 28,
    )
    recent_news_label.pack(fill="x", pady=(0, 8))

    news_details_label = None
    if news_details:
        news_details_label = tk.Label(
            news_inner, text=news_details, fg=FG, bg=PANEL_BG,
            font=SMALL_FONT, anchor="w", justify="left", wraplength=NEWS_PANEL_WIDTH - 28,
        )
        news_details_label.pack(fill="x")

    # --- settings panel ---
    SETTINGS_PANEL_WIDTH = 270
    settings_visible = tk.BooleanVar(value=False)
    settings_frame = tk.Frame(
        root, bg=PANEL_BG, width=SETTINGS_PANEL_WIDTH,
        highlightbackground=DIM_FG, highlightthickness=1,
    )
    settings_frame.pack_propagate(False)
    settings_inner = tk.Frame(settings_frame, bg=PANEL_BG)
    settings_inner.pack(fill="both", expand=True, padx=14, pady=14)

    settings_title_label = tk.Label(
        settings_inner, text="SETTINGS", fg=ACCENT, bg=PANEL_BG,
        font=MONO_BOLD, anchor="w",
    )
    settings_title_label.pack(fill="x", pady=(0, 14))

    settings_labels = []
    settings_controls = []

    def setting_label(text):
        label = tk.Label(settings_inner, text=text, fg=DIM_FG, bg=PANEL_BG, font=SMALL_FONT, anchor="w")
        label.pack(fill="x", pady=(8, 3))
        settings_labels.append(label)
        return label

    setting_label("Theme")
    theme_var = tk.StringVar(value=current_theme)
    theme_menu = tk.OptionMenu(settings_inner, theme_var, *THEMES.keys())
    theme_menu.configure(
        bg=PANEL_BG, fg=FG, activebackground=DIM_FG, activeforeground=BG,
        highlightbackground=DIM_FG, font=SMALL_FONT, relief="flat", bd=0,
    )
    theme_menu["menu"].configure(bg=PANEL_BG, fg=FG, font=SMALL_FONT)
    theme_menu.pack(fill="x")
    settings_controls.append(theme_menu)

    setting_label("Chat colour")
    color_index_for_id(MY_ID)
    _starting_color = get_color_index(MY_ID, 0)
    color_var = tk.StringVar(value=str(_starting_color))
    color_menu = tk.OptionMenu(settings_inner, color_var, *[str(i) for i in range(len(COLORS))])
    color_menu.configure(
        bg=PANEL_BG, fg=FG, activebackground=DIM_FG, activeforeground=BG,
        highlightbackground=DIM_FG, font=SMALL_FONT, relief="flat", bd=0,
    )
    color_menu["menu"].configure(bg=PANEL_BG, fg=FG, font=SMALL_FONT)
    color_menu.pack(fill="x")
    settings_controls.append(color_menu)

    setting_label("Display name")
    name_var = tk.StringVar(value=state.my_name)
    name_entry = tk.Entry(
        settings_inner, textvariable=name_var, bg=BG, fg=FG, insertbackground=FG,
        font=SMALL_FONT, relief="flat", highlightthickness=1,
        highlightbackground=DIM_FG, highlightcolor=ACCENT,
    )
    name_entry.pack(fill="x", ipady=5)
    settings_controls.append(name_entry)

    sound_var = tk.BooleanVar(value=gui_settings["sound_on_ping"])
    timestamps_var = tk.BooleanVar(value=gui_settings["show_timestamps"])
    presence_var = tk.BooleanVar(value=gui_settings["show_presence"])
    show_id_var = tk.BooleanVar(value=gui_settings["show_id"])

    for text, var in (
        ("Ping Sound Effect?", sound_var),
        ("Timestamps?", timestamps_var),
        ("Connection Notices?", presence_var),
        ("Show IDs Instead of Names?", show_id_var),
    ):
        cb = tk.Checkbutton(
            settings_inner, text=text, variable=var, bg=PANEL_BG, fg=FG,
            activebackground=PANEL_BG, activeforeground=FG, selectcolor=PANEL_BG,
            font=SMALL_FONT, anchor="w",
        )
        cb.pack(fill="x", pady=(8, 0))
        settings_controls.append(cb)

    settings_status = tk.StringVar(value="")
    settings_status_label = tk.Label(
        settings_inner, textvariable=settings_status, fg=DIM_FG, bg=PANEL_BG,
        font=SMALL_FONT, anchor="w", justify="left", wraplength=SETTINGS_PANEL_WIDTH - 28,
    )
    settings_status_label.pack(fill="x", pady=(10, 0))

    def toggle_news_panel():
        if news_visible.get():
            news_frame.pack_forget()
            news_visible.set(False)
            news_toggle_btn.config(fg=THEMES[current_theme]["dim"])
        else:
            if settings_visible.get():
                settings_frame.pack_forget()
                settings_visible.set(False)
                settings_toggle_btn.config(fg=THEMES[current_theme]["dim"])
            news_frame.pack(side="right", fill="y", padx=(0, 10), pady=8, before=chat_frame)
            news_visible.set(True)
            news_toggle_btn.config(fg=THEMES[current_theme]["accent"])

    # --- conversation area ---
    chat_frame = tk.Frame(root, bg=BG)
    chat_frame.pack(side="top", fill="both", expand=True, padx=10, pady=8)

    active_tab = tk.StringVar(value="chat")
    tab_bar = tk.Frame(chat_frame, bg=BG)
    tab_bar.pack(side="top", fill="x", pady=(0, 6))
    tab_buttons = {}
    for _key, _label in (("chat", "Chat"), ("dms", "DMs"), ("groups", "Groups")):
        _btn = tk.Button(
            tab_bar, text=_label, bg=BG, fg=DIM_FG, activebackground=BG,
            activeforeground=ACCENT, font=SMALL_FONT, relief="flat", bd=0, padx=8,
            command=lambda k=_key: switch_conversation_tab(k),
        )
        _btn.pack(side="left", padx=(0, 4))
        tab_buttons[_key] = _btn

    # --- scrolling public chat log ---
    chat_log = scrolledtext.ScrolledText(
        chat_frame,
        bg=BG,
        fg=FG,
        insertbackground=FG,
        font=FONT,
        wrap="word",
        borderwidth=0,
        highlightthickness=0,
        state="disabled",
    )
    chat_log.pack(fill="both", expand=True)
    chat_log.tag_configure("dim", foreground=DIM_FG)
    chat_log.tag_configure("system", foreground=DIM_FG, font=FONT + ("italic",))
    chat_log.tag_configure("error", foreground=_initial_theme["error"])
    chat_log.tag_configure("me", font=MONO_BOLD)
    chat_log.tag_configure("pinged", background=_initial_theme["ping_bg"], font=MONO_BOLD)
    configure_markdown_tags(chat_log, FONT_FAMILY, FONT[1], _initial_theme)

    for _tag, _color in ANSI_TAG_COLORS.items():
        chat_log.tag_configure(_tag, foreground=_color)

    # --- DM and group panes ---
    dm_frame = tk.Frame(chat_frame, bg=BG)
    dm_sidebar = tk.Frame(dm_frame, bg=PANEL_BG, width=190)
    dm_sidebar.pack(side="left", fill="y", padx=(0, 8))
    dm_sidebar.pack_propagate(False)

    dm_online_label = tk.Label(
        dm_sidebar, text="ONLINE", bg=PANEL_BG, fg=DIM_FG,
        font=SMALL_BOLD, anchor="w",
    )
    dm_online_label.pack(fill="x", padx=8, pady=(7, 2))
    online_dm_list = tk.Listbox(
        dm_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False, height=7,
    )
    online_dm_list.pack(fill="x", padx=6, pady=(0, 6))

    dm_recent_label = tk.Label(
        dm_sidebar, text="RECENT DMS", bg=PANEL_BG, fg=DIM_FG,
        font=SMALL_BOLD, anchor="w",
    )
    dm_recent_label.pack(fill="x", padx=8, pady=(2, 2))
    dm_list = tk.Listbox(
        dm_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False,
    )
    dm_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))
    dm_log = scrolledtext.ScrolledText(
        dm_frame, bg=BG, fg=FG, insertbackground=FG, font=FONT, wrap="word",
        borderwidth=0, highlightthickness=0, state="disabled",
    )
    dm_log.pack(side="left", fill="both", expand=True)

    group_frame = tk.Frame(chat_frame, bg=BG)
    group_sidebar = tk.Frame(group_frame, bg=PANEL_BG, width=170)
    group_sidebar.pack(side="left", fill="y", padx=(0, 8))
    group_sidebar.pack_propagate(False)
    new_group_btn = tk.Button(
        group_sidebar, text="+ New group", bg=PANEL_BG, fg=ACCENT,
        activebackground=DIM_FG, activeforeground=BG, font=SMALL_FONT,
        relief="flat", command=lambda: open_new_group_dialog(),
    )
    new_group_btn.pack(fill="x", padx=6, pady=(6, 2))
    group_list = tk.Listbox(
        group_sidebar, bg=PANEL_BG, fg=FG, selectbackground=DIM_FG,
        selectforeground=FG, font=SMALL_FONT, relief="flat", borderwidth=0,
        highlightthickness=0, exportselection=False,
    )
    group_list.pack(fill="both", expand=True, padx=6, pady=(2, 6))
    group_log = scrolledtext.ScrolledText(
        group_frame, bg=BG, fg=FG, insertbackground=FG, font=FONT, wrap="word",
        borderwidth=0, highlightthickness=0, state="disabled",
    )
    group_log.pack(side="left", fill="both", expand=True)

    for _text_widget in (dm_log, group_log):
        _text_widget.tag_configure("system", foreground=DIM_FG, font=FONT + ("italic",))
        _text_widget.tag_configure("me", font=MONO_BOLD)
        for _i, (_tag, _color) in enumerate(ANSI_TAG_COLORS.items()):
            _text_widget.tag_configure(f"peer{_i}", foreground=_color)
        configure_markdown_tags(_text_widget, FONT_FAMILY, FONT[1], _initial_theme)

    dm_conversations = {}   # ip -> {peer_id, name, messages[(outgoing,name,text,color_idx)]}
    dm_order = []
    online_dm_order = []    # [(peer_id, display_name, ip)] currently reachable
    selected_dm = {"key": None}
    group_messages = {}     # group_id -> messages[(outgoing,name,text,color_idx)]
    group_order = []
    selected_group = {"group_id": None}

    def _render_transcript(widget, messages):
        render_transcript(widget, messages, gui_settings["show_timestamps"])

    def _refresh_online_dm_list():
        rows = [(pid, name, ip) for pid, name, _, ip in reachable_people() if pid != MY_ID]
        online_dm_order[:] = rows
        online_dm_list.delete(0, "end")
        for pid, name, _ in rows:
            online_dm_list.insert("end", f"● {name}")

    def _refresh_dm_list():
        dm_list.delete(0, "end")
        with names_lock:
            names_snapshot = dict(peer_names)
        for dm_key in dm_order:
            conversation = dm_conversations[dm_key]
            peer_id = conversation["peer_id"]
            if peer_id in names_snapshot:
                conversation["name"] = names_snapshot[peer_id]
            dm_list.insert("end", conversation["name"])
        if selected_dm["key"] in dm_order:
            idx = dm_order.index(selected_dm["key"])
            dm_list.selection_set(idx)
            dm_list.see(idx)

    def open_dm(peer_id, name=None, ip=None):
        if peer_id == MY_ID:
            return
        dm_key = ip or stable_peer_ip(peer_id) or peer_id
        if dm_key not in dm_conversations:
            with names_lock:
                resolved = name or peer_names.get(peer_id, peer_id)
            dm_conversations[dm_key] = {"peer_id": peer_id, "name": resolved, "messages": []}
            dm_order.append(dm_key)
        elif name:
            dm_conversations[dm_key]["name"] = name
        dm_conversations[dm_key]["peer_id"] = peer_id
        selected_dm["key"] = dm_key
        _refresh_dm_list()
        _render_transcript(dm_log, dm_conversations[dm_key]["messages"])
        switch_conversation_tab("dms")

    def _on_dm_select(event=None):
        sel = dm_list.curselection()
        if not sel:
            return
        dm_key = dm_order[sel[0]]
        selected_dm["key"] = dm_key
        _render_transcript(dm_log, dm_conversations[dm_key]["messages"])
        if active_tab.get() == "dms":
            prompt_var.set(f"DM @{dm_conversations[dm_key]['name']}>")

    def _on_online_dm_select(event=None):
        sel = online_dm_list.curselection()
        if not sel or sel[0] >= len(online_dm_order):
            return
        pid, name, ip = online_dm_order[sel[0]]
        online_dm_list.selection_clear(0, "end")
        open_dm(pid, name, ip)

    dm_list.bind("<<ListboxSelect>>", _on_dm_select)
    online_dm_list.bind("<<ListboxSelect>>", _on_online_dm_select)

    def handle_dm_event(data):
        pid = data.get("peer_id")
        if not isinstance(pid, str) or len(pid) != 8:
            return
        name = str(data.get("name") or pid)
        dm_key = data.get("dm_key") or stable_peer_ip(pid) or pid
        if dm_key not in dm_conversations:
            dm_conversations[dm_key] = {"peer_id": pid, "name": name, "messages": []}
            dm_order.append(dm_key)
        else:
            dm_conversations[dm_key]["name"] = name
            dm_conversations[dm_key]["peer_id"] = pid
        dm_conversations[dm_key]["messages"].append((
            bool(data.get("outgoing")), state.my_name if data.get("outgoing") else name,
            str(data.get("text", "")), int(data.get("color_idx", 0)),
        ))
        _refresh_dm_list()
        if selected_dm["key"] == dm_key:
            _render_transcript(dm_log, dm_conversations[dm_key]["messages"])

    def _refresh_group_list():
        group_list.delete(0, "end")
        with group_lock:
            snapshot = {gid: group_chats.get(gid) for gid in group_order}
        for gid in group_order:
            group = snapshot.get(gid)
            group_list.insert("end", group["name"] if group else gid)
        if selected_group["group_id"] in group_order:
            idx = group_order.index(selected_group["group_id"])
            group_list.selection_set(idx)
            group_list.see(idx)

    def _on_group_select(event=None):
        sel = group_list.curselection()
        if not sel:
            return
        gid = group_order[sel[0]]
        selected_group["group_id"] = gid
        _render_transcript(group_log, group_messages.get(gid, []))
        if active_tab.get() == "groups":
            with group_lock:
                group_name = group_chats.get(gid, {}).get("name", gid)
            prompt_var.set(f"#{group_name}>")

    group_list.bind("<<ListboxSelect>>", _on_group_select)

    def handle_group_create_event(data):
        gid = data.get("group_id")
        if not isinstance(gid, str):
            return
        if gid not in group_order:
            group_order.append(gid)
            group_messages.setdefault(gid, [])
        _refresh_group_list()
        if selected_group["group_id"] is None:
            selected_group["group_id"] = gid
            _refresh_group_list()
            _render_transcript(group_log, group_messages[gid])

    def handle_group_message_event(data):
        gid = data.get("group_id")
        if not isinstance(gid, str):
            return
        if gid not in group_order:
            group_order.append(gid)
        group_messages.setdefault(gid, []).append((
            bool(data.get("outgoing")), str(data.get("sender_name") or data.get("sender_id") or "unknown"),
            str(data.get("text", "")), int(data.get("color_idx", 0)),
        ))
        _refresh_group_list()
        if selected_group["group_id"] == gid:
            _render_transcript(group_log, group_messages[gid])

    def switch_conversation_tab(tab):
        if tab not in ("chat", "dms", "groups"):
            return
        active_tab.set(tab)
        chat_log.pack_forget()
        dm_frame.pack_forget()
        group_frame.pack_forget()
        if tab == "chat":
            chat_log.pack(fill="both", expand=True)
            prompt_var.set(f"<@{state.my_name}>")
        elif tab == "dms":
            dm_frame.pack(fill="both", expand=True)
            dm_key = selected_dm["key"]
            prompt_var.set(f"DM @{dm_conversations[dm_key]['name']}>" if dm_key in dm_conversations else "DM> ")
        else:
            group_frame.pack(fill="both", expand=True)
            gid = selected_group["group_id"]
            with group_lock:
                gname = group_chats.get(gid, {}).get("name") if gid else None
            prompt_var.set(f"#{gname}>" if gname else "GROUP> ")
        theme = THEMES[current_theme]
        for key, btn in tab_buttons.items():
            btn.configure(fg=theme["accent"] if key == tab else theme["dim"])
        entry.focus_set()

    def show_people_window():
        people.show_people_window(root, THEMES[current_theme], SMALL_FONT, SMALL_BOLD, open_dm)

    def open_new_group_dialog():
        def on_created(gid):
            selected_group["group_id"] = gid
            switch_conversation_tab("groups")

        people.open_new_group_dialog(
            root, THEMES[current_theme], SMALL_FONT, SMALL_BOLD, on_created,
            lambda message: append_log(message, "error"),
        )

    # --- incoming-line rendering ---
    def append_log(text, tag=None):
        log_render.append_log(chat_log, text, tag)

    def append_log_ansi(text, extra_tag=None):
        log_render.append_log_ansi(chat_log, text, extra_tag)

    def append_log_markdown(text, extra_tag=None):
        log_render.append_log_markdown(chat_log, text, extra_tag)

    gui_event_handlers = (
        (EVENT_DM, handle_dm_event),
        (EVENT_GROUP_CREATE, handle_group_create_event),
        (EVENT_GROUP_MSG, handle_group_message_event),
    )

    def classify_and_append(line):
        raw = line.rstrip("\n")
        for event_tag, event_handler in gui_event_handlers:
            if raw.startswith(event_tag):
                try:
                    event_handler(json.loads(raw[len(event_tag):]))
                except (ValueError, TypeError):
                    pass
                return
        stripped = strip_ansi(raw)

        if not gui_settings["show_presence"] and (
            "Connected:" in stripped or "Disconnected:" in stripped or "has joined" in stripped
        ):
            return

        def maybe_hide_timestamp(value):
            if gui_settings["show_timestamps"]:
                return value
            plain = strip_ansi(value)
            if len(plain) >= 6 and plain[2] == ":" and plain[5] == " ":
                return value[6:]
            return value

        if raw.startswith("\a<<PING>>") and raw.endswith("<<PING>>"):
            payload = raw[len("\a<<PING>>"):-len("<<PING>>")]
            if gui_settings["sound_on_ping"]:
                root.bell()
            append_log_ansi(maybe_hide_timestamp(payload) + "\n", "pinged")
        elif stripped.startswith("*") and stripped.endswith("*"):
            append_log(stripped + "\n", "system")
        elif (
            "Connected:" in stripped or "Disconnected:" in stripped or "Pong from" in stripped
            or "has joined" in stripped or "wants to send you a file" in stripped
            or "Type /accept" in stripped
        ):
            append_log_ansi(maybe_hide_timestamp(raw) + "\n", "system")
        elif stripped.startswith(f"{timestamp()} {state.my_name}:") or f" {state.my_name}: " in stripped[:40]:
            append_log_markdown(maybe_hide_timestamp(raw) + "\n", "me")
        elif "Unknown command" in stripped or "Usage:" in stripped or "Failed" in stripped or "Could not" in stripped:
            append_log(maybe_hide_timestamp(stripped) + "\n", "error")
        else:
            append_log_markdown(maybe_hide_timestamp(raw) + "\n")

    for cmd, desc in HELP_COMMANDS:
        row = tk.Frame(help_frame, bg=PANEL_BG)
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(row, text=cmd, fg=ACCENT, bg=PANEL_BG, font=FONT, anchor="w", width=24).pack(side="left")
        tk.Label(row, text=desc, fg=DIM_FG, bg=PANEL_BG, font=FONT, anchor="w").pack(side="left")

    # --- help panel / open-panel-exclusivity (Escape closes whichever panel is open) ---
    def toggle_help():
        if help_visible.get():
            help_frame.pack_forget()
            help_visible.set(False)
            help_btn.config(text="?")
        else:
            help_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 8), before=input_bar)
            help_visible.set(True)
            help_btn.config(text="x")

    def close_open_panel(event=None):
        if help_visible.get():
            toggle_help()
        elif settings_visible.get():
            toggle_settings_panel()
        elif news_visible.get():
            toggle_news_panel()

    root.bind("<Escape>", close_open_panel)

    # --- input entry, prompt, and theme/settings application ---
    prompt_var = tk.StringVar(value=f"<@{state.my_name}>")
    prompt_label = tk.Label(input_bar, textvariable=prompt_var, fg=FG, bg=BG, font=MONO_BOLD)
    prompt_label.pack(side="left", padx=(2, 6))

    entry = tk.Entry(
        input_bar, bg=BG, fg=FG, insertbackground=FG, font=FONT,
        relief="flat", highlightthickness=1, highlightbackground=DIM_FG,
        highlightcolor=ACCENT,
    )
    entry.pack(side="left", fill="x", expand=True, ipady=6)
    entry.focus_set()

    def apply_theme(name):
        """Apply a GUI theme explicitly to every themed GUI surface."""
        nonlocal current_theme

        if name not in THEMES:
            return False
        theme = THEMES[name]
        bg = theme["bg"]
        panel = theme["panel"]
        fg = theme["fg"]
        dim = theme["dim"]
        accent = theme["accent"]

        root.configure(bg=bg)
        header.configure(bg=bg)
        divider.configure(bg=dim)
        input_bar.configure(bg=bg)
        chat_frame.configure(bg=bg)
        help_frame.configure(bg=panel, highlightbackground=dim)
        news_frame.configure(bg=panel, highlightbackground=dim)
        news_inner.configure(bg=panel)
        settings_frame.configure(bg=panel, highlightbackground=dim)
        settings_inner.configure(bg=panel)

        title_label.configure(bg=bg, fg=accent)
        name_label.configure(bg=bg, fg=fg)
        people_btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=accent)
        news_toggle_btn.configure(bg=bg, fg=accent if news_visible.get() else dim, activebackground=bg, activeforeground=accent)
        settings_toggle_btn.configure(bg=bg, fg=accent if settings_visible.get() else dim, activebackground=bg, activeforeground=accent)

        chat_log.configure(bg=bg, fg=fg, insertbackground=fg)
        chat_log.tag_configure("dim", foreground=dim)
        chat_log.tag_configure("system", foreground=dim)
        chat_log.tag_configure("error", foreground=theme["error"])
        chat_log.tag_configure("pinged", background=theme["ping_bg"])
        configure_markdown_tags(chat_log, FONT_FAMILY, FONT[1], theme)

        prompt_label.configure(bg=bg, fg=fg)
        entry.configure(bg=bg, fg=fg, insertbackground=fg, highlightbackground=dim, highlightcolor=accent)
        send_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)
        help_btn.configure(bg=panel, fg=dim, activebackground=dim, activeforeground=bg)

        news_heading_label.configure(bg=panel, fg=dim)
        recent_news_label.configure(bg=panel, fg=accent)
        if news_details_label is not None:
            news_details_label.configure(bg=panel, fg=fg)

        for child in help_frame.winfo_children():
            try:
                child.configure(bg=panel)
            except tk.TclError:
                pass
            for grandchild in child.winfo_children():
                try:
                    current_fg = grandchild.cget("fg")
                    grandchild.configure(bg=panel, fg=accent if current_fg in (THEMES[current_theme]["accent"], THEMES[current_theme]["fg"]) and grandchild.cget("width") == 24 else dim)
                except tk.TclError:
                    pass

        settings_title_label.configure(bg=panel, fg=accent)
        settings_status_label.configure(bg=panel, fg=dim)
        for label in settings_labels:
            label.configure(bg=panel, fg=dim)
        name_entry.configure(bg=bg, fg=fg, insertbackground=fg, highlightbackground=dim, highlightcolor=accent)
        theme_menu.configure(bg=panel, fg=fg, activebackground=dim, activeforeground=bg, highlightbackground=dim)
        theme_menu["menu"].configure(bg=panel, fg=fg)
        color_menu.configure(bg=panel, fg=fg, activebackground=dim, activeforeground=bg, highlightbackground=dim)
        color_menu["menu"].configure(bg=panel, fg=fg)
        for control in settings_controls:
            if isinstance(control, tk.Checkbutton):
                control.configure(bg=panel, fg=fg, activebackground=panel, activeforeground=fg, selectcolor=panel)
        apply_settings_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)

        tab_bar.configure(bg=bg)
        for key, btn in tab_buttons.items():
            btn.configure(bg=bg, fg=accent if key == active_tab.get() else dim, activebackground=bg, activeforeground=accent)
        for frame in (dm_frame, group_frame):
            frame.configure(bg=bg)
        for sidebar in (dm_sidebar, group_sidebar):
            sidebar.configure(bg=panel)
        for listbox in (online_dm_list, dm_list, group_list):
            listbox.configure(bg=panel, fg=fg, selectbackground=dim, selectforeground=fg)
        for sidebar_label in (dm_online_label, dm_recent_label):
            sidebar_label.configure(bg=panel, fg=dim)
        new_group_btn.configure(bg=panel, fg=accent, activebackground=dim, activeforeground=bg)
        for text_widget in (dm_log, group_log):
            text_widget.configure(bg=bg, fg=fg, insertbackground=fg)
            text_widget.tag_configure("system", foreground=dim)
            configure_markdown_tags(text_widget, FONT_FAMILY, FONT[1], theme)

        current_theme = name
        theme_var.set(name)
        return True

    def apply_settings():
        requested_theme = theme_var.get().strip().lower()
        if requested_theme not in THEMES:
            settings_status.set("Unknown theme.")
            return

        new_name = name_var.get().strip()
        if not new_name:
            settings_status.set("Display name cannot be empty.")
            return

        try:
            requested_color = int(color_var.get())
        except ValueError:
            settings_status.set("Invalid chat colour.")
            return
        if not (0 <= requested_color < len(COLORS)):
            settings_status.set("Invalid chat colour.")
            return
        current_color = get_color_index(MY_ID)

        apply_theme(requested_theme)
        if requested_color != current_color:
            process_command(f"/color {requested_color}")
        gui_settings["sound_on_ping"] = bool(sound_var.get())
        gui_settings["show_timestamps"] = bool(timestamps_var.get())
        gui_settings["show_presence"] = bool(presence_var.get())
        gui_settings["show_id"] = bool(show_id_var.get())
        state.show_id_instead_of_name = gui_settings["show_id"]

        if new_name != state.my_name:
            process_command(f"/name {new_name}")
            remember_color_for_name(state.my_name, requested_color)
            name_label.config(text=f" @{state.my_name}")
            root.title(f"dechat* @{state.my_name}")
            name_var.set(state.my_name)
            switch_conversation_tab(active_tab.get())

        settings_status.set("Settings applied.")

    apply_settings_btn = tk.Button(
        settings_inner, text="Apply", command=apply_settings,
        bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=12, pady=5,
    )
    apply_settings_btn.pack(fill="x", pady=(14, 0))
    settings_controls.append(apply_settings_btn)

    def toggle_settings_panel():
        if settings_visible.get():
            settings_frame.pack_forget()
            settings_visible.set(False)
            settings_toggle_btn.config(fg=THEMES[current_theme]["dim"])
            return
        if news_visible.get():
            news_frame.pack_forget()
            news_visible.set(False)
            news_toggle_btn.config(fg=THEMES[current_theme]["dim"])
        settings_frame.pack(side="right", fill="y", padx=(0, 10), pady=8, before=chat_frame)
        settings_visible.set(True)
        settings_toggle_btn.config(fg=THEMES[current_theme]["accent"])

    settings_toggle_btn.configure(command=toggle_settings_panel)

    # --- sending input: slash commands, DMs, group messages, file picker ---
    def send_current():
        text = entry.get()
        entry.delete(0, "end")
        if not text:
            return
        if not text.startswith("/") and active_tab.get() == "dms":
            dm_key = selected_dm["key"]
            conversation = dm_conversations.get(dm_key) if dm_key else None
            pid = conversation.get("peer_id") if conversation else None
            if not conversation or not pid:
                dm_log.configure(state="normal")
                dm_log.insert("end", "Select a person from the People button first.\n", "system")
                dm_log.configure(state="disabled")
                return
            send_private_message(pid, text)
            return
        if not text.startswith("/") and active_tab.get() == "groups":
            gid = selected_group["group_id"]
            if not gid:
                group_log.configure(state="normal")
                group_log.insert("end", "Create or select a group first.\n", "system")
                group_log.configure(state="disabled")
                return
            send_group_message(gid, text)
            return
        if text == "/help":
            toggle_help()
            return
        if text == "/clear":
            target_log = chat_log
            if active_tab.get() == "dms":
                target_log = dm_log
            elif active_tab.get() == "groups":
                target_log = group_log
            target_log.configure(state="normal")
            target_log.delete("1.0", "end")
            target_log.configure(state="disabled")
            return
        if text == "/sendfile" or text.startswith("/sendfile "):
            parts = text.split(" ", 2)
            if len(parts) < 3:
                target = parts[1] if len(parts) > 1 else None
                if not target:
                    append_log("Usage: /sendfile <name|id> <path>\n", "error")
                    return
                path = filedialog.askopenfilename(title="Choose file to send")
                if not path:
                    return
                text = f"/sendfile {target} {path}"
        should_continue = process_command(text)
        if text.startswith("/name "):
            name_label.config(text=f" @{state.my_name}")
            prompt_var.set(f"<@{state.my_name}>")
            root.title(f"dechat* @{state.my_name}")
        if not should_continue:
            on_close()

    send_btn = tk.Button(
        input_bar, text="Send", command=send_current,
        bg=PANEL_BG, fg=ACCENT, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=14,
    )
    send_btn.pack(side="left", padx=(8, 0))
    entry.bind("<Return>", lambda e: send_current())

    help_btn = tk.Button(
        input_bar, text="?", command=toggle_help,
        bg=PANEL_BG, fg=DIM_FG, activebackground=DIM_FG, activeforeground=BG,
        font=MONO_BOLD, relief="flat", padx=10,
    )
    help_btn.pack(side="left", padx=(6, 0))

    # --- background refresh loops, window lifecycle, and mainloop kickoff ---
    def refresh_peer_count():
        with lock:
            count = len(peers)
        peer_count_var.set(f"{count} peer" if count == 1 else f"{count} peers")
        _refresh_online_dm_list()
        root.after(1000, refresh_peer_count)

    def drain_queue():
        try:
            while True:
                line = output.ui_output_queue.get_nowait()
                classify_and_append(line)
        except queue.Empty:
            pass
        root.after(80, drain_queue)

    closing = {"done": False}

    def on_close():
        if closing["done"]:
            return
        closing["done"] = True
        shutdown_networking()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    if not start_networking():
        from tkinter import messagebox
        messagebox.showerror(
            "dechat",
            f"Could not start dechat: port {CHAT_PORT} is already in use.\n"
            f"Another instance of dechat may already be running on this machine.",
        )
        root.destroy()
        return

    append_log("Type /help to toggle the command list, or /quit to exit.\n", "system")
    switch_conversation_tab("chat")

    refresh_peer_count()
    drain_queue()
    root.mainloop()


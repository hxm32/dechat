"use strict";
(() => {
  const $ = (id) => document.getElementById(id);

  const HELP_COMMANDS = [
    ["/connect <ip>", "manually connect to a peer by IP"],
    ["/recent [count]", "show recent messages with their ids"],
    ["/reply <msg_id> <text>", "reply to a message (pings its sender)"],
    ["/name <newname>", "change your display name"],
    ["/ping <name>", "measure round-trip latency"],
    ["/sendfile <name> [path]", "offer a file (no path opens a file picker)"],
    ["/accept <n>", "accept a pending file offer"],
    ["/reject <n>", "decline a pending file offer"],
    ["/offers", "list pending incoming file offers"],
    ["/ignore <name>", "stop showing a peer's messages"],
    ["/unignore <name>", "resume showing a peer's messages"],
    ["/clear", "clear the current log"],
    ["/quit", "exit dechat"],
  ];

  // Per-browser preferences. Storage can be unavailable, so every access is guarded.
  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem("dechat." + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem("dechat." + key, JSON.stringify(value)); } catch { /* ignore */ }
    },
  };

  const prefs = {
    theme: store.get("theme", "classic"),
    sound: store.get("sound", true),
    timestamps: store.get("timestamps", true),
    presence: store.get("presence", true),
  };

  let me = null;            // latest /api/state
  let themes = {};
  let tab = "chat";
  let live = false;         // false while replaying history, so old pings stay silent
  let newsShown = false;
  const dms = new Map();    // dm_key -> {peerId, name, lines}
  const dmOrder = [];
  let selDm = null;
  const groups = new Map(); // group id -> {name, lines}
  const groupOrder = [];
  let selGroup = null;

  const logs = { chat: $("chat-log"), dm: $("dm-log"), group: $("group-log") };
  const input = $("input");

  // ---- log rendering ----

  // html is built and escaped by the server, never from raw peer text here
  function addLine(box, cls, html) {
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const div = document.createElement("div");
    div.className = "line " + cls;
    div.innerHTML = html;
    box.appendChild(div);
    while (box.childElementCount > 2000) box.firstElementChild.remove();
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function note(text) {
    const box = logs[tab];
    const div = document.createElement("div");
    div.className = "line local";
    div.textContent = text;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function renderLog(box, lines) {
    box.textContent = "";
    const frag = document.createDocumentFragment();
    for (const html of lines) {
      const div = document.createElement("div");
      div.className = "line";
      div.innerHTML = html;
      frag.appendChild(div);
    }
    box.appendChild(frag);
    box.scrollTop = box.scrollHeight;
  }

  // ---- themes and local preferences ----

  function applyTheme(name) {
    const theme = themes[name];
    if (!theme) return;
    for (const [key, value] of Object.entries(theme)) {
      document.documentElement.style.setProperty("--" + key, value);
    }
  }

  function applyPrefs() {
    document.body.classList.toggle("no-ts", !prefs.timestamps);
    document.body.classList.toggle("no-presence", !prefs.presence);
  }

  let audioCtx = null;
  function beep() {
    try {
      audioCtx = audioCtx || new AudioContext();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.frequency.value = 880;
      gain.gain.value = 0.08;
      osc.connect(gain).connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + 0.15);
    } catch { /* audio blocked or unsupported */ }
  }

  // ---- DMs and groups ----

  function liveTargetFor(key) {
    // Peer ids change every session, so prefer whoever is online at that address
    const conv = dms.get(key);
    const online = me && me.people.find((p) => p.id !== me.id && p.dm_key === key);
    return online ? online.id : conv && conv.peerId;
  }

  function fillList(ul, rows, selected) {
    ul.textContent = "";
    for (const [key, label] of rows) {
      const li = document.createElement("li");
      li.dataset.key = key;
      li.textContent = label;
      if (key === selected) li.className = "sel";
      ul.appendChild(li);
    }
  }

  function renderDmLists() {
    const online = me ? me.people.filter((p) => p.id !== me.id) : [];
    fillList($("online-list"), online.map((p) => [p.id, "\u25cf " + p.name]), null);
    fillList($("dm-list"), dmOrder.map((k) => [k, dms.get(k).name]), selDm);
  }

  function renderGroupList() {
    fillList($("group-list"), groupOrder.map((g) => [g, groups.get(g).name || g]), selGroup);
  }

  function ensureDm(peerId, name, key) {
    let conv = dms.get(key);
    if (!conv) {
      conv = { peerId, name, lines: [] };
      dms.set(key, conv);
      dmOrder.push(key);
    } else {
      conv.peerId = peerId;
      if (name) conv.name = name;
    }
    return conv;
  }

  function openDm(peerId, name, key) {
    key = key || peerId;
    ensureDm(peerId, name, key);
    selectDm(key);
    switchTab("dm");
  }

  function selectDm(key) {
    selDm = key;
    renderDmLists();
    renderLog(logs.dm, dms.get(key).lines);
    updatePrompt();
  }

  function ensureGroup(gid, name) {
    let group = groups.get(gid);
    if (!group) {
      group = { name: name || gid, lines: [] };
      groups.set(gid, group);
      groupOrder.push(gid);
      if (selGroup === null) {
        selGroup = gid;
        renderLog(logs.group, group.lines);
      }
    } else if (name) {
      group.name = name;
    }
    renderGroupList();
    return group;
  }

  function selectGroup(gid) {
    selGroup = gid;
    renderGroupList();
    renderLog(logs.group, groups.get(gid).lines);
    updatePrompt();
  }

  function markUnread(which) {
    if (tab !== which) document.querySelector(`.tab[data-tab="${which}"]`).classList.add("unread");
  }

  function onDm(d) {
    const conv = ensureDm(d.peer_id, d.name, d.dm_key);
    conv.lines.push(d.html);
    renderDmLists();
    if (selDm === d.dm_key) addLine(logs.dm, "", d.html);
    markUnread("dm");
  }

  function onGroupMsg(d) {
    const group = ensureGroup(d.group_id);
    group.lines.push(d.html);
    if (selGroup === d.group_id) addLine(logs.group, "", d.html);
    markUnread("group");
  }

  // ---- tabs and prompt ----

  function updatePrompt() {
    let text = `<@${me ? me.name : ""}>`;
    if (tab === "dm") {
      text = selDm && dms.has(selDm) ? `DM @${dms.get(selDm).name}>` : "DM>";
    } else if (tab === "group") {
      text = selGroup && groups.has(selGroup) ? `#${groups.get(selGroup).name}>` : "GROUP>";
    }
    $("prompt").textContent = text;
  }

  function switchTab(next) {
    tab = next;
    for (const btn of document.querySelectorAll(".tab")) {
      btn.classList.toggle("active", btn.dataset.tab === next);
      if (btn.dataset.tab === next) btn.classList.remove("unread");
    }
    for (const name of ["chat", "dm", "group"]) $("pane-" + name).hidden = name !== next;
    logs[next].scrollTop = logs[next].scrollHeight;
    updatePrompt();
    input.focus();
  }

  // ---- panels, help and dialogs ----

  function setPanel(which) {
    for (const name of ["news", "settings"]) {
      const open = name === which && $("panel-" + name).hidden;
      $("panel-" + name).hidden = !open;
      $("btn-" + name).classList.toggle("on", open);
    }
    if (!$("panel-settings").hidden) syncSettingsForm();
  }

  function toggleHelp() {
    $("help").hidden = !$("help").hidden;
    $("btn-help").textContent = $("help").hidden ? "?" : "x";
  }

  function showPeople() {
    const table = $("people-table");
    table.textContent = "";
    const head = table.insertRow();
    for (const label of ["Name", "Colour", "ID", "IP / route", ""]) {
      const th = document.createElement("th");
      th.textContent = label;
      head.appendChild(th);
    }
    for (const p of me.people) {
      const row = table.insertRow();
      row.insertCell().textContent = p.name;
      const color = row.insertCell();
      const swatch = document.createElement("span");
      swatch.className = "swatch a" + me.color_classes[p.color % me.color_classes.length];
      swatch.textContent = p.color;
      color.appendChild(swatch);
      row.insertCell().textContent = p.id;
      row.insertCell().textContent = p.ip;
      if (p.id !== me.id) {
        const btn = document.createElement("button");
        btn.textContent = "DM";
        btn.onclick = () => {
          $("dlg-people").close();
          openDm(p.id, p.name, p.dm_key);
        };
        row.insertCell().appendChild(btn);
      }
    }
    $("dlg-people").showModal();
  }

  function showNewGroup() {
    const others = me.people.filter((p) => p.id !== me.id);
    if (!others.length) {
      note("No reachable peers are available for a group.");
      return;
    }
    $("group-name").value = "";
    $("group-status").textContent = "";
    const box = $("group-members");
    box.textContent = "";
    for (const p of others) {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = p.id;
      label.append(cb, `${p.name} (${p.id})`);
      box.appendChild(label);
    }
    $("dlg-group").showModal();
    $("group-name").focus();
  }

  async function createGroup() {
    const name = $("group-name").value.trim();
    const members = [...$("group-members").querySelectorAll("input:checked")].map((cb) => cb.value);
    const status = $("group-status");
    if (!name) { status.textContent = "Enter a group name."; return; }
    if (!members.length) { status.textContent = "Select at least one other member."; return; }
    const res = await post("/api/group", { name, members });
    if (!res.ok) {
      status.textContent = "Could not create the group. The Chat tab has details.";
      return;
    }
    $("dlg-group").close();
    ensureGroup(res.group_id);
    selectGroup(res.group_id);
    switchTab("group");
  }

  function showOverlay(title, text, extra, withQuit) {
    $("overlay-title").textContent = title;
    $("overlay-text").textContent = text;
    $("overlay-extra").textContent = extra || "";
    $("overlay-quit").hidden = !withQuit;
    $("overlay").hidden = false;
  }

  // ---- settings ----

  function syncSettingsForm() {
    if (!me) return;
    $("set-theme").value = prefs.theme;
    $("set-color").value = String(me.color);
    $("set-name").value = me.name;
    $("set-sound").checked = prefs.sound;
    $("set-timestamps").checked = prefs.timestamps;
    $("set-presence").checked = prefs.presence;
    $("set-showid").checked = me.show_id;
  }

  async function applySettings() {
    const name = $("set-name").value.trim();
    const status = $("set-status");
    if (!name) { status.textContent = "Display name cannot be empty."; return; }
    const res = await post("/api/settings", {
      name, color: Number($("set-color").value), show_id: $("set-showid").checked,
    });
    status.textContent = res.ok ? "Settings applied." : (res.error || "Could not apply settings.");
    refreshState();
  }

  function bindPref(id, key) {
    $(id).addEventListener("change", (e) => {
      prefs[key] = e.target.checked;
      store.set(key, prefs[key]);
      applyPrefs();
    });
  }

  // ---- server communication ----

  async function post(url, body) {
    try {
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      return await res.json();
    } catch {
      return { ok: false, error: "Could not reach dechat." };
    }
  }

  async function refreshState() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      me = await res.json();
    } catch { return; }

    $("me-name").textContent = " @" + me.name;
    document.title = `dechat* @${me.name}`;
    $("btn-people").textContent = me.peer_count === 1 ? "1 peer" : `${me.peer_count} peers`;

    if (!newsShown) {
      $("news-recent").textContent = me.news.recent;
      $("news-details").textContent = me.news.details;
      newsShown = true;
    }
    if (me.outdated && !store.get("outdated_dismissed", false)) $("outdated").hidden = false;
    if (me.locked) {
      showOverlay(
        "Please update to the latest version",
        "Different versions have different chat protocols. Your version will not work unless you update",
        `Your version: ${me.version}`, true,
      );
    }

    for (const [gid, name] of Object.entries(me.groups)) ensureGroup(gid, name);
    for (const conv of dms.values()) {
      const person = me.people.find((p) => p.id === conv.peerId);
      if (person) conv.name = person.name;
    }
    renderDmLists();
    updatePrompt();
  }

  function resetAll() {
    for (const box of Object.values(logs)) box.textContent = "";
    dms.clear(); dmOrder.length = 0; selDm = null;
    groups.clear(); groupOrder.length = 0; selGroup = null;
    renderDmLists(); renderGroupList();
  }

  function connect() {
    const source = new EventSource("/events");
    source.onopen = () => { $("conn").hidden = true; };
    source.onerror = () => { $("conn").hidden = false; };
    source.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      switch (d.t) {
        case "hello": live = false; if (d.reset) resetAll(); break;
        case "live": live = true; break;
        case "line":
          addLine(logs.chat, d.kind, d.html);
          if (d.kind === "ping" && live && prefs.sound) beep();
          break;
        case "dm": onDm(d); break;
        case "group_create": ensureGroup(d.group_id, d.name); break;
        case "group_msg": onGroupMsg(d); break;
        case "quit":
          source.close();
          showOverlay("dechat has exited", "You can close this tab.", "", false);
          break;
      }
    };
  }

  // ---- sending ----

  function pickFile(target) {
    const picker = $("file-input");
    picker.value = "";
    picker.onchange = () => { if (picker.files[0]) upload(target, picker.files[0]); };
    picker.click();
  }

  async function upload(target, file) {
    if (me && file.size > me.max_upload) {
      note(`'${file.name}' is too large to send (limit ${Math.floor(me.max_upload / 1048576)} MB).`);
      return;
    }
    try {
      const res = await fetch(
        `/api/upload?peer=${encodeURIComponent(target)}&name=${encodeURIComponent(file.name)}`,
        { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
      );
      const body = await res.json();
      if (!body.ok) note(body.error || "Upload failed.");
    } catch {
      note("Upload failed.");
    }
  }

  function currentTarget() {
    if (tab === "dm") return selDm && liveTargetFor(selDm);
    if (tab === "group") return selGroup;
    return null;
  }

  // Everything before the first await runs inside the key press, which the file picker needs
  async function send() {
    const text = input.value;
    input.value = "";
    if (!text.trim()) return;

    if (text === "/help") return toggleHelp();
    if (text === "/clear") {
      logs[tab].textContent = "";
      if (tab === "dm" && selDm) dms.get(selDm).lines.length = 0;
      if (tab === "group" && selGroup) groups.get(selGroup).lines.length = 0;
      return;
    }
    const pick = /^\/sendfile\s+(\S+)\s*$/.exec(text);
    if (pick) return pickFile(pick[1]);

    const target = currentTarget();
    if (!text.startsWith("/") && tab === "dm" && !target) {
      return note("Select a person from the list or the People button first.");
    }
    if (!text.startsWith("/") && tab === "group" && !target) {
      return note("Create or select a group first.");
    }
    const res = await post("/api/send", { text, tab, target });
    if (!res.ok && res.error) note(res.error);
    if (text.startsWith("/")) refreshState();
  }

  // ---- wiring ----

  function wire() {
    for (const btn of document.querySelectorAll(".tab")) btn.onclick = () => switchTab(btn.dataset.tab);
    $("btn-news").onclick = () => setPanel("news");
    $("btn-settings").onclick = () => setPanel("settings");
    $("btn-people").onclick = () => me && showPeople();
    $("btn-new-group").onclick = () => me && showNewGroup();
    $("btn-dismiss").onclick = () => { $("outdated").hidden = true; store.set("outdated_dismissed", true); };
    $("btn-send").onclick = send;
    $("btn-help").onclick = toggleHelp;
    $("btn-apply").onclick = applySettings;
    $("btn-attach").onclick = () => {
      const target = selDm && liveTargetFor(selDm);
      if (tab === "dm" && target) pickFile(target);
      else note("Open a DM first, or type /sendfile <name> to pick a file.");
    };
    $("people-close").onclick = () => $("dlg-people").close();
    $("group-cancel").onclick = () => $("dlg-group").close();
    $("group-create").onclick = createGroup;
    $("overlay-quit").onclick = async () => {
      await post("/api/quit", {});
      showOverlay("dechat has exited", "You can close this tab.", "", false);
    };

    $("online-list").onclick = (e) => {
      const li = e.target.closest("li");
      const person = li && me.people.find((p) => p.id === li.dataset.key);
      if (person) openDm(person.id, person.name, person.dm_key);
    };
    $("dm-list").onclick = (e) => {
      const li = e.target.closest("li");
      if (li) selectDm(li.dataset.key);
    };
    $("group-list").onclick = (e) => {
      const li = e.target.closest("li");
      if (li) selectGroup(li.dataset.key);
    };

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) send();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("help").hidden) toggleHelp();
      else if (!$("panel-settings").hidden) setPanel("settings");
      else if (!$("panel-news").hidden) setPanel("news");
    });

    $("set-theme").onchange = (e) => {
      prefs.theme = e.target.value;
      store.set("theme", prefs.theme);
      applyTheme(prefs.theme);
    };
    bindPref("set-sound", "sound");
    bindPref("set-timestamps", "timestamps");
    bindPref("set-presence", "presence");

    const help = $("help");
    for (const [cmd, desc] of HELP_COMMANDS) {
      const row = document.createElement("div");
      const name = document.createElement("b");
      name.textContent = cmd;
      const text = document.createElement("span");
      text.textContent = desc;
      row.append(name, text);
      help.appendChild(row);
    }
  }

  async function init() {
    wire();
    applyPrefs();
    try {
      themes = await (await fetch("/api/themes")).json();
    } catch { themes = {}; }
    const themeSelect = $("set-theme");
    for (const name of Object.keys(themes)) themeSelect.add(new Option(name, name));
    if (!themes[prefs.theme]) prefs.theme = "classic";
    applyTheme(prefs.theme);

    await refreshState();
    const colorSelect = $("set-color");
    for (let i = 0; i < (me ? me.color_classes.length : 0); i++) colorSelect.add(new Option(String(i), String(i)));
    syncSettingsForm();

    connect();
    setInterval(refreshState, 2000);
    switchTab("chat");
  }

  init();
})();

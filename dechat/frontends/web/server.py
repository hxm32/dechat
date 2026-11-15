"""HTTP server for the web UI: static files, a small JSON API and a server-sent event stream."""
import json
import os
import queue
import shutil
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dechat.core import state
from dechat.core.colors import COLORS, get_color_index, remember_color_for_name
from dechat.core.config import (
    CURRENT_VERSION,
    FILE_CHUNK_SIZE,
    MAX_FILE_CHUNKS,
    MAX_GROUP_MEMBERS,
    WEB_HOST,
    WEB_PORT,
)
from dechat.core.identity import MY_ID
from dechat.core.state import group_chats, group_lock, lock, peers
from dechat.core.utils import safe_display_filename, sanitize_for_display
from dechat.features.file_send import send_file
from dechat.features.groups import create_group_chat, send_group_message
from dechat.features.messages import send_private_message
from dechat.frontends.commands import process_command
from dechat.frontends.web import render
from dechat.frontends.web.people import reachable_people
from dechat.frontends.web.themes import THEMES

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

# Requests must be addressed to the loopback name, which blocks DNS rebinding.
ALLOWED_HOSTS = {f"localhost:{WEB_PORT}", f"127.0.0.1:{WEB_PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}

MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = MAX_FILE_CHUNKS * FILE_CHUNK_SIZE
KEEPALIVE_SECONDS = 15

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class WebApp:
    """Shared context for the request handlers."""

    def __init__(self, hub, update_status=None, locked=False, outdated=False):
        self.hub = hub
        self.locked = locked
        self.outdated = outdated
        status = update_status or {}
        self.news = {
            "recent": status.get("recentNews") or "No news right now.",
            "details": status.get("news_details") or "",
        }
        self.quit_event = threading.Event()
        self._upload_dir = None
        self._upload_lock = threading.Lock()

    def upload_dir(self):
        with self._upload_lock:
            if self._upload_dir is None:
                self._upload_dir = tempfile.mkdtemp(prefix="dechat_upload_")
            return self._upload_dir

    def cleanup(self):
        """Removes uploaded files, which only need to live while offers are pending."""
        with self._upload_lock:
            if self._upload_dir:
                shutil.rmtree(self._upload_dir, ignore_errors=True)
                self._upload_dir = None

    def snapshot(self):
        with lock:
            peer_count = len(peers)
        with group_lock:
            groups = {
                gid: group["name"] for gid, group in group_chats.items()
                if MY_ID in group.get("members", set())
            }
        return {
            "id": MY_ID,
            "name": state.my_name,
            "color": get_color_index(MY_ID, 0),
            "color_classes": render.COLOR_CLASSES,
            "show_id": state.show_id_instead_of_name,
            "version": CURRENT_VERSION,
            "peer_count": peer_count,
            "people": reachable_people(),
            "groups": groups,
            "locked": self.locked,
            "outdated": self.outdated,
            "news": self.news,
            "max_upload": MAX_UPLOAD_BYTES,
        }


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second instance share the port
    allow_reuse_address = os.name != "nt"

    def __init__(self, app):
        self.app = app
        super().__init__((WEB_HOST, WEB_PORT), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "dechat"

    def log_message(self, fmt, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode())

    def _fail(self, status, message):
        self._json(status, {"ok": False, "error": message})

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def do_GET(self):
        if not self._host_ok():
            return self._fail(403, "Forbidden")
        path = urlsplit(self.path).path
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            return self._send(200, (STATIC_DIR / name).read_bytes(), content_type)
        if path == "/api/state":
            return self._json(200, self.app.snapshot())
        if path == "/api/themes":
            return self._json(200, THEMES)
        if path == "/events":
            return self._stream_events()
        self._fail(404, "Not found")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._fail(403, "Forbidden")
        url = urlsplit(self.path)
        if self.app.locked and url.path != "/api/quit":
            return self._fail(403, "Update required")
        if url.path == "/api/upload":
            return self._upload(parse_qs(url.query))

        routes = {
            "/api/send": self._api_send,
            "/api/group": self._api_group,
            "/api/settings": self._api_settings,
            "/api/quit": self._api_quit,
        }
        handler = routes.get(url.path)
        if handler is None:
            return self._fail(404, "Not found")
        # Requiring JSON forces a CORS preflight for cross-site pages
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._fail(415, "Expected application/json")
        body = self._read_json()
        if body is None:
            return self._fail(400, "Invalid JSON")
        handler(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_JSON_BYTES:
                return None
            data = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _api_send(self, body):
        text = body.get("text")
        tab = body.get("tab", "chat")
        target = body.get("target")
        if not isinstance(text, str) or not text.strip():
            return self._fail(400, "Empty message")
        text = text.replace("\r", " ").replace("\n", " ")

        keep_running = True
        if text.startswith("/") or tab == "chat":
            keep_running = process_command(text)
        elif tab == "dm":
            if not isinstance(target, str) or not target:
                return self._fail(400, "Select a person first")
            send_private_message(target, text)
        elif tab == "group":
            if not isinstance(target, str) or not target:
                return self._fail(400, "Create or select a group first")
            send_group_message(target, text)
        else:
            return self._fail(400, "Unknown tab")

        if not keep_running:
            self.app.quit_event.set()
        self._json(200, {"ok": True, "quit": not keep_running})

    def _api_group(self, body):
        name = body.get("name")
        members = body.get("members")
        if not isinstance(name, str) or not isinstance(members, list):
            return self._fail(400, "Invalid group")
        member_ids = [m for m in members if isinstance(m, str)][:MAX_GROUP_MEMBERS]
        group_id = create_group_chat(name, member_ids)
        self._json(200, {"ok": bool(group_id), "group_id": group_id})

    def _api_settings(self, body):
        color = body.get("color")
        name = body.get("name")
        show_id = body.get("show_id")
        if color is not None and (
            not isinstance(color, int) or isinstance(color, bool) or not 0 <= color < len(COLORS)
        ):
            return self._fail(400, "Invalid chat colour")
        if name is not None:
            name = sanitize_for_display(name).strip() if isinstance(name, str) else ""
            if not name:
                return self._fail(400, "Display name cannot be empty")

        if color is not None and color != get_color_index(MY_ID):
            process_command(f"/color {color}")
        if isinstance(show_id, bool):
            state.show_id_instead_of_name = show_id
        if name is not None and name != state.my_name:
            process_command(f"/name {name}")
            remember_color_for_name(state.my_name, color if color is not None else get_color_index(MY_ID, 0))
        self._json(200, {"ok": True})

    def _api_quit(self, body):
        self.app.quit_event.set()
        self._json(200, {"ok": True})

    def _upload(self, query):
        """Receives a file picked in the browser and offers it to a peer."""
        peer = (query.get("peer") or [""])[0]
        filename = sanitize_for_display((query.get("name") or [""])[0])
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/octet-stream":
            return self._fail(415, "Expected application/octet-stream")
        if not peer:
            return self._fail(400, "Missing peer")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._fail(411, "Empty upload")
        if length > MAX_UPLOAD_BYTES:
            return self._fail(413, "File is too large to send")

        filename = safe_display_filename(filename)
        folder = os.path.join(self.app.upload_dir(), uuid.uuid4().hex[:8])
        os.makedirs(folder)
        path = os.path.join(folder, filename)
        try:
            remaining = length
            with open(path, "wb") as out:
                while remaining > 0:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise OSError("connection closed during upload")
                    out.write(block)
                    remaining -= len(block)
        except OSError:
            return self._fail(400, "Upload failed")
        send_file(peer, path)
        self._json(200, {"ok": True})

    def _write_event(self, data, event_id=None):
        prefix = f"id: {event_id}\n" if event_id else ""
        self.wfile.write(f"{prefix}data: {data}\n\n".encode())
        self.wfile.flush()

    def _stream_events(self):
        hub = self.app.hub
        sub, backlog, reset = hub.subscribe(self.headers.get("Last-Event-ID", ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._write_event(json.dumps({"t": "hello", "reset": reset}))
            for seq, data in backlog:
                self._write_event(data, f"{hub.boot}-{seq}")
            self._write_event('{"t":"live"}')
            while True:
                try:
                    seq, data = sub.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    # A dropped subscriber (slow client) closes so the browser reconnects and replays
                    if not hub.is_subscribed(sub):
                        break
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(data, f"{hub.boot}-{seq}")
                if data == '{"t":"quit"}':
                    break
        except OSError:
            pass
        finally:
            hub.unsubscribe(sub)

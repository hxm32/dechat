"""Starts the web UI: update check, networking, the HTTP server and the browser."""
import threading
import time
import webbrowser

from dechat.core import output
from dechat.core.config import CHAT_PORT, CURRENT_VERSION, PROTOCOL_VERSION, WEB_PORT, WEB_URL
from dechat.core.updates import fetch_update_status
from dechat.frontends.web.hub import Hub, make_sink
from dechat.frontends.web.server import WebApp, WebServer
from dechat.net.lifecycle import shutdown_networking, start_networking


def _open_browser():
    if not webbrowser.open(WEB_URL):
        print(f"Could not open a browser automatically. Please open {WEB_URL} yourself.")


def run_web(debug=False):
    """Runs until /quit, the Quit button or Ctrl+C. Returns a process exit code."""
    output.set_mode("web")
    hub = Hub()
    output.set_web_sink(make_sink(hub, echo=debug))

    status = fetch_update_status()
    protocol = status.get("protocolVersion") if status else None
    latest = status.get("latestVersion") if status else None
    locked = protocol is not None and protocol != PROTOCOL_VERSION
    outdated = not locked and latest is not None and latest != CURRENT_VERSION

    app = WebApp(hub, status, locked=locked, outdated=outdated)
    try:
        httpd = WebServer(app)
    except OSError:
        print(f"Could not start the web interface: port {WEB_PORT} is already in use.")
        print("Another instance of dechat may already be running on this machine.")
        return 1

    if locked:
        print("Different versions have different chat protocols.")
        print("Your version will not work unless you update. Please update to the latest version.")
    elif not start_networking():
        print(f"Could not start dechat: port {CHAT_PORT} is already in use.")
        print("Another instance of dechat may already be running on this machine.")
        httpd.server_close()
        return 1
    elif outdated:
        print("You are not on the latest version of dechat. The app will still work, but you may miss out on the latest features.")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"dechat {CURRENT_VERSION} chat interface: {WEB_URL}")
    print("Opening it in your browser. Press Ctrl+C or send /quit to exit.")
    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        while not app.quit_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass

    if not locked:
        shutdown_networking()
    hub.publish({"t": "quit"})
    time.sleep(0.3)  # lets the quit event reach open tabs before the server stops
    httpd.shutdown()
    httpd.server_close()
    app.cleanup()
    print("Goodbye!")
    return 0

"""Fan-out of core output to browser clients, with replay for new tabs."""
import collections
import json
import queue
import threading
import uuid

from dechat.core import state
from dechat.core.output import EVENT_DM, EVENT_GROUP_CREATE, EVENT_GROUP_MSG
from dechat.core.utils import timestamp
from dechat.features.peers import stable_peer_ip
from dechat.frontends.web import render

HISTORY_LIMIT = 3000
SUBSCRIBER_QUEUE_SIZE = 5000


class Hub:
    def __init__(self):
        self.boot = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._seq = 0
        self._history = collections.deque(maxlen=HISTORY_LIMIT)
        self._subscribers = set()

    def publish(self, payload):
        with self._lock:
            self._seq += 1
            item = (self._seq, json.dumps(payload, separators=(",", ":")))
            self._history.append(item)
            for sub in list(self._subscribers):
                try:
                    sub.put_nowait(item)
                except queue.Full:
                    self._subscribers.discard(sub)

    def subscribe(self, last_event_id=""):
        """Returns (queue, backlog, reset). reset means the client must clear its state."""
        boot, _, seq = last_event_id.partition("-")
        try:
            after = int(seq) if boot == self.boot else None
        except ValueError:
            after = None
        sub = queue.Queue(SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            backlog = [i for i in self._history if after is None or i[0] > after]
            self._subscribers.add(sub)
        return sub, backlog, after is None

    def is_subscribed(self, sub):
        with self._lock:
            return sub in self._subscribers

    def unsubscribe(self, sub):
        with self._lock:
            self._subscribers.discard(sub)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_event(name, data):
    """Converts a core event payload into what the browser needs, or None if invalid."""
    ts = timestamp()
    if name == "dm":
        pid = data.get("peer_id")
        if not isinstance(pid, str) or len(pid) != 8:
            return None
        peer_name = str(data.get("name") or pid)
        outgoing = bool(data.get("outgoing"))
        sender = state.my_name if outgoing else peer_name
        return {
            "t": "dm", "peer_id": pid, "name": peer_name,
            "dm_key": data.get("dm_key") or stable_peer_ip(pid) or pid,
            "html": render.transcript_html(
                outgoing, sender, str(data.get("text", "")), _as_int(data.get("color_idx")), ts
            ),
        }
    gid = data.get("group_id")
    if not isinstance(gid, str):
        return None
    if name == "group_create":
        return {"t": "group_create", "group_id": gid, "name": str(data.get("name") or gid)}
    sender = str(data.get("sender_name") or data.get("sender_id") or "unknown")
    return {
        "t": "group_msg", "group_id": gid,
        "html": render.transcript_html(
            bool(data.get("outgoing")), sender, str(data.get("text", "")),
            _as_int(data.get("color_idx")), ts,
        ),
    }


_EVENT_TAGS = (
    (EVENT_DM, "dm"),
    (EVENT_GROUP_CREATE, "group_create"),
    (EVENT_GROUP_MSG, "group_msg"),
)


def make_sink(hub, echo=False):
    """Returns the function core.output calls with every printed chunk in web mode.

    With echo set (debug runs) the raw text is also mirrored to the terminal.
    """
    def publish_line(line):
        for tag, name in _EVENT_TAGS:
            if line.startswith(tag):
                try:
                    data = json.loads(line[len(tag):])
                except ValueError:
                    return
                event = _build_event(name, data) if isinstance(data, dict) else None
                if event:
                    hub.publish(event)
                return
        kind, body = render.render_line(line)
        hub.publish({"t": "line", "kind": kind, "html": body})

    def sink(text):
        lines = text.rstrip("\n").split("\n")
        if len(lines) > 1:
            # Drop the blank lines that come from leading or doubled newlines
            lines = [l for l in lines if l] or [""]
        for line in lines:
            if echo and not line.startswith("@@DECHAT_"):
                print(line, flush=True)
            publish_line(line)

    return sink

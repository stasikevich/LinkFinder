"""The map, served to your own browser, so changes need no files at all.

A page opened from disk cannot write to disk. Everything that followed from
that - download a file, find it, import it - was ceremony, not design. When the
window is open the tool can simply answer the page itself, and a setup typed on
the map is written the moment it is typed.

Deliberately small and deliberately shut in:

- bound to 127.0.0.1 only, on a port the operating system picks;
- every request must carry a one-time token that exists only in this run;
- a request from anywhere but the loopback address is refused;
- a fixed handful of routes, no file system access, no CORS, no cookies;
- it lives and dies with the window that started it.

A saved HTML file keeps working on its own as a viewer. It just cannot write,
which is the honest state of affairs rather than a workaround.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Callable, Dict, List, Optional

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python 3.6 and older
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class ThreadingHTTPServer(HTTPServer):      # type: ignore
        daemon_threads = True

LOOPBACK = ("127.0.0.1", "::1")
MAX_BODY = 1 << 20                       # a setup change is a few hundred bytes


class Pages:
    """What the server is allowed to answer, and nothing else.

    The window hands this in. Keeping the behaviour here rather than in the
    handler means the routes can be tested without opening a socket at all.
    """

    def __init__(self, html: Callable[[], str],
                 state: Callable[[], Dict[str, Any]],
                 set_setups: Callable[[str, List[str]], Dict[str, Any]],
                 todo: Optional[Callable[[], Dict[str, Any]]] = None,
                 tags: Optional[Callable[[], Dict[str, Any]]] = None,
                 set_tags: Optional[Callable[[List[str]], Dict[str, Any]]] = None,
                 table: Optional[Callable[[], str]] = None,
                 ports: Optional[Callable[[], Dict[str, Any]]] = None,
                 confirm_ports: Optional[Callable[[str], Dict[str, Any]]] = None,
                 changes: Optional[Callable[[], Dict[str, Any]]] = None,
                 mark_many: Optional[Callable[[List[str], List[str]],
                                              Dict[str, Any]]] = None,
                 setup_diff: Optional[Callable[[List[str]],
                                               Dict[str, Any]]] = None,
                 sheet: Optional[Callable[[], str]] = None,
                 help: Optional[Callable[[], str]] = None,
                 netbox_find: Optional[Callable[[str], Any]] = None,
                 netbox_setup: Optional[Callable[[], Any]] = None,
                 suspicious: Optional[Callable[[], Any]] = None):
        self.html = html
        self.state = state
        self.set_setups = set_setups
        self.todo = todo or (lambda: {"changes": []})
        # Which NetBox tags name a Setup - chosen where the setups are visible,
        # rather than in a window that may have no map in it at all.
        self.tags = tags or (lambda: {"seen": [], "chosen": []})
        self.set_tags = set_tags or (lambda names: {"chosen": list(names)})
        # The page cannot write a file, but it can be handed one to save.
        self.table = table or (lambda: "")
        # Which switch port is which, proposed here and confirmed here: the
        # answer is obvious while looking at the link it is about.
        self.ports = ports or (lambda: {"proposals": []})
        self.confirm_ports = confirm_ports or (lambda name: {"confirmed": name})
        # What moved since the map before this one.
        self.changes = changes or (lambda: {"changes": []})
        # A bench arrives as a list of addresses in a ticket, not as a row of
        # clicks, so it can be pasted in and marked in one go.
        self.mark_many = mark_many or (lambda lines, names: {"marked": []})
        self.setup_diff = setup_diff or (lambda setups: {"rows": []})
        # The same sheet the window saves, so neither side is the odd one out.
        self.sheet = sheet or (lambda: "")
        # The guide, served from here so the picture can explain itself
        # without anybody going back to the window to find Help.
        self.help = help or (lambda: "<p>no guide in this build</p>")
        # Looking one thing up by serial or tag, without a scan: the question
        # people actually arrive with is "where is this bench", not "here is
        # a list of addresses".
        self.netbox_find = netbox_find or (lambda _w: {"records": []})
        self.netbox_setup = netbox_setup or (lambda: {"opened": False})
        # Four detectors existed and none of them reached the picture.
        self.suspicious = suspicious or (lambda: {"found": [], "summary": ""})


class MapServer:
    """A loopback server for one window, with a token nobody else has."""

    def __init__(self, pages: Pages, host: str = "127.0.0.1"):
        self.pages = pages
        self.token = secrets.token_urlsafe(24)
        self.host = host
        self._server = None
        self._thread = None

    @property
    def base(self) -> str:
        return "/m/%s" % self.token

    @property
    def url(self) -> str:
        if self._server is None:
            return ""
        return "http://%s:%d%s/map" % (self.host, self.port, self.base)

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def start(self) -> str:
        handler = _handler_for(self)
        self._server = ThreadingHTTPServer((self.host, 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="linkfinder-map", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # --- the routes, as plain functions so they can be tested directly ----
    def route(self, method: str, path: str, body: bytes = b"") -> Any:
        """Return (status, content_type, payload). Unknown paths are 404.

        Nothing here reads a file or takes a name from the request: the only
        thing a caller can choose is which of a fixed handful of things to ask
        for.
        """
        if not path.startswith(self.base + "/"):
            # A wrong or missing token is not told apart from a wrong path, so
            # the answer gives nothing away about which it was.
            return 404, "text/plain", b"not found"
        tail = path[len(self.base) + 1:].split("?")[0]

        if method == "GET" and tail == "map":
            return 200, "text/html; charset=utf-8", self.pages.html().encode("utf-8")
        if method == "GET" and tail == "state":
            return _json(200, self.pages.state())
        if method == "GET" and tail == "todo":
            return _json(200, self.pages.todo())
        if method == "GET" and tail == "tags":
            return _json(200, self.pages.tags())
        if method == "POST" and tail == "bulk":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            lines = [str(x) for x in (asked.get("lines") or [])]
            names = [str(n) for n in (asked.get("setups") or []) if str(n).strip()]
            if not lines:
                return _json(400, {"error": "no devices listed"})
            return _json(200, self.pages.mark_many(lines, names))
        if method == "POST" and tail == "setupdiff":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            wanted = [str(n) for n in (asked.get("setups") or []) if str(n).strip()]
            if not wanted:
                return _json(400, {"error": "no setup named"})
            return _json(200, self.pages.setup_diff(wanted))
        if method == "GET" and tail == "changes":
            return _json(200, self.pages.changes())
        if method == "GET" and tail == "switchports":
            return _json(200, self.pages.ports())
        if method == "POST" and tail == "switchports":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            switch = str(asked.get("switch") or "")
            if not switch:
                return _json(400, {"error": "no switch named"})
            return _json(200, self.pages.confirm_ports(switch))
        if method == "GET" and tail == "suspicious":
            return _json(200, self.pages.suspicious())
        if method == "POST" and tail == "netboxsetup":
            return _json(200, self.pages.netbox_setup())
        if method == "POST" and tail == "netboxfind":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            wanted = str(asked.get("wanted") or "").strip()
            if not wanted:
                return _json(400, {"error": "nothing to look up"})
            return _json(200, self.pages.netbox_find(wanted))
        if method == "GET" and tail == "help":
            return (200, "text/html; charset=utf-8",
                    self.pages.help().encode("utf-8"))
        if method == "GET" and tail == "sheet":
            return (200, "text/csv; charset=utf-8",
                    self.pages.sheet().encode("utf-8"))
        if method == "GET" and tail == "table":
            return (200, "text/csv; charset=utf-8",
                    self.pages.table().encode("utf-8"))
        if method == "POST" and tail == "tags":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            names = [str(n) for n in (asked.get("tags") or []) if str(n).strip()]
            return _json(200, self.pages.set_tags(names))
        if method == "POST" and tail == "setups":
            try:
                asked = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return _json(400, {"error": "that was not JSON"})
            device = str(asked.get("device") or "")
            names = [str(n) for n in (asked.get("setups") or []) if str(n).strip()]
            if not device:
                return _json(400, {"error": "no device named"})
            return _json(200, self.pages.set_setups(device, names))
        return 404, "text/plain", b"not found"


def _json(status: int, body: Dict[str, Any]):
    return (status, "application/json; charset=utf-8",
            json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _handler_for(owner: MapServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LinkFinder"
        sys_version = ""

        def log_message(self, *_args):
            pass                         # the window has its own log

        def _refuse_if_remote(self) -> bool:
            if self.client_address[0] not in LOOPBACK:
                self.send_response(403)
                self.end_headers()
                return True
            return False

        def _answer(self, method: str, body: bytes = b"") -> None:
            if self._refuse_if_remote():
                return
            status, kind, payload = owner.route(method, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            if kind.startswith("text/csv"):
                self.send_header("Content-Disposition",
                                 "attachment; filename=linkfinder-table.csv")
            self.send_header("Content-Length", str(len(payload)))
            # This page is for this machine and this run. Nothing about it
            # should be cached, embedded elsewhere or reachable cross-origin.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._answer("GET")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self.send_response(413)
                self.end_headers()
                return
            self._answer("POST", self.rfile.read(length) if length else b"")

    return Handler

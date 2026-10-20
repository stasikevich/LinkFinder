"""Checks for the little server that lets the map write without files.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.serve import LOOPBACK, MapServer, Pages

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


held = {}
asked = []


def pages():
    return Pages(html=lambda: "<html>the map</html>",
                 state=lambda: {"setups": dict(held)},
                 set_setups=lambda device, names: (
                     asked.append((device, list(names)))
                     or held.__setitem__(device, list(names))
                     or {"saved": device}),
                 todo=lambda: {"changes": [{"what": "add a device"}]})


print("-- the routes, without opening a socket at all --")
srv = MapServer(pages())
status, kind, body = srv.route("GET", srv.base + "/map")
check("the map is served", (status, body), (200, b"<html>the map</html>"))
check("as HTML", kind.startswith("text/html"), True)

status, _k, body = srv.route("GET", srv.base + "/state")
check("the state is JSON", json.loads(body), {"setups": {}})

status, _k, body = srv.route(
    "POST", srv.base + "/setups",
    json.dumps({"device": "ip:1", "setups": ["dio"]}).encode("utf-8"))
check("a setup is written", (status, held), (200, {"ip:1": ["dio"]}))
check("and the window was told which device", asked[-1], ("ip:1", ["dio"]))

status, _k, body = srv.route(
    "POST", srv.base + "/setups",
    json.dumps({"device": "ip:1", "setups": []}).encode("utf-8"))
check("an empty list takes a device out of every setup", held["ip:1"], [])

status, _k, _b = srv.route("POST", srv.base + "/setups", b"not json at all")
check("rubbish is refused, not swallowed", status, 400)
status, _k, _b = srv.route("POST", srv.base + "/setups", b'{"setups":["x"]}')
check("and so is a change with no device named", status, 400)

print()
print("-- nothing else is reachable --")
# The token is the only thing standing between this and any page in any tab
# on this machine, so a wrong one must look exactly like a wrong path.
for path in ("/m/anotherToken/state", "/state", "/", srv.base,
             srv.base + "/../../etc/passwd", srv.base + "/map/../state",
             "/m/" + srv.token[:-1] + "/map"):
    status, _k, _b = srv.route("GET", path)
    check("%-34s is not found" % path[:34], status, 404)

check("and a route that exists still needs the right method",
      srv.route("GET", srv.base + "/setups")[0], 404)
check("there is no way to ask it for a file",
      srv.route("GET", srv.base + "/file?name=/etc/passwd")[0], 404)

print()
print("-- and over a real socket --")
live = MapServer(pages())
url = live.start()
check("it binds to the loopback address only", live.host, "127.0.0.1")
check("the loopback addresses it will answer", LOOPBACK, ("127.0.0.1", "::1"))
check("the port is one the system picked", live.port > 0, True)
check("the token is long enough not to be guessed", len(live.token) >= 24, True)
check("and the address carries it", live.token in url, True)


def ask(path, data=None):
    request = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (live.port, path), data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read(), dict(answer.headers)
    except urllib.error.HTTPError as problem:
        return problem.code, b"", {}


status, body, headers = ask(live.base + "/map")
check("a browser gets the map", (status, body), (200, b"<html>the map</html>"))
check("it is never cached", headers.get("Cache-Control"), "no-store")
check("nor framed by anything", headers.get("X-Frame-Options"), "DENY")
check("nor sniffed for another type",
      headers.get("X-Content-Type-Options"), "nosniff")
check("and no page from anywhere else may read it",
      "Access-Control-Allow-Origin" in headers, False)

status, body, _h = ask(live.base + "/setups",
                       json.dumps({"device": "ip:2",
                                   "setups": ["bench"]}).encode("utf-8"))
check("a change over the wire is written", (status, held.get("ip:2")),
      (200, ["bench"]))
check("a wrong token over the wire is refused too",
      ask("/m/wrong/state")[0], 404)

live.stop()
try:
    ask(live.base + "/state")
    check("it stops answering when the window closes", "still up", "gone")
except Exception:
    check("it stops answering when the window closes", True, True)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all map-server checks passed")

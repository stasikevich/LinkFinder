"""Checks for the maps the tool keeps, so "what moved" can be answered.

The history is a convenience and must behave like one: it never blocks a
scan, never grows without limit, and never lets a half-finished map become
the thing a later run is judged against.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import history

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


WHERE = os.path.join(tempfile.gettempdir(), "lf_history_checks")
if os.path.isdir(WHERE):
    shutil.rmtree(WHERE)


def a_map(names, link=("sw1", "1/1/7")):
    nodes = [{"id": "ip:10.0.0.%d" % (n + 1), "name": name, "kind": "host",
              "mgmt_ips": ["10.0.0.%d" % (n + 1)], "state": "scanned"}
             for n, name in enumerate(names)]
    nodes.append({"id": "chassis:aa", "name": link[0], "kind": "switch",
                  "chassis_id": "aa", "state": "unscanned"})
    return {"nodes": nodes, "edges": [
        {"id": "e1", "a": "ip:10.0.0.1", "a_iface": "eno1",
         "b": "chassis:aa", "b_iface": link[1], "protos": ["LLDP"]}]}


print("-- a map is kept, an empty one is not --")
check("the first map is kept", bool(history.keep(a_map(["one"]), WHERE)), True)
check("an empty map is refused", history.keep({"nodes": []}, WHERE), "")
check("and so is one with no nodes key at all", history.keep({}, WHERE), "")
check("so only the real map is on file", len(history.listing(WHERE)), 1)

print()
print("-- the first scan has nothing to be compared against, and says so --")
fresh = os.path.join(tempfile.gettempdir(), "lf_history_fresh")
if os.path.isdir(fresh):
    shutil.rmtree(fresh)
first = history.drift(a_map(["one"]), fresh)
check("no changes are invented", first["changes"], [])
check("and the reason is given", "no earlier map" in first["note"], True)

print()
print("-- a cable that moved is found --")
time.sleep(1.1)                           # the file name is the second it was taken
history.keep(a_map(["one", "two"]), WHERE)
moved = history.drift(a_map(["one", "two"], link=("sw1", "1/1/9")), WHERE)
kinds = sorted(set(c["kind"] for c in moved["changes"]))
check("something is reported", bool(moved["changes"]), True)
check("and it is about the link", any("link" in k for k in kinds), True)

print()
print("-- a device that vanished is found --")
time.sleep(1.1)
history.keep(a_map(["one", "two", "three"]), WHERE)
gone = history.drift(a_map(["one", "two"]), WHERE)
check("the missing device is named",
      any(c["kind"] == "device removed" for c in gone["changes"]), True)

print()
print("-- an unchanged estate is not noise --")
time.sleep(1.1)
same = a_map(["one", "two"])
history.keep(same, WHERE)
quiet = history.drift(same, WHERE)
check("nothing is reported", quiet["changes"], [])
check("and it says so rather than staying blank",
      "nothing moved" in quiet.get("note", ""), True)

print()
print("-- it cannot grow without limit --")
for n in range(history.KEEP + 6):
    history.keep(a_map(["one"]), WHERE)
check("the cap holds", len(history.listing(WHERE)) <= history.KEEP, True)

print()
print("-- it never blocks a scan --")
blocked = os.path.join(WHERE, "a-file-not-a-folder")
with open(blocked, "w") as handle:
    handle.write("x")
check("a folder it cannot make is not an error",
      history.keep(a_map(["one"]), blocked), "")
check("and reading it back gives nothing, not a crash",
      history.previous(blocked), None)

shutil.rmtree(WHERE)
shutil.rmtree(fresh, ignore_errors=True)
print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all history checks passed")

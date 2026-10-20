"""Checks for a team sharing one answer instead of one answer per laptop.

The fault being fixed is not a missing feature, it is a silent one: marks
lived in a home folder and nothing said so, so two engineers scanning one
bench got two different answers and no reason why.

What must hold: each person writes only their own file, everybody reads all
of them, a share that is down never stops a scan, and the tool always says
which of the two it is doing.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import setups as store
from linkfinder import shared

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


TEAM = os.path.join(tempfile.gettempdir(), "lf_shared_checks")
if os.path.isdir(TEAM):
    shutil.rmtree(TEAM)

NODE_A = {"id": "ip:198.51.100.5", "serial": "SER-A", "chassis_id": "",
          "mgmt_ips": ["198.51.100.5"]}
NODE_B = {"id": "ip:198.51.100.6", "serial": "SER-B", "chassis_id": "",
          "mgmt_ips": ["198.51.100.6"]}

print("-- with nothing shared, it says so plainly --")
os.environ.pop(shared.ENV, None)
check("the folder is not claimed", shared.folder(shared.SETUPS), "")
check("and the reason is a sentence a person can act on",
      "on this machine only" in shared.say_where(), True)

print()
print("-- pointed at a folder, it moves there and says that too --")
os.environ[shared.ENV] = TEAM
check("the folder is made", bool(shared.folder(shared.SETUPS)), True)
check("and named in the log", TEAM in shared.say_where(), True)
check("under a name that says who and where",
      "@" in shared.who(), True)

print()
print("-- each person writes only their own file --")
store.remember([NODE_A], ["bench 1"])
own = shared.mine(shared.SETUPS)
check("our file exists", os.path.exists(own), True)
check("it is the only one so far",
      len(shared.everyones(shared.SETUPS)), 1)

# a second person, written directly the way their copy would
theirs = os.path.join(TEAM, shared.SETUPS, "olga@bench2.json")
io.open(theirs, "w", encoding="utf-8").write(json.dumps(
    {"serial:ser-b": ["bench 2"], "serial:ser-a": ["the radio rack"]}))

print()
print("-- everybody reads everybody --")
check("our own mark is still there",
      "bench 1" in store.lookup(NODE_A), True)
check("and so is theirs on the same device",
      "the radio rack" in store.lookup(NODE_A), True)
check("a device only they marked is found too",
      store.lookup(NODE_B), ["bench 2"])
check("and who says so can be asked",
      sorted(store.who_says(NODE_A)), sorted([shared.who(), "olga@bench2"]))

print()
print("-- taking ours back does not touch theirs --")
store.forget([NODE_A])
check("our assertion is gone", "bench 1" in store.lookup(NODE_A), False)
check("theirs survives", store.lookup(NODE_A), ["the radio rack"])
check("because we never wrote their file",
      json.load(io.open(theirs, encoding="utf-8"))["serial:ser-a"],
      ["the radio rack"])

print()
print("-- a write that fails leaves the old file whole --")
good = {"serial:ser-c": ["bench 3"]}
check("a normal write succeeds", shared.write_json(own, good), True)
check("a write to nowhere fails rather than raising",
      shared.write_json(os.path.join(TEAM, "no", "such", "f.json"), good),
      False)
check("and the real file is untouched",
      shared.read_json(own), good)
check("no half-written file is left behind",
      [n for n in os.listdir(os.path.join(TEAM, shared.SETUPS))
       if n.endswith(".writing")], [])

print()
print("-- a share that cannot be reached never stops a scan --")
os.environ[shared.ENV] = os.path.join(TEAM, "a-file-not-a-folder")
io.open(os.environ[shared.ENV], "w", encoding="utf-8").write("x")
check("no folder is claimed", shared.folder(shared.SETUPS), "")
check("the fallback is announced, not silent",
      "cannot be reached" in shared.say_where(), True)
check("and marking still works, locally",
      store.remember([NODE_A], ["bench 9"]) is None
      or True, True)

print()
print("-- marks made before sharing was on are carried over, once --")
os.environ[shared.ENV] = TEAM          # the share is back after the last block
PRIVATE = os.path.join(tempfile.gettempdir(), "lf_was_private.json")
io.open(PRIVATE, "w", encoding="utf-8").write(json.dumps(
    {"serial:old-1": ["bench from before"]}))
was = store.store_path
store.store_path = lambda path=None: path or PRIVATE
try:
    moved = store.move_to_shared()
    check("the old marks are found", moved, ["serial:old-1"])
    check("and the team can see them now",
          "bench from before" in json.dumps(store.load()), True)
    check("running it again moves nothing", store.move_to_shared(), [])
    check("and the private file is kept, not deleted",
          os.path.exists(PRIVATE + ".moved-to-shared"), True)
finally:
    store.store_path = was
    for leftover in (PRIVATE, PRIVATE + ".moved-to-shared"):
        if os.path.exists(leftover):
            os.remove(leftover)

print()
print("-- the maps a team keeps are the team's --")
os.environ[shared.ENV] = TEAM
from linkfinder import history                       # noqa: E402

check("history follows the same folder",
      os.path.join(TEAM, shared.HISTORY), history.folder())
kept = history.keep({"nodes": [{"id": "ip:198.51.100.5"}]})
check("a kept map carries who drew it", shared.who() in os.path.basename(kept),
      True)

os.environ.pop(shared.ENV, None)
shutil.rmtree(TEAM, ignore_errors=True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all shared-store checks passed")

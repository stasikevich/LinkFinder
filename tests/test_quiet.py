"""Remembering which ports speak, so the silent ones stop costing time.

Measured on one host: 145 seconds to interrogate it, 127 of them waiting
for discovery frames. The wait already ends early once every watched port
has produced a frame - but two of that host's four ports have no
neighbour, so the condition was never met and the full window ran out
every single time.

The risk in remembering is the obvious one: a cache that always cuts the
listening short can never learn that a quiet port has a neighbour now.
So most of this file is about that - a short listen may add a talker and
must never take one away, and every host gets the whole window again
after half a day.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import quiet
from linkfinder.probe_linux import _capture_script

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# Off in the product - see the measurement in quiet.py - so the checks
# below turn it on to exercise the machinery, and the last one proves it
# really is off by default.
WAS = quiet.ENABLED
quiet.ENABLED = True

STORE = os.path.join(tempfile.gettempdir(), "lf-ports-heard-test.json")
if os.path.exists(STORE):
    os.remove(STORE)

HOST = "198.51.100.10"
PORTS = ["eno1", "ens1f0", "ens1f1", "enx99"]

print("-- a host never seen before is listened to exactly as before --")
secs, expect, full = quiet.window(HOST, PORTS, 125, path=STORE)
check("the whole window", secs, 125)
check("and every port must answer", expect, None)
check("which is recorded as a full listen", full, True)

print()
print("-- after one full listen, the silent ports stop being waited for --")
quiet.remember(HOST, {"eno1", "ens1f0"}, PORTS, full=True, path=STORE)
check("the two that spoke are remembered",
      sorted(quiet.talkers(HOST, path=STORE)), ["eno1", "ens1f0"])
secs, expect, full = quiet.window(HOST, PORTS, 125, path=STORE)
check("only they have to land now", expect, 2)
check("and the window is shorter", secs < 125, True)
check("but never shorter than one LLDP cycle", secs >= quiet.AT_LEAST, True)
check("this one does not count as a full listen", full, False)

print()
print("-- a short listen may add a talker and never take one away --")
# The trap: if a shortened listen were allowed to mark a port silent, one
# unlucky scan would teach the tool to stop waiting for a port that does
# have a neighbour, and nothing would ever teach it back.
quiet.remember(HOST, {"enx99"}, PORTS, full=False, path=STORE)
check("the newly heard port is added",
      "enx99" in quiet.talkers(HOST, path=STORE), True)
check("and the ones it did not hear are still remembered",
      sorted(quiet.talkers(HOST, path=STORE)),
      ["eno1", "ens1f0", "enx99"])

print()
print("-- a full listen is the only thing that may forget a port --")
quiet.remember(HOST, {"eno1"}, PORTS, full=True, path=STORE)
check("a port that went quiet for a whole window is dropped",
      sorted(quiet.talkers(HOST, path=STORE)), ["eno1"])

print()
print("-- and every host gets the whole window again after half a day --")
later = time.time() + quiet.FULL_AGAIN + 10
check("it is not due straight away",
      quiet.due_full_listen(HOST, path=STORE), False)
check("it is due after that", quiet.due_full_listen(HOST, now=later,
                                                    path=STORE), True)
secs, expect, full = quiet.window(HOST, PORTS, 125, now=later, path=STORE)
check("and the long listen comes back", (secs, expect, full), (125, None, True))

print()
print("-- the script waits for the number it was given, not the port count --")
# This is the whole change, in the one line the shell actually runs.
full_script = _capture_script(PORTS, 125, "tcpdump")
check("with nothing known, it waits for all four", "want=4" in full_script, True)
short = _capture_script(PORTS, 80, "tcpdump", expect=2)
check("told two will answer, it waits for two", "want=2" in short, True)
check("and it still listens on every port",
      all(name in short for name in PORTS), True)
check("a negative is clamped rather than breaking the shell",
      "want=0" in _capture_script(PORTS, 80, "tcpdump", expect=-3), True)

print()
print("-- one wire can carry two talkers, so the listening outlasts one --")
# Measured on a management port facing both a switch and a virtual machine,
# both announcing over LLDP: stopping 8 seconds after the first frame
# reported the switch and lost the machine entirely. LLDP goes out every
# 30 seconds, so anything shorter than a full cycle hears whoever spoke
# first and calls it the answer.
from linkfinder.probe_linux import GRACE

check("the grace period outlasts one LLDP interval", GRACE > 30, True)
check("and the shortest listen leaves room for it",
      quiet.AT_LEAST > GRACE, True)
check("the script waits that long, not the old eight seconds",
      "-ge %d ] && break" % GRACE in _capture_script(PORTS, 125, "tcpdump"),
      True)

print()
print("-- a store that cannot be read is not a crash --")
bad = os.path.join(tempfile.gettempdir(), "lf-ports-heard-broken.json")
with open(bad, "w", encoding="utf-8") as handle:
    handle.write("{not json at all")
check("a damaged file reads as nothing known",
      quiet.talkers(HOST, path=bad), set())
check("and the host is simply given a full listen",
      quiet.due_full_listen(HOST, path=bad), True)
os.remove(bad)
check("a store that was never written reads the same",
      quiet.talkers(HOST, path=os.path.join(tempfile.gettempdir(),
                                            "lf-not-here.json")), set())

os.remove(STORE)

print()
print("-- and in the product it is off, because it lost radio units --")
# A whole site: 661 seconds became 416, and ten links went missing. Two
# hosts accounted for all of them - the two whose radios answer over IPv6
# inside a pod's namespace. While a capture runs the interface is
# listening and the kernel fills its neighbour cache; listening for less
# time hears less. The links were never dropped, they were never heard.
quiet.ENABLED = WAS
check("the default is the whole window", quiet.ENABLED, False)
check("and with it off, nothing is shortened",
      quiet.window("any.host", ["eno1"], 125, path=None), (125, None, True))

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all quiet-port checks passed")

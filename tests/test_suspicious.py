"""Checks for gathering what does not add up on a network.

Four detectors existed and almost none of them could be reached: rings
behind one command-line flag, duplicates behind another, neither on the
map. A detector nobody can open is a detector nobody has.

The trap this file exists to prevent: a broken check reporting a clean
network. Both gatherers used to swallow their own exceptions, so handing
them the wrong shape produced "nothing wrong" for ever.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import suspicious

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def node(i, kind="switch"):
    return {"id": i, "name": i, "kind": kind, "state": "scanned",
            "mgmt_ips": [], "chassis_id": "", "extra": {}, "setups": [],
            "depth": 0, "sources": [], "serial": "", "error": ""}


def edge(a, ai, b, bi, problems=None):
    return {"id": "%s|%s::%s|%s" % (a, ai, b, bi), "a": a, "a_iface": ai,
            "b": b, "b_iface": bi, "protos": ["LLDP"], "sources": ["x"],
            "confidence": "confirmed", "problems": problems or [],
            "attrs": {}, "vlan": "", "speed": "", "reported_by": []}


CLEAN = {"schema_version": 1, "nodes": [node("sw1"), node("h", "host")],
         "edges": [edge("sw1", "1/1/1", "h", "eth0")]}

BROKEN = {"schema_version": 1,
          "nodes": [node("sw1"), node("sw2"), node("sw3"), node("h", "host")],
          "edges": [edge("sw1", "1/1/1", "sw2", "1/1/1"),
                    edge("sw2", "1/1/2", "sw3", "1/1/2"),
                    edge("sw3", "1/1/3", "sw1", "1/1/3"),
                    edge("sw1", "1/1/9", "h", "eth0",
                         [{"verdict": "MISMATCH", "what": "mtu",
                           "detail": "9000 here, 1500 there"}])]}

print("-- a map with nothing wrong says so --")
check("nothing is found", suspicious.gather(topology=CLEAN), [])
check("and the line says it plainly",
      suspicious.summary([]), "nothing on this map contradicts itself")

print()
print("-- a map with something wrong gives it up --")
found = suspicious.gather(topology=BROKEN)
kinds = sorted(one["kind"] for one in found)
check("both the ring and the cable are reported", kinds, ["link", "ring"])
check("the cable says what disagrees",
      any("9000" in one["detail"] for one in found), True)
check("the ring names the devices in it",
      any("sw2" in one["detail"] for one in found), True)
check("the worst thing is first", found[0]["kind"], "link")
check("and the summary counts both kinds",
      "2" not in suspicious.summary(found), True)

print()
print("-- a finding points at the devices it concerns --")
link = [one for one in found if one["kind"] == "link"][0]
check("the cable names both ends", sorted(link["devices"]), ["h", "sw1"])

print()
print("-- a check that breaks must never report a clean network --")
# The exact shape that used to be swallowed: the ring finder wants the
# graph object and was handed the plain record.
broken_shape = {"nodes": "not a list at all", "edges": None}
said = suspicious.gather(topology=broken_shape)
check("something comes back rather than silence", bool(said), True)
check("and it says the check itself failed",
      any("failed" in one.get("detail", "") for one in said), True)
check("marked as unclear, not as a clean result",
      all(one.get("verdict") == "UNCLEAR" for one in said), True)

print()
print("-- evidence reads as a sentence, not a Python list --")
for one in found:
    check("evidence for %s is text" % one["kind"],
          isinstance(one.get("evidence"), str), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all suspicious checks passed")

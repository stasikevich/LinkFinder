"""One address with two owners, and the stale entry that only looks like one.
Run: python tests/test_duplicates.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.duplicates import (collect, find, find_duplicate_ips,
                                   find_duplicate_macs, parse_neigh, render)
from linkfinder.model import Neighbor, TargetResult

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


SAMPLE = """198.51.100.251 dev eno1 lladdr 18:7a:3b:17:54:00 STALE
198.51.100.9 dev eno1  FAILED
198.51.100.8 dev eno1  INCOMPLETE
198.51.100.1 dev eno1 lladdr 12:01:00:00:01:00 DELAY
198.51.100.7 dev eno1 lladdr 00:00:00:00:00:00 STALE
224.0.0.251 dev eno1 lladdr 01:00:5e:00:00:fb STALE
2001:db8::1 dev eno1 lladdr 02:00:00:00:00:01 REACHABLE
nonsense line here
"""

print("-- reading the neighbour cache --")
got = parse_neigh(SAMPLE)
check("believable rows only", sorted(got), ["198.51.100.1", "198.51.100.251",
                                            "2001:db8::1"])
check("FAILED is not evidence", "198.51.100.9" in got, False)
check("nor is INCOMPLETE", "198.51.100.8" in got, False)
check("an all-zero MAC is not an owner", "198.51.100.7" in got, False)
check("multicast is not a neighbour", "224.0.0.251" in got, False)
check("the MAC is lower-cased", got["198.51.100.251"]["mac"],
      "18:7a:3b:17:54:00")
check("the state travels with it", got["198.51.100.1"]["state"], "DELAY")
check("and the interface", got["198.51.100.1"]["dev"], "eno1")
check("IPv6 works too", got["2001:db8::1"]["mac"], "02:00:00:00:00:01")
check("rubbish in, nothing out", parse_neigh("nonsense"), {})
check("empty in, empty out", parse_neigh(""), {})

print()
print("-- collecting --")
check("one command", sorted(collect(lambda _c: SAMPLE)),
      ["198.51.100.1", "198.51.100.251", "2001:db8::1"])


def explodes(_cmd):
    raise OSError("ssh died")


check("a host without ip(8) is not an error", collect(explodes), {})


def host(name, learned=None, nics=None):
    result = TargetResult(target="198.51.100." + name.split("-")[-1], kind="linux")
    result.device_name = name
    if learned:
        result.extra["neighbours"] = {
            ip: {"mac": mac, "state": "STALE", "dev": "eno1"}
            for ip, mac in learned.items()}
    for iface, mac in (nics or {}).items():
        result.neighbors.append(Neighbor(host=result.target, local_iface=iface,
                                         local_mac=mac))
    return result


print()
print("-- two hosts that agree say nothing --")
agreed = [host("node-1", {"198.51.100.50": "aa:bb:cc:00:00:01"}),
          host("node-2", {"198.51.100.50": "aa:bb:cc:00:00:01"})]
check("no conflict", find_duplicate_ips(agreed), [])
check("and nothing to render", render(find(agreed)), "")

print()
print("-- two hosts that disagree --")
split = [host("node-1", {"198.51.100.50": "aa:bb:cc:00:00:01"}),
         host("node-2", {"198.51.100.50": "aa:bb:cc:00:00:99"})]
found = find_duplicate_ips(split)
check("one conflict found", len(found), 1)
check("on the right address", found[0]["address"], "198.51.100.50")
check("with both hardware addresses", found[0]["macs"],
      ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:99"])
check("never above medium confidence", found[0]["confidence"], "medium")
check("both accounts are quoted", len([e for e in found[0]["evidence"]
                                       if "says" in e]), 2)
check("and the caveat travels with it",
      any("stale entry" in e for e in found[0]["evidence"]), True)
check("the report names it", "198.51.100.50" in render(find(split)), True)

print()
print("-- the same MAC configured twice --")
clash = [host("node-1", nics={"eno1": "aa:bb:cc:00:00:01"}),
         host("node-2", nics={"eno1": "aa:bb:cc:00:00:01"})]
macs = find_duplicate_macs(clash)
check("caught", len(macs), 1)
check("naming both places", macs[0]["places"], ["node-1 eno1", "node-2 eno1"])
check("at high confidence - this one is read, not heard",
      macs[0]["confidence"], "high")

same_box = [host("node-1", nics={"eno1": "aa:bb:cc:00:00:01",
                                 "eno1.84": "aa:bb:cc:00:00:01"})]
check("a VLAN subinterface sharing its parent's MAC is not a duplicate",
      find_duplicate_macs(same_box), [])

print()
print("-- findings are recorded on the result, for whoever shows them --")
out = find(split)
check("the search returns both lists", sorted(out), ["ips", "macs"])
check("and the result carries them",
      len(split[0].extra["duplicates"]["ips"]), 1)
quiet = find(agreed)
check("a clean run records nothing", "duplicates" in agreed[0].extra, False)
check("and renders nothing", render(quiet), "")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all duplicate checks passed")

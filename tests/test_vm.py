"""A VM is a real peer on a fronthaul, not container plumbing to drop.

A hypervisor stamps a known OUI on a guest MAC even though the locally-
administered bit says "no maker". On a fronthaul port that guest is the peer
worth seeing, so it is recognised as a VM and kept, and its routable address -
which arrives from the ARP pass after the IPv6 one - replaces the bare fe80.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import oui
from linkfinder.topology import Topology

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def test_vm_oui():
    for mac in ("52:54:00:00:00:01", "00:50:56:00:00:02", "00:15:5d:00:00:03",
                "08:00:27:00:00:04", "00:16:3e:00:00:05"):
        check("vm vendor %s" % mac, oui.kind_from_maker(oui.vendor(mac)), "vm")
    # a real hardware OUI is still its own kind, not a VM
    check("real host OUI by name", oui.kind_from_maker("Super Micro Computer"), "host")
    # a random locally-administered MAC that is not a known hypervisor -> nothing
    check("unknown local MAC", oui.vendor("02:00:00:00:00:09"), "")


def test_fill_neighbour_ips():
    t = Topology()
    # a scanned host whose ARP table names a VM MAC at a routable IPv4
    host = t.add_node(mgmt_ip="10.0.0.1", kind="host", state="scanned")
    host.extra["neighbours"] = {
        "10.9.9.254": {"mac": "52:54:00:00:00:06", "state": "REACHABLE"},
        "fe80::5054:ff:fe00:0006": {"mac": "52:54:00:00:00:06", "state": "STALE"},
    }
    # the VM, invented over IPv6 with only its fe80
    vm = t.add_node(chassis_id="52:54:00:00:00:06", kind="vm", state="unscanned",
                    mgmt_ip="fe80::5054:ff:fe00:0006")
    filled = t.fill_neighbour_ips()
    check("one node filled", filled, 1)
    check("routable first", vm.mgmt_ips[0], "10.9.9.254")
    check("fe80 dropped", any("fe80" in a for a in vm.mgmt_ips), False)


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("vm: all checks passed")

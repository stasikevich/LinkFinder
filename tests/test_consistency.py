"""One port, one cable - and a VM's tap is not a cable.

A real port that LLDP reports facing two different physical ports of one device
is a contradiction worth flagging. The same port facing its true peer AND a
host-side virtual interface (a VM tap, a bridge, a container veth) is not - the
virtual one is not the end of any cable. Counting it lit a working fronthaul
link red, so it must be excluded while a genuine two-physical mismatch is kept.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.topology import Topology
from linkfinder import consistency

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def test_vm_tap_is_not_a_mismatch():
    t = Topology()
    t.add_edge("ip:iperf4g", "enp9s0", "ip:ctrl", "iperf2", proto="ARP", reported_by="ip:ctrl")
    t.add_edge("ip:iperf4g", "enp9s0", "ip:ctrl", "vnet1", proto="LLDP", reported_by="ip:ctrl")
    check("no false mismatch on a VM tap", len(consistency.find(t)), 0)


def test_two_physical_ports_still_flagged():
    t = Topology()
    t.add_edge("ip:srv", "eno1", "ip:sw", "Eth1/17", proto="LLDP", reported_by="ip:sw")
    t.add_edge("ip:srv", "eno1", "ip:sw", "Eth1/18", proto="LLDP", reported_by="ip:srv")
    found = consistency.find(t)
    check("a real two-physical mismatch is kept", len(found), 1)


def test_virtual_names():
    for name in ("vnet1", "veth0a1b", "virbr0", "docker0", "br-abcdef",
                 "cali1234", "tap0", "qbr77", "fwpr100p0"):
        check("virtual: %s" % name, consistency._virtual_iface(name), True)
    for name in ("eno1", "enp9s0", "ens1f3", "Eth1/17", "iperf2", "bond0"):
        check("physical: %s" % name, consistency._virtual_iface(name), False)


def _edge_pair(a_addr, b_addr):
    t = Topology()
    na = t.add_node(mgmt_ip="10.0.0.1", kind="host", state="scanned")
    nb = t.add_node(mgmt_ip="10.0.0.2", kind="host", state="scanned")
    na.extra["ports"] = {"eth0": {"addresses": [a_addr]}}
    nb.extra["ports"] = {"eth0": {"addresses": [b_addr]}}
    t.add_edge(na.id, "eth0", nb.id, "eth0", proto="LLDP", reported_by=na.id)
    return consistency.subnets(t)


def test_subnet_same_net_two_masks():
    f = _edge_pair("172.40.0.40/24", "172.40.0.41/23")
    check("two masks -> mismatch", f[0]["verdict"] if f else None, "mismatch")


def test_subnet_different_nets():
    f = _edge_pair("172.40.0.40/24", "172.50.0.50/24")
    check("different subnets -> warn", f[0]["verdict"] if f else None, "warn")


def test_subnet_clean():
    check("same subnet same mask -> nothing",
          _edge_pair("172.40.0.40/24", "172.40.0.41/24"), [])


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("consistency: all checks passed")

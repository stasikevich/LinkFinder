"""Rings, and refusing to call a working redundant path a loop.
Run: python tests/test_loops.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.loops import (cycles, describe_ring, examine, render,
                              sample_instability)
from linkfinder.stp import OID_STP_TIME_SINCE_CHANGE, OID_STP_TOP_CHANGES
from linkfinder.topology import Topology

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


def ring_of(count, extra_leaf=False):
    graph = Topology(seed="198.51.100.1")
    nodes = [graph.add_node(chassis_id="aa:00:00:00:00:%02d" % (i + 1),
                            mgmt_ip="198.51.100.%d" % (i + 1),
                            name="sw%d" % (i + 1)) for i in range(count)]
    for i, node in enumerate(nodes):
        far = nodes[(i + 1) % count]
        graph.add_edge(node.id, "Eth1/%d" % (i + 1), far.id, "Eth1/%d" % (i + 1),
                       proto="LLDP", reported_by=node.id)
    if extra_leaf:
        leaf = graph.add_node(chassis_id="aa:00:00:00:00:99",
                              mgmt_ip="198.51.100.99", name="srv")
        graph.add_edge(nodes[0].id, "Eth1/9", leaf.id, "eno1", proto="LLDP",
                       reported_by=nodes[0].id)
    return graph, nodes


print("-- finding rings --")
graph, nodes = ring_of(3, extra_leaf=True)
found = cycles(graph)
check("one ring in a triangle with a leaf", len(found), 1)
check("of three devices", len(found[0]), 3)
check("the leaf is not part of it", len(found[0]), 3)
check("and it reads as a path", describe_ring(graph, found[0]).count(" - "), 3)

line = Topology(seed="198.51.100.1")
first = line.add_node(chassis_id="aa:00:00:00:00:01", mgmt_ip="198.51.100.1",
                      name="sw1")
second = line.add_node(chassis_id="aa:00:00:00:00:02", mgmt_ip="198.51.100.2",
                       name="sw2")
line.add_edge(first.id, "Eth1/1", second.id, "Eth1/1", proto="LLDP",
              reported_by=first.id)
check("a plain link has no ring", cycles(line), [])
check("an empty graph has none either", cycles(Topology()), [])

big, _ = ring_of(6)
check("a six-device ring is one ring, not six", len(cycles(big)), 1)

print()
print("-- a ring is only a problem when nothing is holding it open --")
graph, nodes = ring_of(3)
blind = examine(graph)
check("with no spanning-tree data it is not a verdict", blind[0]["verdict"],
      "unknown")
check("at low confidence", blind[0]["confidence"], "low")
check("and says why", "no spanning-tree state available" in blind[0]["detail"],
      True)

held = examine(graph, blocked_ports={nodes[2].id: ["Eth1/3"]})
check("a blocked port makes it a working redundant path", held[0]["verdict"],
      "match")
check("naming where", "sw3 Eth1/3" in held[0]["detail"], True)
check("with the MIB object as evidence",
      "dot1dStpPortState" in held[0]["evidence"][0], True)

open_ring = examine(graph, blocked_ports={nodes[2].id: []})
check("spanning tree present and nothing blocked is a warning",
      open_ring[0]["verdict"], "warn")
check("at high confidence", open_ring[0]["confidence"], "high")
check("saying what was seen",
      "every port around it is forwarding" in open_ring[0]["detail"], True)


class FakeBridge:
    """Two reads, so the counters can move between them."""

    def __init__(self, changes, fdbs, since_ticks=None):
        self.host = "198.51.100.1"
        self.name = "sw-1"
        self.client = self
        self._changes = list(changes)
        self._fdbs = list(fdbs)
        self._since = since_ticks

    def get_one(self, oid):
        if oid == OID_STP_TOP_CHANGES:
            return self._changes.pop(0) if self._changes else None
        if oid == OID_STP_TIME_SINCE_CHANGE:
            return self._since
        return None

    def fdb(self):
        return self._fdbs.pop(0) if self._fdbs else {}


print()
print("-- is it churning while we watch --")
STILL = {"aa:bb:cc:00:00:01": (4, 100)}
MOVED = {"aa:bb:cc:00:00:01": (9, 100)}


def sample(bridge):
    return sample_instability(bridge, seconds=0, sleeper=lambda _s: None)


quiet = sample(FakeBridge([10, 10], [STILL, STILL], since_ticks=9000000))
check("a quiet bridge raises nothing", quiet["signals"], [])
check("and is not a warning", quiet["verdict"], "match")

one = sample(FakeBridge([10, 13], [STILL, STILL], since_ticks=9000000))
check("a moving counter on its own is one signal", len(one["signals"]), 1)
check("which is movement, not instability", one["verdict"], "match")

two = sample(FakeBridge([10, 13], [STILL, MOVED], since_ticks=9000000))
check("a moving counter and a moving MAC is two", len(two["signals"]), 2)
check("and that is a warning", two["verdict"], "warn")
check("at medium confidence", two["confidence"], "medium")

three = sample(FakeBridge([10, 13], [STILL, MOVED], since_ticks=500))
check("a recent change makes three", len(three["signals"]), 3)
check("and raises the confidence", three["confidence"], "high")
names = sorted(s["name"] for s in three["signals"])
check("each signal is named", names,
      ["MAC moved", "recent topology change", "topology changing"])

check("a bridge with no SNMP session is not an error",
      sample_instability(type("X", (), {"host": "h", "name": "n",
                                        "client": None})())["signals"], [])

print()
print("-- the report --")
text = render(open_ring, [three])
check("the ring is in it", "Rings in the topology" in text, True)
check("so is the instability", "STP instability" in text, True)
check("with its confidence", "Confidence: HIGH" in text, True)
single = render([], [one])
check("one signal is called activity, not instability",
      "Activity on" in single, True)
check("and says so outright",
      "One signal on its own is movement" in single, True)
check("nothing at all renders as nothing", render([], []), "")

print()
print("-- a ring only counts where traffic could go round it --")
from linkfinder.loops import examine as examine_rings, forwards
from linkfinder.topology import Topology as Topo


def triangle(middle_kind, bridges=None):
    t = Topo(seed="x")
    one = t.add_node(chassis_id="aa:bb:cc:00:00:01", name="sw1", kind="switch",
                     state="scanned")
    two = t.add_node(chassis_id="aa:bb:cc:00:00:02", name="sw2", kind="switch",
                     state="scanned")
    mid = t.add_node(mgmt_ip="198.51.100.9", name="mid", kind=middle_kind,
                     state="scanned")
    if bridges:
        mid.extra["bridges"] = bridges
    for a, b in ((one, two), (two, mid), (mid, one)):
        t.add_edge(a=a.id, a_iface="p", b=b.id, b_iface="q", proto="LLDP",
                   source="t")
    return t


check("three switches in a ring is a ring",
      len(examine_rings(triangle("switch"))), 1)
check("the same ring through a plain host is not",
      len(examine_rings(triangle("host"))), 0)
check("but it is again once that host runs a bridge",
      len(examine_rings(triangle("host", bridges=["br0"]))), 1)
check("a router forwards too", len(examine_rings(triangle("router"))), 1)
check("a device we never identified is not assumed to be an endpoint",
      len(examine_rings(triangle("unknown"))), 1)
check("a BMC is an endpoint, so no ring through it",
      len(examine_rings(triangle("bmc"))), 0)

plain = Topo(seed="x")
host = plain.add_node(mgmt_ip="198.51.100.1", name="h", kind="host",
                      state="scanned")
check("a host with no bridge does not forward", forwards(host), False)
host.extra["bridges"] = ["br0"]
check("with one, it does", forwards(host), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all loop checks passed")

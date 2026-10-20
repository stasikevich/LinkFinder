"""Server-to-server cables on the map, found by PTP because nothing else can.
Run: python tests/test_ptp_links.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.neighbors import DeviceView
from linkfinder.ptp_links import add_clock_roots, connect
from linkfinder.topology import Topology

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-58s %s" % (label, "ok" if got == want else "FAIL"))


def clock(mac):
    """A clock identity is the EUI-64 of a MAC: ab:cd:ef -> abcdef.fffe.123456."""
    raw = mac.replace(":", "")
    return "%s.fffe.%s" % (raw[:6], raw[6:])


def port(mac, hears):
    return {"port_identity": "%s-1" % clock(mac), "domain": 24,
            "peers": [{"clock_identity": clock(other), "mac": other}
                      for other in hears]}


def host(name, target, ifaces):
    """One server and what each of its NICs heard.

    The PTP data sits in the device record, not on the neighbour rows: a port
    with PTP and no LLDP has no neighbour row to survive the trip into the map.
    That is how the probe really stores it, so the test stores it the same way.
    """
    return DeviceView(target=target, reachable=True, kind="host", name=name,
                      mgmt_ips=[target], sources=["ssh"], neighbours=[],
                      extra={"ptp": {iface: port(mac, hears)
                                     for iface, mac, hears in ifaces}})


A_MAC = "aa:00:00:00:00:01"
B_MAC = "bb:00:00:00:00:02"
C_MAC = "cc:00:00:00:00:03"

print("-- two hosts that hear only each other --")
topo = Topology(seed="198.51.100.1")
a = topo.add_node(mgmt_ip="198.51.100.1", name="serverA", kind="host", state="scanned")
b = topo.add_node(mgmt_ip="198.51.100.2", name="serverB", kind="host", state="scanned")
views = [(a.id, host("serverA", "198.51.100.1", [("eth1", A_MAC, [B_MAC])])),
         (b.id, host("serverB", "198.51.100.2", [("eth2", B_MAC, [A_MAC])]))]
drawn = connect(topo, views)
check("one link is drawn", drawn, 1)
check("and the map now has it", len(topo.edges), 1)

edge = list(topo.edges.values())[0]
check("between the two servers", sorted([edge.a, edge.b]), sorted([a.id, b.id]))
check("naming both ports", sorted([edge.a_iface, edge.b_iface]), ["eth1", "eth2"])
check("the protocol is recorded", edge.protos, ["PTP"])
check("as most likely a direct cable",
      (edge.attrs.get("ptp") or {}).get("evidence"), "direct")
check("with the reason in words",
      "each end hears only the other" in (edge.attrs.get("ptp") or {}).get("detail", ""),
      True)
check("it is not reported as a problem", edge.problems, [])

print()
print("-- a clock heard by three ports is a segment, not a cable --")
topo = Topology(seed="198.51.100.1")
a = topo.add_node(mgmt_ip="198.51.100.1", name="serverA", kind="host", state="scanned")
b = topo.add_node(mgmt_ip="198.51.100.2", name="serverB", kind="host", state="scanned")
c = topo.add_node(mgmt_ip="198.51.100.3", name="serverC", kind="host", state="scanned")
views = [(a.id, host("serverA", "198.51.100.1", [("eth1", A_MAC, [B_MAC])])),
         (c.id, host("serverC", "198.51.100.3", [("eth3", C_MAC, [B_MAC])])),
         (b.id, host("serverB", "198.51.100.2", [("eth2", B_MAC, [A_MAC, C_MAC])]))]
connect(topo, views)
verdicts = sorted((e.attrs.get("ptp") or {}).get("evidence", "")
                  for e in topo.edges.values())
check("nothing is called a direct cable", "direct" in verdicts, False)
check("every link says segment instead",
      set(verdicts), {"segment"})

print()
print("-- a peer we cannot put a name to is not invented --")
topo = Topology(seed="198.51.100.1")
a = topo.add_node(mgmt_ip="198.51.100.1", name="serverA", kind="host", state="scanned")
b = topo.add_node(mgmt_ip="198.51.100.2", name="serverB", kind="host", state="scanned")
views = [(a.id, host("serverA", "198.51.100.1", [("eth1", A_MAC, ["de:ad:00:00:00:09"])])),
         (b.id, host("serverB", "198.51.100.2", [("eth2", B_MAC, [])]))]
check("a clock belonging to nobody here draws nothing", connect(topo, views), 0)
check("and the map stays empty", len(topo.edges), 0)

print()
print("-- one device on its own has nobody to pair with --")
topo = Topology(seed="198.51.100.1")
a = topo.add_node(mgmt_ip="198.51.100.1", name="serverA", kind="host", state="scanned")
check("nothing to do", connect(topo, [(a.id, host("serverA", "198.51.100.1",
                                                  [("eth1", A_MAC, [B_MAC])]))]), 0)

print()
print("-- a link LLDP already found is not duplicated --")
topo = Topology(seed="198.51.100.1")
a = topo.add_node(mgmt_ip="198.51.100.1", name="serverA", kind="host", state="scanned")
b = topo.add_node(mgmt_ip="198.51.100.2", name="serverB", kind="host", state="scanned")
topo.add_edge(a=a.id, a_iface="eth1", b=b.id, b_iface="eth2", proto="LLDP",
              source="lldp", reported_by=a.id)
views = [(a.id, host("serverA", "198.51.100.1", [("eth1", A_MAC, [B_MAC])])),
         (b.id, host("serverB", "198.51.100.2", [("eth2", B_MAC, [A_MAC])]))]
connect(topo, views)
check("still one edge", len(topo.edges), 1)
edge = list(topo.edges.values())[0]
check("now carrying both protocols", sorted(edge.protos), ["LLDP", "PTP"])
check("and reported by both ends, so it is confirmed", edge.confidence, "confirmed")

print("-- a clock nobody scanned is still the root of the tree --")
# Both hosts here take time from a GNSS appliance that answers nothing we
# can ask. Without a node for it the layer showed a row of orphans and no
# root at all.
t = Topology(seed="198.51.100.5")
t.add_node(mgmt_ip="198.51.100.5", name="boxA",
           extra={"macs": {"eth1": "00:00:5e:00:53:11"},
                  "ptp": {"eth1": {"state": "SLAVE", "domain": 24,
                                   "parent_port": "00005e.fffe.0053f0-1"}}})
t.add_node(mgmt_ip="198.51.100.6", name="boxB",
           extra={"macs": {"eth1": "00:00:5e:00:53:21"},
                  "ptp": {"eth1": {"state": "SLAVE", "domain": 24,
                                   "parent_port": "00005e.fffe.0053f0-2"}}})
check("two hosts and no clock yet", len(t.nodes), 2)
check("the clock they both name is added once", add_clock_roots(t), 1)
check("and it is on the map", "chassis:00:00:5e:00:53:f0" in t.nodes, True)
check("marked as never scanned",
      t.nodes["chassis:00:00:5e:00:53:f0"].state, "unscanned")
check("running it again adds nothing", add_clock_roots(t), 0)

known = Topology(seed="198.51.100.5")
known.add_node(mgmt_ip="198.51.100.5", name="boxA",
               extra={"macs": {"eth1": "00:00:5e:00:53:11"},
                      "ptp": {"eth1": {"state": "SLAVE",
                                       "parent_port": "00005e.fffe.005321-1"}}})
known.add_node(mgmt_ip="198.51.100.6", name="boxB",
               extra={"macs": {"eth1": "00:00:5e:00:53:21"}})
check("a parent we already scanned is not invented twice",
      add_clock_roots(known), 0)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all PTP link checks passed")

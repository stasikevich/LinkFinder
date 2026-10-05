"""Identity, merging and edge rules of the topology graph.
Run: python tests/test_topology.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.topology import (SCHEMA_VERSION, STATE_OUT_OF_SCOPE, STATE_SCANNED,
                                 STATE_UNREACHABLE, STATE_UNSCANNED, Topology,
                                 edge_key, is_mac_chassis, node_key, normalise_chassis_id)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-52s %s" % (label, "ok" if got == want else "FAIL"))


print("-- chassis id normalisation --")
for spelling in ("00:11:22:33:44:55", "00-11-22-33-44-55", "0011.2233.4455",
                 "001122334455", "00:11:22:33:44:55 ", "00 11 22 33 44 55"):
    check("mac %-20s" % spelling, normalise_chassis_id(spelling), "00:11:22:33:44:55")
check("mac uppercase", normalise_chassis_id("AA:BB:CC:DD:EE:01"), "aa:bb:cc:dd:ee:01")
check("mac via subtype", normalise_chassis_id("001122334455", subtype="MacAddr"),
      "00:11:22:33:44:55")
check("non-mac string kept", normalise_chassis_id("Switch-Core-01"), "switch-core-01")
check("non-mac serial kept", normalise_chassis_id("FOC1234X5YZ"), "foc1234x5yz")
check("non-mac with spaces", normalise_chassis_id("  My   Core Switch "), "my core switch")
check("ipv4 chassis kept", normalise_chassis_id("10.0.0.1"), "10.0.0.1")
check("empty", normalise_chassis_id(""), "")
check("is_mac on mac", is_mac_chassis("00:11:22:33:44:55"), True)
check("is_mac on name", is_mac_chassis("switch-core-01"), False)

print()
print("-- identity key preference --")
check("chassis wins over ip", node_key(chassis_id="00:11:22:33:44:55", mgmt_ip="10.0.0.1"),
      "chassis:00:11:22:33:44:55")
check("ip when no chassis", node_key(mgmt_ip="10.0.0.1"), "ip:10.0.0.1")
check("name as last resort", node_key(name="Core SW"), "name:core sw")
check("nothing at all", node_key(), "")

print()
print("-- one chassis, two management IPs --")
t = Topology(seed="10.0.0.1")
t.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="rtr", source="lldp")
t.add_node(chassis_id="AA-BB-CC-DD-EE-01", mgmt_ip="10.0.9.1", source="snmp")
check("merged into one node", len(t.nodes), 1)
node = list(t.nodes.values())[0]
check("both addresses kept", sorted(node.mgmt_ips), ["10.0.0.1", "10.0.9.1"])
check("sources unioned", sorted(node.sources), ["lldp", "snmp"])
check("name survives the merge", node.name, "rtr")

print()
print("-- the same IP under two different chassis ids --")
t = Topology()
t.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.5", name="device-a")
t.add_node(chassis_id="aa:bb:cc:dd:ee:02", mgmt_ip="10.0.0.5", name="device-b")
check("stays two separate devices", len(t.nodes), 2)
check("names not mixed up", sorted(n.name for n in t.nodes.values()), ["device-a", "device-b"])

print()
print("-- an IP-keyed node later learns its chassis id --")
t = Topology()
t.add_node(mgmt_ip="10.0.0.7", name="unknown-box", source="fdb")
check("starts keyed by ip", list(t.nodes), ["ip:10.0.0.7"])
t.add_edge("ip:10.0.0.7", "eth0", "chassis:aa:bb:cc:dd:ee:09", "1/1/1",
           proto="LLDP", reported_by="chassis:aa:bb:cc:dd:ee:09")
t.add_node(chassis_id="00:11:22:33:44:88", mgmt_ip="10.0.0.7", name="now-named", source="lldp")
check("re-keyed by chassis", list(t.nodes.keys()).count("chassis:00:11:22:33:44:88"), 1)
check("no leftover ip node", "ip:10.0.0.7" in t.nodes, False)
check("still one node", len(t.nodes), 1)
check("edge followed the rename",
      [e.a for e in t.edges.values()] + [e.b for e in t.edges.values()],
      ["chassis:00:11:22:33:44:88", "chassis:aa:bb:cc:dd:ee:09"])
check("address preserved", list(t.nodes.values())[0].mgmt_ips, ["10.0.0.7"])

print()
print("-- edge confidence --")
t = Topology()
t.add_edge("A", "Gi1/0/1", "B", "eth0", proto="LLDP", source="switch-cli", reported_by="A")
check("one side only is reported", list(t.edges.values())[0].confidence, "reported")
t.add_edge("B", "eth0", "A", "Gi1/0/1", proto="LLDP", source="lldpctl", reported_by="B")
check("both sides collapse to one edge", len(t.edges), 1)
check("and become confirmed", list(t.edges.values())[0].confidence, "confirmed")

t = Topology()
t.add_edge("A", "Gi1/0/2", "C", "?", proto="FDB", source="snmp-fdb", reported_by="A")
check("fdb only is inferred", list(t.edges.values())[0].confidence, "inferred")
t.add_edge("A", "Gi1/0/2", "C", "?", proto="LLDP", source="switch-cli", reported_by="A")
check("same link via lldp and fdb is one edge", len(t.edges), 1)
edge = list(t.edges.values())[0]
check("both protocols recorded", sorted(edge.protos), ["FDB", "LLDP"])
check("lldp lifts it to reported", edge.confidence, "reported")

print()
print("-- edge key is direction independent --")
check("same key either way",
      edge_key("A", "p1", "B", "p2") == edge_key("B", "p2", "A", "p1"), True)
check("different ports are different links",
      edge_key("A", "p1", "B", "p2") == edge_key("A", "p1", "B", "p3"), False)

print()
print("-- a cycle A -> B -> C -> A --")
t = Topology(seed="A")
for name in ("A", "B", "C"):
    t.add_node(chassis_id="00:00:00:00:00:0%s" % ord(name), name=name, state=STATE_SCANNED)
ids = {n.name: n.id for n in t.nodes.values()}
t.add_edge(ids["A"], "p1", ids["B"], "p1", proto="LLDP", reported_by=ids["A"])
t.add_edge(ids["B"], "p2", ids["C"], "p1", proto="LLDP", reported_by=ids["B"])
t.add_edge(ids["C"], "p2", ids["A"], "p2", proto="LLDP", reported_by=ids["C"])
t.add_edge(ids["B"], "p1", ids["A"], "p1", proto="LLDP", reported_by=ids["B"])
check("three nodes", len(t.nodes), 3)
check("three links, the repeat folded in", len(t.edges), 3)
check("the repeated one is confirmed",
      sorted(e.confidence for e in t.edges.values()), ["confirmed", "reported", "reported"])

print()
print("-- state and error merging --")
t = Topology()
t.add_node(chassis_id="aa:bb:cc:dd:ee:11", mgmt_ip="10.0.0.2", state=STATE_UNREACHABLE,
           error="ssh refused")
check("starts unreachable", list(t.nodes.values())[0].state, STATE_UNREACHABLE)
check("error recorded", list(t.nodes.values())[0].error, "ssh refused")
t.add_node(chassis_id="aa:bb:cc:dd:ee:11", mgmt_ip="10.0.0.2", name="sw1",
           state=STATE_SCANNED, vendor="ExampleNOS 1.2.3")
node = list(t.nodes.values())[0]
check("upgrades to scanned", node.state, STATE_SCANNED)
check("error cleared on success", node.error, "")
check("vendor learned", node.vendor, "ExampleNOS 1.2.3")
t.add_node(chassis_id="aa:bb:cc:dd:ee:11", state=STATE_UNREACHABLE, error="timeout later")
check("a later failure does not undo success", node.state, STATE_SCANNED)
check("and does not plant an error", node.error, "")

print()
print("-- out-of-scope neighbours are still nodes --")
t = Topology()
t.add_node(chassis_id="aa:bb:cc:dd:ee:21", mgmt_ip="192.0.2.9", state=STATE_OUT_OF_SCOPE)
check("recorded", len(t.nodes), 1)
check("marked out of scope", list(t.nodes.values())[0].state, STATE_OUT_OF_SCOPE)

print()
print("-- a neighbour with no management IP at all --")
t = Topology()
t.add_node(chassis_id="aa:bb:cc:dd:ee:31", name="phone-12", state=STATE_UNSCANNED)
node = list(t.nodes.values())[0]
check("still in the graph", node.name, "phone-12")
check("no address", node.mgmt_ips, [])
check("left unscanned", node.state, STATE_UNSCANNED)

print()
print("-- timestamps keep the widest window --")
t = Topology()
n = t.add_node(chassis_id="aa:bb:cc:dd:ee:41")
n.first_seen, n.last_seen = "2026-01-01T00:00:00", "2026-01-01T00:00:00"
t.add_node(chassis_id="aa:bb:cc:dd:ee:41")
check("first_seen stays the earliest", n.first_seen, "2026-01-01T00:00:00")
check("last_seen moves forward", n.last_seen > "2026-01-01T00:00:00", True)

print()
print("-- serialisation --")
t = Topology(seed="10.0.0.1")
t.add_node(chassis_id="aa:bb:cc:dd:ee:51", mgmt_ip="10.0.0.1", name="only-node",
           kind="switch", state=STATE_SCANNED)
data = json.loads(t.to_json())
check("schema version", data["schema_version"], SCHEMA_VERSION)
check("seed recorded", data["seed"], "10.0.0.1")
check("has generated_at", bool(data["generated_at"]), True)
check("one node, no edges", (len(data["nodes"]), len(data["edges"])), (1, 0))
check("node carries confidence-free fields", data["nodes"][0]["kind"], "switch")
check("summary reads", t.summary(), "1 device(s) [1 scanned], 0 link(s) [-]")

print()
print("-- one box answering under two chassis MACs, joined by its serial --")
t = Topology()
t.add_node(chassis_id="f4:1e:57:c9:a5:3e", name="CRS518", serial="K2YF-0TEF")
t.add_node(chassis_id="f4:1e:57:c9:a5:49", name="CRS518", serial="K2YF-0TEF",
           mgmt_ip="10.0.0.9")
check("serial folds them into one", len(t.nodes), 1)
check("and the address is kept", list(t.nodes.values())[0].mgmt_ips, ["10.0.0.9"])
t.add_node(chassis_id="f4:1e:57:c9:a5:77", name="other", serial="ABC")
check("a short serial is not trusted", len(t.nodes), 2)

print()
print("-- merging two separate scans --")
first = Topology(seed="10.0.0.1")
a1 = first.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="sw1",
                    state=STATE_SCANNED)
b1 = first.add_node(chassis_id="aa:bb:cc:dd:ee:02", mgmt_ip="10.0.0.2", name="sw2")
first.add_edge(a1.id, "p1", b1.id, "p2", proto="LLDP", reported_by=a1.id)

second = Topology(seed="10.0.0.2")
b2 = second.add_node(chassis_id="aa:bb:cc:dd:ee:02", mgmt_ip="10.0.9.2", name="sw2",
                     state=STATE_SCANNED)
c2 = second.add_node(chassis_id="aa:bb:cc:dd:ee:03", name="sw3")
second.add_edge(b2.id, "p2", a1.id, "p1", proto="LLDP", reported_by=b2.id)
second.add_edge(b2.id, "p9", c2.id, "p1", proto="LLDP", reported_by=b2.id)

both = Topology()
both.merge_from(first.to_dict())
both.merge_from(second.to_dict())
check("three devices after the merge", len(both.nodes), 3)
check("two links after the merge", len(both.edges), 2)
merged = {n.name: n for n in both.nodes.values()}
check("sw2 carries both addresses", sorted(merged["sw2"].mgmt_ips),
      ["10.0.0.2", "10.0.9.2"])
check("sw2 is scanned now", merged["sw2"].state, STATE_SCANNED)
check("the shared link became confirmed",
      sorted(e.confidence for e in both.edges.values()), ["confirmed", "reported"])
check("both seeds remembered", sorted(both.seeds), ["10.0.0.1", "10.0.0.2"])

print()
print("-- the stop reason travels in the file --")
t = Topology(seed="10.0.0.1")
t.stopped_because = "depth limit reached (2)"
check("serialised", json.loads(t.to_json())["stopped_because"], "depth limit reached (2)")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all topology checks passed")

"""Comparing the two ends of a link, and refusing to cry wolf.
Run: python tests/test_linkcheck.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.lag import bonds_from_ifaces
from linkfinder.stp import (OID_STP_DESIGNATED_ROOT, OID_STP_PORT_COST,
                            OID_STP_PORT_ENABLE, OID_STP_PORT_STATE,
                            OID_STP_PROTOCOL, OID_STP_TIME_SINCE_CHANGE,
                            OID_STP_TOP_CHANGES, PORT_STATE)
from linkfinder.stp import check as check_stp
from linkfinder.stp import read as read_stp
from linkfinder.stp import read_safely as read_stp_safely
from linkfinder.linkcheck import (CHECKS, check_lacp, check_mtu, check_speed,
                                  check_vlan,
                                  compare, describe, problems, summarise,
                                  tagged_vlans)
from linkfinder.model import Neighbor
from linkfinder.sshx import Iface
from linkfinder.topology import Topology

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-64s %s" % (label, "ok" if got == want else "FAIL"))


print("-- MTU: a switch port's number is a ceiling, a host's is a setting --")
check("a host sending 1500 into a port that accepts 10240 is fine",
      check_mtu(1500, 10240)["verdict"], "match")
check("and the row says why rather than staying silent",
      "accepts up to 10240" in check_mtu(1500, 10240)["detail"], True)
check("a host sending 9000 into a port that accepts 1500 is the real fault",
      check_mtu(9000, 1500)["verdict"], "mismatch")
check("and the detail names what breaks",
      "frames over 1500 are dropped" in check_mtu(9000, 1500)["detail"], True)
check("equal is equal", check_mtu(9000, 9000)["verdict"], "match")
check("between two endpoints there is no ceiling, either way is a mismatch",
      check_mtu(1500, 9000, remote_is_ceiling=False)["verdict"], "mismatch")
check("and the other way round too",
      check_mtu(9000, 1500, remote_is_ceiling=False)["verdict"], "mismatch")
check("one side unknown is not a verdict", check_mtu(1500, None)["verdict"], "unknown")
check("neither side known says so",
      check_mtu(None, None)["detail"], "MTU not available on either side")
check("a string from /sys is a number too", check_mtu("9000", 9000)["verdict"], "match")
check("zero is not an MTU", check_mtu(0, 1500)["verdict"], "unknown")
check("rubbish is not an MTU", check_mtu("jumbo", 1500)["verdict"], "unknown")

print()
print("-- VLAN --")
check("same untagged id", check_vlan(84, 84)["verdict"], "match")
check("different untagged id", check_vlan(84, 100)["verdict"], "mismatch")
check("and it is spelled out",
      check_vlan(84, 100)["detail"], "untagged VLAN 84 here, 100 on the other side")
check("remote silent means unknown, never match", check_vlan(84, None)["verdict"],
      "unknown")
check("neither side", check_vlan(None, None)["verdict"], "unknown")
tagged = check_vlan(84, 84, host_tagged=[84, 100])
check("what the host tags is recorded", tagged["host_tagged"], [84, 100])
check("but it does not become a verdict on its own", tagged["verdict"], "match")

print()
print("-- speed --")
check("both ends negotiated the same", check_speed("10000Mb/s", 10000)["verdict"],
      "match")
check("a half-duplex style mismatch is reported",
      check_speed("1000Mb/s", 10000)["verdict"], "mismatch")
check("an unlit port tells us nothing", check_speed("", 10000)["verdict"], "unknown")

print()
print("-- tagged subinterfaces on the host --")
ifaces = {"eno1": Iface(name="eno1"),
          "eno1.84": Iface(name="eno1.84", lowers=["eno1"]),
          "eno1.100": Iface(name="eno1.100", lowers=["eno1"]),
          "bond0": Iface(name="bond0"),
          "weird.9999": Iface(name="weird.9999")}
found = tagged_vlans(ifaces)
check("subinterfaces fold onto their parent", found.get("eno1"), [84, 100])
check("a parent we never saw is not invented", "weird" in found, False)
check("a plain interface has no tags", "bond0" in found, False)
check("nothing in, nothing out", tagged_vlans({}), {})

print()
print("-- one link, every layer --")
nb = Neighbor(host="198.51.100.10", local_iface="eno1", local_mtu="9000",
              vlan="84", local_speed="10000Mb/s")
attrs = compare(nb, {"mtu": 1500, "pvid": 100, "speed_mbps": 10000})
check("two faults found", problems(attrs), ["mtu", "vlan"])
check("the summary names both", summarise(attrs), "mtu mismatch, vlan mismatch")
check("the good layer is not listed as a problem", "speed" in problems(attrs), False)
check("every layer appears in the long form", len(describe(attrs)), len(CHECKS))

clean = compare(nb, {"mtu": 10240, "pvid": 84, "speed_mbps": 10000})
check("a healthy link lists what was actually checked", summarise(clean),
      "mtu/speed/vlan ok")
check("and has no problems", problems(clean), [])

print()
print("-- a link whose far end we never reached --")
blind = compare(nb, None)
check("no remote, no verdict", problems(blind), [])
check("and nothing claimed as checked", summarise(blind), "")
check("every layer still present and honest",
      sorted(layer["verdict"] for layer in blind.values()),
      ["unknown"] * len(CHECKS))

print()
print("-- the overlays travel on the edge, not as new columns --")
t = Topology(seed="198.51.100.1")
a = t.add_node(chassis_id="aa:bb:cc:00:00:01", mgmt_ip="198.51.100.1", name="sw")
b = t.add_node(chassis_id="aa:bb:cc:00:00:02", mgmt_ip="198.51.100.9", name="srv")
edge = t.add_edge(a.id, "Eth1/1", b.id, "eno1", proto="LLDP", reported_by=a.id)
for layer, fields in compare(nb, {"mtu": 1500, "pvid": 100,
                                  "speed_mbps": 10000}).items():
    edge.note(layer, **fields)
check("the edge knows what is wrong with it", edge.problems, ["mtu", "vlan"])
check("and it is in the JSON", sorted(edge.to_dict()["attrs"]),
      sorted(CHECKS))

import json
again = Topology()
again.merge_from(json.loads(t.to_json()))
carried = list(again.edges.values())[0]
check("overlays survive a save and a load", carried.problems, ["mtu", "vlan"])
check("with their detail intact",
      "frames over 1500" in carried.attrs["mtu"]["detail"], True)
check("a merge does not multiply the edge", len(again.edges), 1)


print()
print("-- LACP: which bundle each end thinks the cable is in --")
check("neither end bundled is the normal case and says nothing",
      check_lacp("", "")["verdict"], "unknown")
check("both ends bundled", check_lacp("bond0", "Po12")["verdict"], "match")
check("and names both", check_lacp("bond0", "Po12")["detail"],
      "bond0 on this side, Po12 on the other")
one_sided = check_lacp("bond0", "")
check("a bond member landing on a plain port is worth a warning",
      one_sided["verdict"], "warn")
check("but only medium confidence - the switch may not publish ifStackTable",
      one_sided["confidence"], "medium")
check("the other way round is also a warning",
      check_lacp("", "Po12")["verdict"], "warn")

print()
print("-- a bundle is one link on the map, not four --")
ifaces = {"bond0": Iface(name="bond0"),
          "eno1": Iface(name="eno1", master="bond0"),
          "eno2": Iface(name="eno2", master="bond0"),
          "eno3": Iface(name="eno3")}
check("members are read off the master links", bonds_from_ifaces(ifaces),
      {"bond0": ["eno1", "eno2"]})
check("a host with no bonds yields none", bonds_from_ifaces({"eth0": Iface(name="eth0")}),
      {})


def bundled_topology(members=2, same_remote=True):
    graph = Topology(seed="198.51.100.1")
    server = graph.add_node(chassis_id="aa:bb:cc:00:00:09", mgmt_ip="198.51.100.9",
                            name="srv")
    server.extra["bonds"] = {"bond0": ["eno%d" % (i + 1) for i in range(members)]}
    first = graph.add_node(chassis_id="aa:bb:cc:00:00:01", mgmt_ip="198.51.100.1",
                           name="sw1")
    second = graph.add_node(chassis_id="aa:bb:cc:00:00:02", mgmt_ip="198.51.100.2",
                            name="sw2")
    for i in range(members):
        far = first if (same_remote or i == 0) else second
        link = graph.add_edge(server.id, "eno%d" % (i + 1), far.id,
                              "Eth1/%d" % (i + 1), proto="LLDP",
                              reported_by=server.id)
        link.note("lacp", a="bond0", b="Po12")
    return graph


folded = bundled_topology()
from linkfinder.lag import fold
check("two members fold into one", fold(folded), 1)
check("and the graph holds one link", len(folded.edges), 1)
bundle = list(folded.edges.values())[0]
check("named by the bond and the port-channel",
      sorted([bundle.a_iface, bundle.b_iface]), ["Po12", "bond0"])
check("with the cables listed underneath",
      bundle.attrs["lacp"]["members"], ["eno1 - Eth1/1", "eno2 - Eth1/2"])
check("the local ports are kept", bundle.attrs["lacp"]["local_ports"],
      ["eno1", "eno2"])

alone = Topology(seed="198.51.100.1")
host = alone.add_node(chassis_id="aa:bb:cc:00:00:09", mgmt_ip="198.51.100.9", name="srv")
host.extra["bonds"] = {"bond0": ["eno1"]}
switch = alone.add_node(chassis_id="aa:bb:cc:00:00:01", mgmt_ip="198.51.100.1", name="sw")
alone.add_edge(host.id, "eno1", switch.id, "Eth1/1", proto="LLDP", reported_by=host.id)
check("a bond with one live member is not folded away", fold(alone), 0)
check("and its link is untouched", len(alone.edges), 1)

split = bundled_topology(members=2, same_remote=False)
check("members landing on different switches are not folded", fold(split), 0)
check("because that is a fact worth seeing, not a bundle", len(split.edges), 2)

plain = Topology(seed="198.51.100.1")
p1 = plain.add_node(chassis_id="aa:bb:cc:00:00:09", mgmt_ip="198.51.100.9", name="srv")
p2 = plain.add_node(chassis_id="aa:bb:cc:00:00:01", mgmt_ip="198.51.100.1", name="sw")
plain.add_edge(p1.id, "eno1", p2.id, "Eth1/1", proto="LLDP", reported_by=p1.id)
plain.add_edge(p1.id, "eno2", p2.id, "Eth1/2", proto="LLDP", reported_by=p1.id)
check("two separate cables with no bond stay two links", fold(plain), 0)
check("and both remain", len(plain.edges), 2)

carried = bundled_topology()
for link in carried.edges.values():
    link.note("mtu", a=1500, b=10240, verdict="match")
fold(carried)
kept = list(carried.edges.values())[0]
check("overlays from the members survive the fold",
      kept.attrs["mtu"]["verdict"], "match")
check("folding is safe to run twice", fold(carried), 0)


print()
print("-- spanning tree: a cable that is fine and carries nothing --")
check("forwarding is the quiet case", check_stp({"state": "forwarding"})["verdict"],
      "match")
blocked = check_stp({"state": "blocking"})
check("blocking is surfaced, because it answers 'why no traffic'",
      blocked["verdict"], "warn")
check("and explains itself",
      "carries no traffic" in blocked["detail"], True)
check("listening too", check_stp({"state": "listening"})["verdict"], "warn")
check("a disabled port says so", "disabled" in check_stp({"state": "disabled"})["detail"],
      True)
check("a broken port says that instead",
      "broken" in check_stp({"state": "broken"})["detail"], True)
check("no state is not a verdict", check_stp(None)["verdict"], "unknown")
check("every MIB value has a word", sorted(PORT_STATE.values()),
      ["blocking", "broken", "disabled", "forwarding", "learning", "listening"])


class FakeBridge:
    """A switch that publishes BRIDGE-MIB, which the bench one does not."""

    def __init__(self, mib, walks, bridge_map=None):
        self.host = "198.51.100.1"
        self.name = "sw-1"
        self._mib = mib
        self._walks = walks
        self._bridge = bridge_map or {}
        self.client = self

    def get_one(self, oid):
        return self._mib.get(oid)

    def walk(self, root, limit=4000):
        for index, value in sorted((self._walks.get(root) or {}).items()):
            yield "%s.%d" % (root, index), value

    def bridge_to_ifindex(self):
        return self._bridge


MIB = {OID_STP_PROTOCOL: 3, OID_STP_TOP_CHANGES: 7,
       OID_STP_TIME_SINCE_CHANGE: 150000,
       OID_STP_DESIGNATED_ROOT: b"8000001122334455"}
WALKS = {OID_STP_PORT_STATE: {1: 5, 2: 2, 3: 1},
         OID_STP_PORT_ENABLE: {1: 1, 2: 1, 3: 2},
         OID_STP_PORT_COST: {1: 2000, 2: 2000, 3: 0}}

tree = read_stp(FakeBridge(MIB, WALKS, {1: 1001, 2: 1002, 3: 1003}))
check("a bridge that runs STP says so", tree.running, True)
check("the protocol is named", tree.protocol, "IEEE 802.1D")
check("topology changes are counted", tree.topology_changes, 7)
check("and timed in seconds, not ticks", tree.seconds_since_change, 1500.0)
check("bridge ports are mapped onto ifIndex", sorted(tree.ports),
      [1001, 1002, 1003])
check("the forwarding one", tree.ports[1001]["state"], "forwarding")
check("the blocked one", tree.ports[1002]["state"], "blocking")
check("the disabled one", tree.ports[1003]["enabled"], False)
check("the summary counts what is not forwarding",
      "1 not forwarding" in tree.summary(), True)

quiet = read_stp(FakeBridge({}, {}))
check("a bridge with no spanning tree is not an error", quiet.running, False)
check("and says so in words",
      "runs no spanning tree" in quiet.summary(), True)
check("with no ports invented", quiet.ports, {})


class Exploding:
    host = "198.51.100.1"
    name = "sw-boom"

    @property
    def client(self):
        raise RuntimeError("agent went away")


check("an SNMP blow-up is caught",
      read_stp_safely(Exploding()).error.startswith("RuntimeError"), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all link-check checks passed")

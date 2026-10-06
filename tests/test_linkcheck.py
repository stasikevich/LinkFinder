"""Comparing the two ends of a link, and refusing to cry wolf.
Run: python tests/test_linkcheck.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.linkcheck import (check_mtu, check_speed, check_vlan, compare,
                                  describe, problems, summarise, tagged_vlans)
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
check("every layer appears in the long form", len(describe(attrs)), 3)

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
      ["unknown", "unknown", "unknown"])

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
      ["mtu", "speed", "vlan"])

import json
again = Topology()
again.merge_from(json.loads(t.to_json()))
carried = list(again.edges.values())[0]
check("overlays survive a save and a load", carried.problems, ["mtu", "vlan"])
check("with their detail intact",
      "frames over 1500" in carried.attrs["mtu"]["detail"], True)
check("a merge does not multiply the edge", len(again.edges), 1)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all link-check checks passed")

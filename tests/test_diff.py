"""Comparing two scans: what moved, and what only looks like it moved.
Run: python tests/test_diff.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.diff import (DEVICE_ADDED, DEVICE_CHANGED, DEVICE_REMOVED,
                             LINK_ADDED, LINK_MOVED, LINK_REMOVED, compare,
                             compare_files, match_nodes, render)
from linkfinder.topology import STATE_SCANNED, STATE_UNREACHABLE, Topology

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


def site(port="Eth1/1", second_host=False, host_state=STATE_SCANNED,
         gm="", mtu_verdict="match", host_name="srv1", host_ip="198.51.100.9"):
    graph = Topology(seed="198.51.100.1")
    switch = graph.add_node(chassis_id="aa:bb:cc:00:00:01", mgmt_ip="198.51.100.1",
                            name="sw1", kind="switch", state=STATE_SCANNED)
    host = graph.add_node(chassis_id="aa:bb:cc:00:00:09", mgmt_ip=host_ip,
                          name=host_name, kind="host", state=host_state)
    if gm:
        host.extra["ptp"] = {"eno1": {"gm_identity": gm}}
    link = graph.add_edge(switch.id, port, host.id, "eno1", proto="LLDP",
                          reported_by=switch.id)
    link.note("mtu", a=1500, b=10240, verdict=mtu_verdict)
    if second_host:
        other = graph.add_node(chassis_id="aa:bb:cc:00:00:22",
                               mgmt_ip="198.51.100.22", name="srv2", kind="host",
                               state=STATE_SCANNED)
        graph.add_edge(switch.id, "Eth1/5", other.id, "eno1", proto="LLDP",
                       reported_by=switch.id)
    return json.loads(graph.to_json())


print("-- a scan compared with itself --")
same = compare(site(), site())
check("nothing moved", same.quiet, True)
check("and it says so", same.summary(), "no change")
check("the report is short", "Nothing changed." in render(same), True)

print()
print("-- a device appears and another goes quiet --")
grown = compare(site(), site(second_host=True))
check("the new device is listed", [c.what for c in grown.of_kind(DEVICE_ADDED)],
      ["srv2"])
check("so is its link", len(grown.of_kind(LINK_ADDED)), 1)
check("and nothing was invented as removed", grown.of_kind(DEVICE_REMOVED), [])

shrunk = compare(site(second_host=True), site())
check("a device that vanished is reported", [c.what for c in
                                             shrunk.of_kind(DEVICE_REMOVED)], ["srv2"])
check("together with its link", len(shrunk.of_kind(LINK_REMOVED)), 1)

print()
print("-- a cable moved to the next port is one event, not two --")
repatched = compare(site("Eth1/1"), site("Eth1/2"))
check("reported as a move", len(repatched.of_kind(LINK_MOVED)), 1)
check("not as an addition", repatched.of_kind(LINK_ADDED), [])
check("nor as a removal", repatched.of_kind(LINK_REMOVED), [])
move = repatched.of_kind(LINK_MOVED)[0]
check("the old port is named", "Eth1/1" in move.detail, True)
check("and the new one", "Eth1/2" in move.what, True)
check("the endpoint change is spelled out", move.fields[0][1], "sw1 Eth1/1")
check("with where it went", move.fields[0][2], "sw1 Eth1/2")

print()
print("-- a device that changed without moving --")
state = compare(site(), site(host_state=STATE_UNREACHABLE))
changed = state.of_kind(DEVICE_CHANGED)
check("one device changed", len(changed), 1)
check("and the field is named", changed[0].fields,
      [("state", "scanned", "unreachable")])

renamed = compare(site(host_name="srv1"), site(host_name="srv1-new"))
check("a rename is a change, not a new device",
      [f[0] for f in renamed.of_kind(DEVICE_CHANGED)[0].fields], ["name"])
check("and the device is not reported as added", renamed.of_kind(DEVICE_ADDED), [])

readdressed = compare(site(host_ip="198.51.100.9"), site(host_ip="198.51.100.77"))
check("a device matched by chassis survives an address change",
      readdressed.of_kind(DEVICE_ADDED), [])
check("and the address change itself is reported",
      [f[0] for f in readdressed.of_kind(DEVICE_CHANGED)[0].fields], ["mgmt_ips"])

print()
print("-- the things an engineer is actually watching for --")
clock = compare(site(gm="025566.fffe.000001"), site(gm="02aabb.fffe.000002"))
fields = clock.of_kind(DEVICE_CHANGED)[0].fields
check("a grandmaster change is caught", fields[0][0], "ptp grandmaster on eno1")
check("with both values", (fields[0][1], fields[0][2]),
      ("025566.fffe.000001", "02aabb.fffe.000002"))

broke = compare(site(mtu_verdict="match"), site(mtu_verdict="mismatch"))
check("a link that went bad is reported", len(broke.of_kind("link changed")), 1)
check("naming the layer", broke.of_kind("link changed")[0].fields,
      [("mtu verdict", "match", "mismatch")])

print()
print("-- matching devices across files --")
before = [{"id": "chassis:aa", "chassis_id": "aa", "mgmt_ips": ["10.0.0.1"],
           "name": "sw"}]
after = [{"id": "ip:10.0.0.1", "chassis_id": "", "mgmt_ips": ["10.0.0.1"],
          "name": "sw"}]
pairs, gone, arrived = match_nodes(before, after)
check("a device whose id changed is still the same device",
      pairs.get("ip:10.0.0.1"), "chassis:aa")
check("so nothing is reported as gone", gone, [])
check("and nothing as new", arrived, [])

pairs, gone, arrived = match_nodes(before, [{"id": "chassis:bb", "chassis_id": "bb",
                                             "mgmt_ips": [], "name": "other"}])
check("a genuinely different device is not paired", pairs, {})
check("the old one is gone", len(gone), 1)
check("and the new one arrived", len(arrived), 1)

print()
print("-- through files, the way it is actually used --")
work = tempfile.mkdtemp(prefix="lf-diff-")
first = os.path.join(work, "monday.topology.json")
second = os.path.join(work, "tuesday.topology.json")
for path, payload in ((first, site()), (second, site("Eth1/2", second_host=True))):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
report = compare_files(first, second)
check("both files read", bool(report.changes), True)
check("the move survived the round trip", len(report.of_kind(LINK_MOVED)), 1)
check("and so did the new device", len(report.of_kind(DEVICE_ADDED)), 1)
text = render(report)
check("the report names the new device", "srv2" in text, True)
check("and has a heading", text.startswith("Changes between"), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all diff checks passed")

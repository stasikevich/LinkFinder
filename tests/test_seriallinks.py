"""A radio that answered on its console cable is drawn on the map.

serialports.capture() records the name a COM/tty port's console printed. This
turns that into a node: a radio named by what it said, cabled to the host on
the port it answered on. A port that stayed silent draws nothing; two cables
that report the same name are one radio with two consoles, not two radios.

Run: python tests/test_seriallinks.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import seriallinks
from linkfinder.topology import Topology

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def host_with(serial):
    topo = Topology()
    host = topo.add_node(mgmt_ip="10.0.0.9", name="bench-host", kind="windows",
                         state="scanned", extra={"serial": serial})
    return topo, host


def radios(topo):
    return [n for n in topo.nodes.values() if n.kind == "radio"]


def edges_from(topo, host_id):
    return [e for e in topo.edges.values() if host_id in (e.a, e.b)]


print("-- an answered console cable becomes a radio cabled to the host --")
serial = {
    "COM4": {"port": "COM4", "kind": "usb-serial", "likely_radio": True,
             "serial": "DK0CCYY8A", "answered": True,
             "radio_name": "Integrity-ME-0-B1B3B8B28n41-02-1"},
    "COM9": {"port": "COM9", "kind": "usb-serial", "likely_radio": True,
             "serial": "DK0NOANSW", "baud": 115200},     # probed, no answer
    "COM1": {"port": "COM1", "kind": "uart", "likely_radio": False},
}
topo, host = host_with(serial)
added = seriallinks.connect(topo)
check("one radio was drawn", added, 1)
rs = radios(topo)
check("exactly one radio node exists", len(rs), 1)
check("the radio is named by what it said",
      rs[0].name, "Integrity-ME-0-B1B3B8B28n41-02-1")
check("the radio is a radio", rs[0].kind, "radio")
check("the cable serial is kept, but not as the device serial",
      (rs[0].extra.get("console_cable_serial"), rs[0].serial),
      ("DK0CCYY8A", ""))
es = [e for e in edges_from(topo, host.id)]
check("there is one cable from the host to the radio", len(es), 1)
edge = es[0]
com_side = edge.a_iface if edge.a == host.id else edge.b_iface
check("the host side of the cable is the COM port it answered on",
      com_side, "COM4")
radio_side = edge.b_iface if edge.a == host.id else edge.a_iface
# add_edge stores an empty iface as "?", which the map's edge labels hide -
# so the radio side draws blank, like a fronthaul radio's far end does.
check("the radio side carries no real label (hidden on the map)",
      radio_side, "?")
check("the silent and the UART ports drew nothing", len(rs), 1)


print()
print("-- two cables, one name: a single radio with two consoles --")
serial2 = {
    "COM4": {"port": "COM4", "likely_radio": True, "answered": True,
             "radio_name": "Integrity-ME-0-B1B3B8B28n41-02-1"},
    "COM6": {"port": "COM6", "likely_radio": True, "answered": True,
             "radio_name": "Integrity-ME-0-B1B3B8B28n41-02-1"},
}
topo, host = host_with(serial2)
added = seriallinks.connect(topo)
check("two cables were drawn", added, 2)
check("but they go to one radio, not two", len(radios(topo)), 1)
coms = sorted((e.a_iface if e.a == host.id else e.b_iface)
              for e in edges_from(topo, host.id))
check("each cable keeps its own COM port", coms, ["COM4", "COM6"])


print()
print("-- nothing to draw when nothing answered --")
topo, host = host_with({
    "COM9": {"port": "COM9", "likely_radio": True, "baud": 115200}})
check("a host whose ports stayed silent draws no radio",
      seriallinks.connect(topo), 0)
check("and no radio node is invented", len(radios(topo)), 0)

topo = Topology()
topo.add_node(mgmt_ip="10.0.0.1", name="no-serial", kind="switch")
check("a host with no serial record is simply skipped",
      seriallinks.connect(topo), 0)


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all serial-console link checks passed")

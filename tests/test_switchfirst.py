"""Asking the switch before listening to the host.

The slow part of a scan is 125 seconds of listening for LLDP on a host that
runs no LLDP daemon. The switch it plugs into already knows which port each
of the host's addresses is on, in its MAC table, and answers in under a
second. So where a switch is known its table is read first and those ports
are never listened to; where none is known, nothing changes and the host is
listened to as before.

Run: python tests/test_switchfirst.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import engine
from linkfinder.model import Neighbor

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- no switch known: nothing changes --")
# The whole point of the guard: with no switch to ask, the resolver is None
# and the host is listened to exactly as before. No new behaviour sneaks in.
plain = engine.Options()
check("no switch, no resolver", engine._switch_first(plain), None)

print()
print("-- a known switch resolves a host's ports from its MAC table --")
# probe_switch_side is what talks SNMP; it is stubbed so this needs no lab.
# It is handed the host's MACs and fills a result with resolved neighbours,
# exactly as the live one does, and the resolver maps them back to the right
# interface by MAC.
import linkfinder.probe_snmp as _snmp
_real_side = _snmp.probe_switch_side
_real_build = _snmp.build_switches


def fake_side(target, switches, result, macs=None, learned=None, log=None):
    # The switch knows two of the three MACs it was asked about.
    known = {"aa:bb:cc:00:00:01": ("sw-core", "Gi1/1", "26"),
             "aa:bb:cc:00:00:02": ("sw-core", "Gi1/2", "1")}
    for mac in macs or []:
        if mac in known:
            sw, port, vlan = known[mac]
            result.add(Neighbor(host=target, local_mac=mac, switch=sw,
                                port=port, vlan=vlan, mgmt_ip="10.0.0.9",
                                proto="FDB", source="snmp-fdb:10.0.0.9",
                                confidence="high"))


_snmp.probe_switch_side = fake_side
_snmp.build_switches = lambda specs, community: []
try:
    opts = engine.Options()
    opts.switches = ["10.0.0.9"]
    resolve = engine._switch_first(opts)
    out = resolve("198.51.100.5", {
        "eno1": "aa:bb:cc:00:00:01",      # known, port Gi1/1
        "eno2": "aa:bb:cc:00:00:02",      # known, port Gi1/2
        "eno3": "aa:bb:cc:00:00:ff",      # the switch has never seen this one
    })
finally:
    _snmp.probe_switch_side = _real_side
    _snmp.build_switches = _real_build

check("a resolver is built when a switch is given", resolve is not None, True)
by_iface = {n.local_iface: n for n in out}
check("the ports the switch knew came back", sorted(by_iface), ["eno1", "eno2"])
check("each mapped to its own interface, by MAC",
      (by_iface["eno1"].port, by_iface["eno2"].port), ("Gi1/1", "Gi1/2"))
check("the VLAN the switch holds comes with it",
      by_iface["eno1"].vlan, "26")
check("the port the switch never saw is not invented",
      "eno3" in by_iface, False)
check("and every returned link is actually resolved",
      all(n.resolved for n in out), True)

print()
print("-- those ports then drop out of what must be listened to --")
# missing() in the probe lists the interfaces with no resolved neighbour.
# Once switch-first attaches eno1 and eno2, only eno3 is left to capture.
resolved_ifaces = {n.local_iface for n in out if n.resolved}
chosen = ["eno1", "eno2", "eno3"]
still = [i for i in chosen if i not in resolved_ifaces]
check("only the unexplained port is left for the capture", still, ["eno3"])

print()
print("-- SNMP falling over never stops a scan --")
# If the switch query throws, the resolver swallows it and returns nothing,
# so the host is simply listened to as it would have been without a switch.
def boom(*_a, **_k):
    raise OSError("snmp went away")
_snmp.probe_switch_side = boom
_snmp.build_switches = lambda specs, community: []
try:
    opts2 = engine.Options()
    opts2.switches = ["10.0.0.9"]
    safe = engine._switch_first(opts2)("198.51.100.5",
                                       {"eno1": "aa:bb:cc:00:00:01"})
finally:
    _snmp.probe_switch_side = _real_side
    _snmp.build_switches = _real_build
check("a failed switch query yields nothing, not an exception", safe, [])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all switch-first checks passed")

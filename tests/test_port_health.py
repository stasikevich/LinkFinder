"""Port status and health: classification, counter deltas and failure handling.
Run: python tests/test_port_health.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor
from linkfinder.port_health import (COUNTER_OIDS, OID_IF_ADMIN, OID_IF_DESCR,
                                    OID_IF_HIGHSPEED, OID_IF_LASTCHANGE, OID_IF_NAME,
                                    OID_IF_OPER, OID_IF_PHYS, OID_IF_SPEED, OID_IF_TYPE,
                                    OID_PVID, OID_SYS_UPTIME, PortHealth, classify,
                                    collect, collect_safely, human_speed, render_detail,
                                    render_report)
from linkfinder.snmp import SnmpError

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-56s %s" % (label, "ok" if got == want else "FAIL"))


class FakeClient:
    """Serves a canned MIB; the counter table can change between reads."""

    def __init__(self, mib, counters, second=None, fail=()):
        self.mib = mib
        self.counter_reads = [counters] + ([second] if second else [counters])
        self.read = 0
        self.fail = fail
        self.walks = 0

    def _counter_table(self):
        index = min(self.read, len(self.counter_reads) - 1)
        return self.counter_reads[index]

    def walk(self, root, limit=4000):
        self.walks += 1
        if root in self.fail:
            raise SnmpError("timeout on %s" % root)
        for label, oid in COUNTER_OIDS.items():
            if root == oid:
                table = self._counter_table().get(label, {})
                if label == "tx_octets":
                    self.read += 1
                for ifindex, value in sorted(table.items()):
                    yield "%s.%d" % (oid, ifindex), value
                return
        for ifindex, value in sorted(self.mib.get(root, {}).items()):
            yield "%s.%d" % (root, ifindex), value

    def get(self, *oids):
        """The collector asks for columns by index now, not by walking."""
        out = []
        advance = False
        for oid in oids:
            root, _dot, index = oid.rpartition(".")
            if root in self.fail:
                raise SnmpError("timeout on %s" % root)
            table = None
            for label, coid in COUNTER_OIDS.items():
                if root == coid:
                    table = self._counter_table().get(label, {})
                    advance = advance or label == "tx_octets"
                    break
            if table is None:
                table = self.mib.get(root, {})
            try:
                key = int(index)
            except ValueError:
                continue
            if key in table:
                out.append((oid, table[key]))
        if advance:
            self.read += 1
        return out

    def get_one(self, oid):
        if oid in self.fail:
            raise SnmpError("timeout on %s" % oid)
        return self.mib.get(oid)


class FakeSwitch:
    def __init__(self, client, neighbours=(), fdb=None, bridge=None):
        self.host = "198.51.100.1"
        self.name = "sw-test"
        self.client = client
        self._neighbours = list(neighbours)
        self._fdb = fdb or {}
        self._bridge = bridge or {}

    def lldp_neighbours(self):
        return self._neighbours

    def cdp_neighbours(self):
        return []

    def fdb(self):
        return self._fdb

    def bridge_to_ifindex(self):
        return self._bridge


def mib(overrides=None):
    base = {
        OID_IF_NAME: {1: b"Eth1/17", 2: b"Eth1/18", 3: b"Eth1/19", 4: b"Eth1/20"},
        OID_IF_DESCR: {1: b"Ethernet1/17", 2: b"Ethernet1/18", 3: b"Ethernet1/19",
                       4: b"Ethernet1/20"},
        OID_IF_TYPE: {1: 6, 2: 6, 3: 6, 4: 6},
        OID_IF_ADMIN: {1: 1, 2: 1, 3: 2, 4: 1},
        OID_IF_OPER: {1: 1, 2: 2, 3: 2, 4: 1},
        OID_IF_HIGHSPEED: {1: 1000, 2: 0, 3: 0, 4: 10000},
        OID_IF_SPEED: {1: 1000000000, 2: 0, 3: 0, 4: 0},
        OID_IF_LASTCHANGE: {1: 100, 2: 100, 3: 100, 4: 100},
        OID_IF_PHYS: {1: bytes.fromhex("aabbccddee01")},
        OID_PVID: {},
        OID_SYS_UPTIME: 10000000,
    }
    base.update(overrides or {})
    return base


def counters(rx_err=0, tx_err=0, rx_disc=0, tx_disc=0):
    return {
        "rx_errors": {1: rx_err, 2: 0, 3: 0, 4: 0},
        "tx_errors": {1: tx_err, 2: 0, 3: 0, 4: 0},
        "rx_discards": {1: rx_disc, 2: 0, 3: 0, 4: 0},
        "tx_discards": {1: tx_disc, 2: 0, 3: 0, 4: 0},
        "rx_octets": {1: 1000, 2: 0, 3: 0, 4: 0},
        "tx_octets": {1: 1000, 2: 0, 3: 0, 4: 0},
    }


def run(switch, **kw):
    kw.setdefault("sleeper", lambda _s: None)
    return collect(switch, **kw)


print("-- the four basic states --")
report = run(FakeSwitch(FakeClient(mib(), counters(), counters())))
ports = {p.name: p for p in report.ports}
check("admin up + link up          -> UP", ports["Eth1/17"].status, "UP")
check("admin up + link down        -> DOWN", ports["Eth1/18"].status, "DOWN")
check("admin down                  -> DISABLED", ports["Eth1/19"].status, "DISABLED")
check("down port is a free candidate", ports["Eth1/18"].usage, "FREE_CANDIDATE")
check("disabled port usage", ports["Eth1/19"].usage, "DISABLED")
check("disabled is stated with confidence", ports["Eth1/19"].confidence, "high")
check("speed is rendered", ports["Eth1/17"].speed, "1G")
check("10G rendered", ports["Eth1/20"].speed, "10G")
check("mac read", ports["Eth1/17"].mac, "aa:bb:cc:dd:ee:01")

print()
print("-- old high counters alone are NOT a problem --")
stale = counters(rx_err=5000, tx_err=4000, rx_disc=900)
report = run(FakeSwitch(FakeClient(mib(), stale, stale)))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("five thousand old errors, none new  -> UP", port.status, "UP")
check("the total is still reported", port.counters["rx_errors"], 5000)
check("but the delta is zero", port.deltas["rx_errors"], 0)
check("and nothing is blamed on it",
      any("increased" in r for r in port.reasons), False)

print()
print("-- a counter that actually moves --")
report = run(FakeSwitch(FakeClient(mib(), counters(rx_err=1203),
                                   counters(rx_err=1240, rx_disc=3))))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("growing rx errors -> DEGRADED", port.status, "DEGRADED")
check("delta computed", port.deltas["rx_errors"], 37)
check("discards counted too", port.deltas["rx_discards"], 3)
check("confidence high", port.confidence, "high")
check("reason names the counter",
      any("rx_errors increased by 37" in r for r in port.reasons), True)

report = run(FakeSwitch(FakeClient(mib(), counters(tx_disc=10), counters(tx_disc=14))))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("growing discards alone -> DEGRADED", port.status, "DEGRADED")

print()
print("-- a counter that goes backwards (agent restart) is not a fault --")
report = run(FakeSwitch(FakeClient(mib(), counters(rx_err=900), counters(rx_err=5))))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("wrapped counter is clamped, not blamed", port.deltas["rx_errors"], 0)
check("still UP", port.status, "UP")

print()
print("-- usage evidence and its confidence --")
peer = Neighbor(local_iface="Eth1/17", switch="server01", port="eno1", proto="LLDP")
report = run(FakeSwitch(FakeClient(mib(), counters(), counters()), neighbours=[peer]))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("LLDP neighbour -> USED", port.usage, "USED")
check("with high confidence", port.confidence, "high")
check("neighbour recorded", (port.neighbour, port.neighbour_port), ("server01", "eno1"))

fdb = {"aa:bb:cc:dd:ee:10": (1, "84"), "aa:bb:cc:dd:ee:11": (1, "84")}
report = run(FakeSwitch(FakeClient(mib(), counters(), counters()),
                        fdb=fdb, bridge={1: 1}))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("FDB entries -> USED", port.usage, "USED")
check("two MACs counted", port.fdb_macs, 2)
check("no LLDP, so confidence from FDB", port.confidence, "high")

single = {"aa:bb:cc:dd:ee:10": (1, "84")}
report = run(FakeSwitch(FakeClient(mib(), counters(), counters()),
                        fdb=single, bridge={1: 1}))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("one MAC only -> medium confidence", port.confidence, "medium")

report = run(FakeSwitch(FakeClient(mib(), counters(), counters())))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("link up but nothing identifies the far end -> low", port.confidence, "low")

print()
print("-- link flapping --")
recent = mib({OID_IF_LASTCHANGE: {1: 9999000, 2: 100, 3: 100, 4: 100}})
report = run(FakeSwitch(FakeClient(recent, counters(), counters())))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("link changed 10 seconds ago -> DEGRADED", port.status, "DEGRADED")
check("reason explains it", any("changed" in r for r in port.reasons), True)

print()
print("-- speed below what was expected --")
report = run(FakeSwitch(FakeClient(mib(), counters(), counters())), expect_speed=10000)
port = {p.name: p for p in report.ports}["Eth1/17"]
check("1G where 10G was expected -> DEGRADED", port.status, "DEGRADED")
check("reason names both speeds",
      any("below the expected 10G" in r for r in port.reasons), True)
report = run(FakeSwitch(FakeClient(mib(), counters(), counters())))
check("without an expectation it is not judged",
      {p.name: p for p in report.ports}["Eth1/17"].status, "UP")

print()
print("-- missing MIB values --")
sparse = mib({OID_IF_ADMIN: {}, OID_IF_OPER: {}})
report = run(FakeSwitch(FakeClient(sparse, counters(), counters())))
port = {p.name: p for p in report.ports}["Eth1/17"]
check("no admin or oper state -> UNKNOWN", port.status, "UNKNOWN")
check("usage unknown too", port.usage, "UNKNOWN")
check("and it says why", any("not reported" in r for r in port.reasons), True)

print()
print("-- only physical ports unless asked otherwise --")
mixed = mib({OID_IF_TYPE: {1: 6, 2: 6, 3: 53, 4: 24}})
report = run(FakeSwitch(FakeClient(mixed, counters(), counters())))
check("virtual and loopback skipped", sorted(p.name for p in report.ports),
      ["Eth1/17", "Eth1/18"])
report = run(FakeSwitch(FakeClient(mixed, counters(), counters())), physical_only=False)
check("--all-interfaces keeps them", len(report.ports), 4)

print()
print("-- SNMP failures never escape --")
dead = FakeClient(mib(), counters(), counters(), fail=(OID_IF_NAME, OID_IF_DESCR))
report = run(FakeSwitch(dead))
check("empty interface table is reported, not raised",
      report.error.startswith("timeout") or "no interface table" in report.error, True)
check("no ports invented", report.ports, [])
check("summary says it is unavailable",
      report.summary().startswith("port health unavailable"), True)


class Exploding:
    host = "198.51.100.9"
    name = "boom"

    class client:
        @staticmethod
        def walk(root, limit=4000):
            raise RuntimeError("socket exploded")

        @staticmethod
        def get_one(oid):
            raise RuntimeError("socket exploded")


report = collect_safely(Exploding(), sleeper=lambda _s: None)
check("an unexpected exception is caught", report.ports, [])
check("and recorded", "socket exploded" in report.error, True)

report = run(FakeSwitch(FakeClient(mib(), counters(), counters())))
check("a good run has no error", report.error, "")

print()
print("-- a vendor MIB we do not have never blocks the result --")
report = run(FakeSwitch(FakeClient(mib({OID_PVID: {}}), counters(), counters())))
check("no VLAN data, ports still produced", len(report.ports), 4)
check("vlan field simply empty",
      {p.name: p for p in report.ports}["Eth1/17"].vlan, "")
report = run(FakeSwitch(FakeClient(mib({OID_PVID: {1: 84}}), counters(), counters()),
                        bridge={1: 1}))
check("when PVID is there it is used",
      {p.name: p for p in report.ports}["Eth1/17"].vlan, "84")

print()
print("-- rendering --")
port = PortHealth(ifindex=1, name="Eth1/17", admin="up", oper="up", speed_mbps=1000,
                  neighbour="server01", neighbour_port="eno1",
                  counters={"rx_errors": 1240, "tx_errors": 0, "rx_discards": 12},
                  deltas={"rx_errors": 37, "tx_errors": 0, "rx_discards": 3})
classify(port)
text = render_detail(port)
for expected in ("Port: Eth1/17", "Health: DEGRADED", "Confidence: HIGH",
                 "Neighbor: server01", "Remote port: eno1", "rx_errors:   1240 (+37)"):
    check("detail contains %r" % expected, expected in text, True)

idle = PortHealth(ifindex=2, name="Eth1/18", admin="up", oper="down")
classify(idle)
text = render_detail(idle)
check("down port block", "Usage: FREE_CANDIDATE" in text and "Health: DOWN" in text, True)

whole = render_report(run(FakeSwitch(FakeClient(mib(), counters(), counters()))))
check("report has a table header", "PORT" in whole and "HEALTH" in whole, True)
check("report ends with a summary", "4 port(s)" in whole, True)

print()
print("-- the opt-in promise: no extra reads unless asked --")
client = FakeClient(mib(), counters(), counters())
before = client.walks
check("nothing read just by building the switch", before, 0)
run(FakeSwitch(client))
check("reads only happen when collect() is called", client.walks > 0, True)

check("human_speed", (human_speed(1000), human_speed(2500), human_speed(0)),
      ("1G", "2500M", ""))

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all port-health checks passed")

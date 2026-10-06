"""Optional SNMP port status and health, as an enrichment layer.

Opt-in by construction: nothing here runs unless the caller asks for it, and a
failure anywhere in here must never disturb the discovery that called it.

The one rule that shapes the whole module: an absolute counter means nothing.
ifInErrors = 5000 may be five years old. A port is only called DEGRADED when a
counter actually moved between two reads taken a few seconds apart, so every
health verdict rests on a delta, never on a total.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .snmp import SnmpError, as_mac, as_text

OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_PHYS = "1.3.6.1.2.1.2.2.1.6"
OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_LASTCHANGE = "1.3.6.1.2.1.2.2.1.9"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"

COUNTER_OIDS = {
    "rx_errors": "1.3.6.1.2.1.2.2.1.14",
    "tx_errors": "1.3.6.1.2.1.2.2.1.20",
    "rx_discards": "1.3.6.1.2.1.2.2.1.13",
    "tx_discards": "1.3.6.1.2.1.2.2.1.19",
    "rx_octets": "1.3.6.1.2.1.31.1.1.1.6",
    "tx_octets": "1.3.6.1.2.1.31.1.1.1.10",
}

FAULT_COUNTERS = ("rx_errors", "tx_errors", "rx_discards", "tx_discards")

ADMIN_STATES = {1: "up", 2: "down", 3: "testing"}
OPER_STATES = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant",
               6: "notPresent", 7: "lowerLayerDown"}
PHYSICAL_IFTYPES = (6, 117, 62, 69)

STATUS_UP = "UP"
STATUS_DOWN = "DOWN"
STATUS_DISABLED = "DISABLED"
STATUS_DEGRADED = "DEGRADED"
STATUS_UNKNOWN = "UNKNOWN"

USAGE_USED = "USED"
USAGE_FREE = "FREE_CANDIDATE"
USAGE_DISABLED = "DISABLED"
USAGE_UNKNOWN = "UNKNOWN"

HIGH, MEDIUM, LOW = "high", "medium", "low"

FLAP_WINDOW_SECONDS = 300


def human_speed(mbps: int) -> str:
    if not mbps:
        return ""
    if mbps >= 1000 and mbps % 1000 == 0:
        return "%dG" % (mbps // 1000)
    return "%dM" % mbps


@dataclass
class PortHealth:
    ifindex: int
    name: str = ""
    descr: str = ""
    mac: str = ""
    admin: str = ""
    oper: str = ""
    speed_mbps: int = 0
    last_change_seconds: Optional[float] = None
    status: str = STATUS_UNKNOWN
    usage: str = USAGE_UNKNOWN
    confidence: str = LOW
    reasons: List[str] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)
    deltas: Dict[str, int] = field(default_factory=dict)
    neighbour: str = ""
    neighbour_port: str = ""
    fdb_macs: int = 0
    vlan: str = ""

    @property
    def speed(self) -> str:
        return human_speed(self.speed_mbps)

    @property
    def label(self) -> str:
        return self.name or self.descr or ("ifindex %d" % self.ifindex)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ifindex": self.ifindex, "name": self.name, "descr": self.descr,
            "mac": self.mac, "admin": self.admin, "oper": self.oper,
            "speed_mbps": self.speed_mbps, "speed": self.speed,
            "last_change_seconds": self.last_change_seconds,
            "status": self.status, "usage": self.usage, "confidence": self.confidence,
            "reasons": list(self.reasons), "counters": dict(self.counters),
            "deltas": dict(self.deltas), "neighbour": self.neighbour,
            "neighbour_port": self.neighbour_port, "fdb_macs": self.fdb_macs,
            "vlan": self.vlan,
        }


@dataclass
class PortHealthReport:
    host: str
    switch_name: str = ""
    sampled_seconds: float = 0.0
    ports: List[PortHealth] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.ports) and not self.error

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for port in self.ports:
            out[port.status] = out.get(port.status, 0) + 1
        return out

    def summary(self) -> str:
        if self.error and not self.ports:
            return "port health unavailable: %s" % self.error
        counts = self.counts()
        parts = ", ".join("%d %s" % (counts[k], k) for k in sorted(counts))
        return "%d port(s): %s" % (len(self.ports), parts or "-")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host, "switch_name": self.switch_name,
            "sampled_seconds": self.sampled_seconds, "error": self.error,
            "counts": self.counts(),
            "ports": [p.to_dict() for p in self.ports],
        }


def _walk_ints(client, root: str) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for oid, value in client.walk(root):
        try:
            out[int(oid.rsplit(".", 1)[-1])] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _walk_text(client, root: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for oid, value in client.walk(root):
        out[int(oid.rsplit(".", 1)[-1])] = as_text(value)
    return out


def read_counters(client) -> Dict[str, Dict[int, int]]:
    """One walk per counter column; GETBULK keeps that to a few packets."""
    snapshot: Dict[str, Dict[int, int]] = {}
    for label, root in COUNTER_OIDS.items():
        try:
            snapshot[label] = _walk_ints(client, root)
        except SnmpError:
            snapshot[label] = {}
    return snapshot


def classify(port: PortHealth, expect_speed: int = 0) -> PortHealth:
    """Decide status, usage and confidence from what was actually observed."""
    reasons: List[str] = []

    if port.admin == "down":
        port.status = STATUS_DISABLED
        port.usage = USAGE_DISABLED
        port.confidence = HIGH
        port.reasons = ["administratively shut down"]
        return port

    if not port.admin or not port.oper:
        port.status = STATUS_UNKNOWN
        port.usage = USAGE_UNKNOWN
        port.confidence = LOW
        port.reasons = ["admin or operational state not reported"]
        return port

    if port.oper != "up":
        port.status = STATUS_DOWN
        port.usage = USAGE_FREE
        port.confidence = MEDIUM
        port.reasons = ["admin up but link is %s" % port.oper]
        return port

    port.status = STATUS_UP
    if port.neighbour:
        port.usage = USAGE_USED
        port.confidence = HIGH
        reasons.append("LLDP neighbour %s" % port.neighbour)
    elif port.fdb_macs:
        port.usage = USAGE_USED
        port.confidence = HIGH if port.fdb_macs > 1 else MEDIUM
        reasons.append("%d MAC(s) learned on this port" % port.fdb_macs)
    else:
        port.usage = USAGE_USED
        port.confidence = LOW
        reasons.append("link is up but nothing identifies the far end")

    faults: List[str] = []
    for label in FAULT_COUNTERS:
        grew = port.deltas.get(label, 0)
        if grew > 0:
            faults.append("%s increased by %d during sampling" % (label, grew))
    if port.last_change_seconds is not None and port.last_change_seconds < FLAP_WINDOW_SECONDS:
        faults.append("link state changed %d seconds ago"
                      % int(port.last_change_seconds))
    if expect_speed and port.speed_mbps and port.speed_mbps < expect_speed:
        faults.append("negotiated %s, below the expected %s"
                      % (human_speed(port.speed_mbps), human_speed(expect_speed)))

    if faults:
        port.status = STATUS_DEGRADED
        port.confidence = HIGH
        reasons = faults + reasons
    port.reasons = reasons
    return port


def collect(switch, interval: float = 3.0, expect_speed: int = 0,
            physical_only: bool = True, sleeper: Callable[[float], None] = time.sleep,
            log: Optional[Callable[[str], None]] = None) -> PortHealthReport:
    """Read the interface table twice and turn the difference into a verdict.

    `switch` is a probe_snmp.Switch - the existing SNMP transport is reused, no
    second implementation and no second set of credentials.
    """
    say = log or (lambda _m: None)
    report = PortHealthReport(host=getattr(switch, "host", ""),
                              switch_name=getattr(switch, "name", ""))
    client = switch.client
    try:
        names = _walk_text(client, OID_IF_NAME)
        descrs = _walk_text(client, OID_IF_DESCR)
        if not names and not descrs:
            report.error = "the device returned no interface table (IF-MIB empty)"
            return report
        types = _walk_ints(client, OID_IF_TYPE)
        admin = _walk_ints(client, OID_IF_ADMIN)
        oper = _walk_ints(client, OID_IF_OPER)
        high_speed = _walk_ints(client, OID_IF_HIGHSPEED)
        low_speed = _walk_ints(client, OID_IF_SPEED)
        last_change = _walk_ints(client, OID_IF_LASTCHANGE)
        phys: Dict[int, str] = {}
        for oid, value in client.walk(OID_IF_PHYS):
            phys[int(oid.rsplit(".", 1)[-1])] = as_mac(value)
        uptime_before = client.get_one(OID_SYS_UPTIME)
        first = read_counters(client)
    except SnmpError as exc:
        report.error = str(exc)
        return report
    except Exception as exc:
        report.error = "%s: %s" % (type(exc).__name__, exc)
        return report

    say("  sampling counters for %.0fs to tell real errors from old ones" % interval)
    started = time.time()
    sleeper(interval)
    try:
        second = read_counters(client)
        uptime_after = client.get_one(OID_SYS_UPTIME)
    except SnmpError as exc:
        second, uptime_after = {}, uptime_before
        report.error = "second counter read failed: %s" % exc
    report.sampled_seconds = round(time.time() - started, 1)

    neighbours = {}
    try:
        for nb in switch.lldp_neighbours():
            if nb.local_iface:
                neighbours[nb.local_iface] = nb
    except Exception:
        pass

    per_port_macs: Dict[int, int] = {}
    try:
        bridge_map = switch.bridge_to_ifindex()
        for _mac, (bridge_port, _vlan) in switch.fdb().items():
            ifindex = bridge_map.get(bridge_port, bridge_port)
            per_port_macs[ifindex] = per_port_macs.get(ifindex, 0) + 1
    except Exception:
        pass

    pvids: Dict[int, int] = {}
    try:
        bridge_map = switch.bridge_to_ifindex()
        for bridge_port, vlan in _walk_ints(client, OID_PVID).items():
            pvids[bridge_map.get(bridge_port, bridge_port)] = vlan
    except Exception:
        pass

    now_ticks = uptime_after if isinstance(uptime_after, int) else None
    for ifindex in sorted(set(list(names) + list(descrs))):
        if physical_only and types and types.get(ifindex) not in PHYSICAL_IFTYPES:
            continue
        port = PortHealth(ifindex=ifindex, name=names.get(ifindex, ""),
                          descr=descrs.get(ifindex, ""), mac=phys.get(ifindex, ""))
        port.admin = ADMIN_STATES.get(admin.get(ifindex), "")
        port.oper = OPER_STATES.get(oper.get(ifindex), "")
        port.speed_mbps = high_speed.get(ifindex) or int(low_speed.get(ifindex, 0) / 1000000)
        if now_ticks is not None and ifindex in last_change:
            port.last_change_seconds = max(0.0, (now_ticks - last_change[ifindex]) / 100.0)
        for label in COUNTER_OIDS:
            before = first.get(label, {}).get(ifindex)
            after = second.get(label, {}).get(ifindex)
            if before is None:
                continue
            port.counters[label] = after if after is not None else before
            if after is not None:
                port.deltas[label] = max(0, after - before)
        nb = neighbours.get(port.name) or neighbours.get(port.descr)
        if nb is not None:
            port.neighbour = nb.switch or nb.chassis_id
            port.neighbour_port = nb.best_port
        port.fdb_macs = per_port_macs.get(ifindex, 0)
        if ifindex in pvids:
            port.vlan = str(pvids[ifindex])
        report.ports.append(classify(port, expect_speed=expect_speed))

    say("  %s" % report.summary())
    return report


def collect_safely(switch, **kwargs) -> PortHealthReport:
    """Never let a health read break the discovery that asked for it."""
    try:
        return collect(switch, **kwargs)
    except Exception as exc:
        return PortHealthReport(host=getattr(switch, "host", ""),
                                error="%s: %s" % (type(exc).__name__, exc))


def render_table(report: PortHealthReport) -> str:
    headers = ["PORT", "ADMIN", "LINK", "SPEED", "USAGE", "HEALTH", "CONF",
               "NEIGHBOUR", "VLAN"]
    rows = []
    for port in report.ports:
        rows.append([port.label, port.admin or "-", port.oper or "-", port.speed or "-",
                     port.usage, port.status, port.confidence,
                     port.neighbour or ("%d MAC(s)" % port.fdb_macs if port.fdb_macs else "-"),
                     port.vlan or "-"])
    if not rows:
        return ""
    widths = [min(28, max(len(h), *(len(r[i]) for r in rows)))
              for i, h in enumerate(headers)]

    def line(cells):
        return "  ".join(str(c)[:widths[i]].ljust(widths[i])
                         for i, c in enumerate(cells)).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def render_detail(port: PortHealth) -> str:
    lines = ["Port: %s" % port.label,
             "Admin: %s" % (port.admin or "unknown").upper(),
             "Link: %s" % (port.oper or "unknown").upper()]
    if port.speed:
        lines.append("Speed: %s" % port.speed)
    lines.append("Usage: %s" % port.usage)
    lines.append("Health: %s" % port.status)
    lines.append("Confidence: %s" % port.confidence.upper())
    if port.neighbour:
        lines.append("")
        lines.append("LLDP:")
        lines.append("  Neighbor: %s" % port.neighbour)
        if port.neighbour_port:
            lines.append("  Remote port: %s" % port.neighbour_port)
    shown = [k for k in FAULT_COUNTERS if k in port.counters]
    if shown:
        lines.append("")
        lines.append("Counters:")
        for key in shown:
            lines.append("  %-12s %d (+%d)" % (key + ":", port.counters[key],
                                               port.deltas.get(key, 0)))
    if port.reasons:
        lines.append("")
        lines.append("Reason:")
        for reason in port.reasons:
            lines.append("  %s" % reason)
    return "\n".join(lines)


def render_report(report: PortHealthReport, detail_for=(STATUS_DEGRADED,)) -> str:
    blocks = []
    header = "Port health on %s" % (report.switch_name or report.host)
    if report.sampled_seconds:
        header += " (counters sampled over %.0fs)" % report.sampled_seconds
    blocks.append(header)
    if report.error:
        blocks.append("Port health: %s" % (STATUS_UNKNOWN if not report.ports else "partial"))
        blocks.append("Error: %s" % report.error)
    table = render_table(report)
    if table:
        blocks.append("")
        blocks.append(table)
    for port in report.ports:
        if port.status in detail_for:
            blocks.append("")
            blocks.append(render_detail(port))
    if report.ports:
        blocks.append("")
        blocks.append(report.summary())
    return "\n".join(blocks)

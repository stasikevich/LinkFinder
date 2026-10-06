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

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .netaddr import is_reachable_mgmt

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
    notes: List[str] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)
    deltas: Dict[str, int] = field(default_factory=dict)
    neighbour: str = ""
    neighbour_port: str = ""
    fdb_macs: int = 0
    vlan: str = ""

    @property
    def speed(self) -> str:
        """A down port still reports a speed; it is not a negotiated one."""
        if self.oper and self.oper != "up":
            return ""
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
            "reasons": list(self.reasons), "notes": list(self.notes),
            "counters": dict(self.counters),
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


def _walk_raw(client, root: str) -> Dict[int, object]:
    out: Dict[int, object] = {}
    for oid, value in client.walk(root):
        try:
            out[int(oid.rsplit(".", 1)[-1])] = value
        except ValueError:
            continue
    return out


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


GET_BATCH = 20


def _column(client, root: str, indexes: Optional[List[int]]) -> Dict[int, object]:
    """One IF-MIB column, asked for by index rather than walked.

    Walking is wrong on a switch with thousands of VLAN interfaces: the VLANs
    occupy the low indexes, the physical ports sit above them, and a walk that
    stops early returns a table with every real port missing and no hint that
    anything was cut. Once ifName has told us which indexes exist we can ask for
    exactly those, which is both correct and far fewer packets.
    """
    if indexes is None:
        return dict(_walk_raw(client, root))
    out: Dict[int, object] = {}
    base = root.rstrip(".")
    for start in range(0, len(indexes), GET_BATCH):
        chunk = indexes[start:start + GET_BATCH]
        try:
            answers = client.get(*["%s.%d" % (base, i) for i in chunk])
        except SnmpError:
            continue
        for oid, value in answers:
            if value == "__END__" or value is None:
                continue
            try:
                out[int(oid.rsplit(".", 1)[-1])] = value
            except ValueError:
                continue
    return out


def _as_ints(raw: Dict[int, object]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for index, value in raw.items():
        try:
            out[index] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _as_texts(raw: Dict[int, object]) -> Dict[int, str]:
    return {index: as_text(value) for index, value in raw.items()}


def read_counters(client, indexes: Optional[List[int]] = None) -> Dict[str, Dict[int, int]]:
    """One read per counter column, by index when we know which ports exist."""
    snapshot: Dict[str, Dict[int, int]] = {}
    for label, root in COUNTER_OIDS.items():
        try:
            snapshot[label] = _as_ints(_column(client, root, indexes))
        except SnmpError:
            snapshot[label] = {}
    return snapshot


TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def lldp_by_ifindex(switch, client, names: Dict[int, str],
                    descrs: Dict[int, str]) -> Dict[int, object]:
    """Join the switch's own LLDP table to ifIndex, exactly where we can.

    lldpRemLocalPortNum is an LLDP port number, not an ifIndex. On gear that
    numbers its physical ports 1000001 upwards while LLDP calls them 1..21, a
    direct lookup matches nothing and the neighbour column stays empty. Try the
    exact routes first and fall back to the port number only when it identifies
    exactly one interface, so a guess can never silently become a fact.
    """
    local: Dict[str, str] = {}
    try:
        for oid, value in client.walk(OID_LLDP_LOC_PORTID):
            local[oid.rsplit(".", 1)[-1]] = as_text(value)
    except Exception:
        pass
    by_name = {text: index for index, text in names.items() if text}
    by_descr = {text: index for index, text in descrs.items() if text}
    tails: Dict[str, List[int]] = {}
    for index, text in names.items():
        match = TRAILING_NUMBER.search(text or "")
        if match:
            tails.setdefault(str(int(match.group(1))), []).append(index)

    out: Dict[int, object] = {}
    try:
        found = switch.lldp_neighbours()
    except Exception:
        return out
    for nb in found:
        key = (getattr(nb, "local_iface", "") or "").strip()
        if not key:
            continue
        text = local.get(key, "")
        index = by_name.get(key) or by_descr.get(key)
        if index is None and text:
            index = by_name.get(text) or by_descr.get(text)
        if index is None and key.isdigit() and int(key) in names:
            index = int(key)
        exact = index is not None
        if index is None and key.isdigit():
            hits = tails.get(str(int(key)), [])
            if len(hits) == 1:
                index = hits[0]
        if index is None:
            continue
        if not exact:
            setattr(nb, "confidence", "inferred")
        out[index] = nb
    return out


def is_physical(ifindex: int, types: Dict[int, int], names: Dict[int, str]) -> bool:
    """Only reject a port when the device positively says it is not one.

    Some switches publish ifType for their VLAN interfaces and nothing at all
    for the physical ports. Reading a missing type as "not physical" threw away
    every real port and reported an empty table.
    """
    kind = types.get(ifindex)
    if kind is None:
        return ifindex in names
    return kind in PHYSICAL_IFTYPES


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


def switches_worth_asking(results=None, explicit=None, targets=None):
    """Which addresses to ask for port health, best first.

    The original version asked only the scan targets, which is wrong for the
    common case: you point the tool at a server, it finds the switch that server
    is plugged into, talks SNMP to it - and then port health went back and asked
    the server, which is not a switch and never answers. The switch we just
    found is the one with the ports worth looking at, so it goes first.
    """
    out = []

    def add(value):
        value = (value or "").strip()
        if value and value not in out and is_reachable_mgmt(value.partition("@")[0]):
            out.append(value)

    for value in explicit or []:
        add(value)
    for result in results or []:
        for nb in getattr(result, "neighbors", []) or []:
            add(getattr(nb, "mgmt_ip", ""))
    for value in targets or []:
        add(value)
    return out


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
        known = sorted(names) if names else None
        if known is None:
            descrs = _walk_text(client, OID_IF_DESCR)
        else:
            descrs = _as_texts(_column(client, OID_IF_DESCR, known))
        if not names and not descrs:
            report.error = "the device returned no interface table (IF-MIB empty)"
            return report
        if known is None:
            known = sorted(set(list(names) + list(descrs)))
        types = _as_ints(_column(client, OID_IF_TYPE, known))
        admin = _as_ints(_column(client, OID_IF_ADMIN, known))
        oper = _as_ints(_column(client, OID_IF_OPER, known))
        high_speed = _as_ints(_column(client, OID_IF_HIGHSPEED, known))
        low_speed = _as_ints(_column(client, OID_IF_SPEED, known))
        last_change = _as_ints(_column(client, OID_IF_LASTCHANGE, known))
        phys = {index: as_mac(value)
                for index, value in _column(client, OID_IF_PHYS, known).items()}
        uptime_before = client.get_one(OID_SYS_UPTIME)
        first = read_counters(client, known)
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
        second = read_counters(client, known)
        uptime_after = client.get_one(OID_SYS_UPTIME)
    except SnmpError as exc:
        second, uptime_after = {}, uptime_before
        report.error = "second counter read failed: %s" % exc
    report.sampled_seconds = round(time.time() - started, 1)

    neighbours = lldp_by_ifindex(switch, client, names, descrs)

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
        if physical_only and not is_physical(ifindex, types, names):
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
        nb = neighbours.get(ifindex)
        if nb is not None:
            port.neighbour = nb.switch or nb.chassis_id
            port.neighbour_port = nb.best_port
            if getattr(nb, "confidence", "") == "inferred":
                port.notes.append("neighbour matched to this port by port number, "
                                  "not by an exact name from the device")
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
        for note in port.notes:
            lines.append("  Note: %s" % note)
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

"""Asking a switch directly what it thinks about time, over SNMP.

Third rung of the PTP ladder. The first two need no credentials at all - the
Announce messages on the wire are read passively, and the protocol's own
management messages are a query anyone on the segment may send. This one uses
the SNMP community discovery already has, and asks the device itself, which is
the only way to learn things a bystander cannot see: which of the switch's own
ports is taking time in, how long it has been in holdover, what it would fall
back to.

RFC 8173 defines the standard MIB; vendors that predate it or ignore it publish
their own. Support is genuinely patchy - the switch this was written against
publishes no PTP objects at all, not even an sysORTable to say so - so the
first thing this does is establish whether there is anything there, and say so
plainly when there is not. Nothing is inferred from an absent table.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .snmp import SnmpError, as_text

PTPBASE = "1.3.6.1.2.1.241"

TABLES = (
    ("system", PTPBASE + ".1.1.1"),
    ("domain", PTPBASE + ".1.1.2"),
    ("clock", PTPBASE + ".1.1.3"),
    ("current", PTPBASE + ".1.1.4"),
    ("parent", PTPBASE + ".1.1.5"),
    ("default", PTPBASE + ".1.1.6"),
    ("running", PTPBASE + ".1.1.7"),
    ("time_properties", PTPBASE + ".1.1.8"),
    ("port", PTPBASE + ".1.1.10"),
    ("port_ds", PTPBASE + ".1.1.11"),
    ("port_running", PTPBASE + ".1.1.12"),
    ("associate", PTPBASE + ".1.1.13"),
)

VENDOR_TREES = (
    ("Cisco CISCO-PTP-MIB", "1.3.6.1.4.1.9.9.760"),
    ("Juniper", "1.3.6.1.4.1.2636.3.65"),
    ("Arista", "1.3.6.1.4.1.30065.3.14"),
    ("Nokia SR OS", "1.3.6.1.4.1.6527.3.1.2.68"),
)

MAX_ROWS = 400


@dataclass
class SwitchPtp:
    """What one switch was willing to say about PTP."""

    host: str = ""
    name: str = ""
    source: str = ""              # which MIB answered, "" when none did
    supported: bool = False
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    vendor_trees: List[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return "PTP over SNMP on %s: %s" % (self.name or self.host, self.error)
        if not self.supported:
            extra = (" (vendor PTP objects present: %s - not decoded)"
                     % ", ".join(self.vendor_trees)) if self.vendor_trees else ""
            return ("%s publishes no standard PTP MIB%s"
                    % (self.name or self.host, extra))
        rows = sum(len(v) for v in self.tables.values())
        return ("%s answers %s with %d object(s) across %d table(s)"
                % (self.name or self.host, self.source, rows, len(self.tables)))

    def to_dict(self) -> Dict[str, Any]:
        return {"host": self.host, "name": self.name, "source": self.source,
                "supported": self.supported,
                "tables": {k: dict(v) for k, v in self.tables.items()},
                "vendor_trees": list(self.vendor_trees), "error": self.error}


def _readable(value: Any) -> Any:
    """SNMP hands back bytes for anything it cannot type.

    as_text already renders a binary value as colon-separated hex, which is the
    form a clock identity is normally written in, so there is nothing to add.
    """
    return as_text(value) if isinstance(value, bytes) else value


def _walk_table(client, root: str, limit: int = MAX_ROWS) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    base = root.rstrip(".") + "."
    for oid, value in client.walk(root, limit=limit):
        key = oid[len(base):] if oid.startswith(base) else oid
        out[key] = _readable(value)
    return out


def read_switch_ptp(switch, log: Optional[Callable[[str], None]] = None) -> SwitchPtp:
    """Standard MIB first, then look for a vendor tree worth naming.

    `switch` is a probe_snmp.Switch, so this reuses the transport and community
    discovery already has. Read-only: every OID here is a GET, and the module
    has no way to write.
    """
    say = log or (lambda _m: None)
    out = SwitchPtp(host=getattr(switch, "host", ""),
                    name=getattr(switch, "name", ""))
    client = getattr(switch, "client", None)
    if client is None:
        out.error = "no SNMP session"
        return out

    for label, root in TABLES:
        try:
            rows = _walk_table(client, root)
        except SnmpError as exc:
            out.error = str(exc)
            return out
        except Exception as exc:
            out.error = "%s: %s" % (type(exc).__name__, exc)
            return out
        if rows:
            out.tables[label] = rows

    if out.tables:
        out.supported = True
        out.source = "PTPBASE-MIB (RFC 8173)"
        say("  ptp over snmp: %s" % out.summary())
        return out

    for label, root in VENDOR_TREES:
        try:
            probe = _walk_table(client, root, limit=4)
        except Exception:
            continue
        if probe:
            out.vendor_trees.append(label)
    say("  ptp over snmp: %s" % out.summary())
    return out


def read_switch_ptp_safely(switch, **kwargs) -> SwitchPtp:
    """A timing read must never take the discovery that asked for it with it."""
    try:
        return read_switch_ptp(switch, **kwargs)
    except Exception as exc:
        return SwitchPtp(host=getattr(switch, "host", ""),
                         name=getattr(switch, "name", ""),
                         error="%s: %s" % (type(exc).__name__, exc))


def render(reports: List[SwitchPtp]) -> str:
    if not reports:
        return ""
    lines = ["PTP, switch side"]
    for report in reports:
        lines.append("  " + report.summary())
        for label in ("default", "parent", "current", "time_properties"):
            rows = report.tables.get(label)
            if not rows:
                continue
            lines.append("    %s:" % label)
            for key in sorted(rows)[:12]:
                lines.append("      %-22s %s" % (key, rows[key]))
    return "\n".join(lines)

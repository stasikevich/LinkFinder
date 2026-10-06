"""Whether the switch is actually using the cable it is plugged into.

A port can be up, negotiated, error-free and carrying nothing at all, because
spanning tree has put it in blocking. Nothing on the host side shows that: the
link is up, the counters are clean, and the traffic simply is not there. It is
one of the quieter ways for a redundant path to be a lie.

Standard BRIDGE-MIB only. Vendor trees carry far more - per-VLAN instances,
MSTP regions, root guard - and that is deliberately out of scope: this answers
"is this port forwarding", not "explain my spanning tree".

A redundant topology where STP is doing its job is not a fault. A blocked port
is reported as what it is - a deliberate decision by the network - and left for
a person to judge.

Unverified against hardware: the switch this was written beside runs no
spanning tree at all and publishes none of these objects, so the decoding is
covered by tests and has never been read off a live bridge. Treat its first
output on real gear with suspicion.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

OID_STP_PROTOCOL = "1.3.6.1.2.1.17.2.1.0"
OID_STP_TIME_SINCE_CHANGE = "1.3.6.1.2.1.17.2.3.0"
OID_STP_TOP_CHANGES = "1.3.6.1.2.1.17.2.4.0"
OID_STP_DESIGNATED_ROOT = "1.3.6.1.2.1.17.2.5.0"
OID_STP_PORT_STATE = "1.3.6.1.2.1.17.2.15.1.3"
OID_STP_PORT_ENABLE = "1.3.6.1.2.1.17.2.15.1.4"
OID_STP_PORT_COST = "1.3.6.1.2.1.17.2.15.1.5"

# RSTP folds blocking and listening into "discarding"; the MIB keeps the older
# names, so the words below are the MIB's and the meaning is spelled out.
PORT_STATE = {
    1: "disabled", 2: "blocking", 3: "listening", 4: "learning",
    5: "forwarding", 6: "broken",
}

CARRIES_TRAFFIC = ("forwarding",)
NOT_CARRYING = ("blocking", "listening", "learning", "broken", "disabled")

PROTOCOL = {1: "unknown", 2: "decLb100", 3: "IEEE 802.1D"}


@dataclass
class BridgeStp:
    """What one switch says about its spanning tree."""

    host: str = ""
    name: str = ""
    running: bool = False
    protocol: str = ""
    designated_root: str = ""
    topology_changes: Optional[int] = None
    seconds_since_change: Optional[float] = None
    ports: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return "STP on %s: %s" % (self.name or self.host, self.error)
        if not self.running:
            return "%s runs no spanning tree, or does not publish it" % (
                self.name or self.host)
        blocked = sum(1 for p in self.ports.values()
                      if p.get("state") in NOT_CARRYING and p.get("state") != "disabled")
        bits = ["%d port(s)" % len(self.ports)]
        if blocked:
            bits.append("%d not forwarding" % blocked)
        if self.topology_changes is not None:
            bits.append("%d topology change(s)" % self.topology_changes)
        return "%s: %s" % (self.name or self.host, ", ".join(bits))

    def to_dict(self) -> Dict[str, Any]:
        return {"host": self.host, "name": self.name, "running": self.running,
                "protocol": self.protocol, "designated_root": self.designated_root,
                "topology_changes": self.topology_changes,
                "seconds_since_change": self.seconds_since_change,
                "ports": {str(k): dict(v) for k, v in self.ports.items()},
                "error": self.error}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read(switch, log: Optional[Callable[[str], None]] = None) -> BridgeStp:
    """The bridge's own view, mapped from bridge ports onto ifIndex.

    dot1dStpPortTable is indexed by bridge port, not by ifIndex, which is the
    usual trap; dot1dBasePortIfIndex is the bridge between them and the SNMP
    layer already reads it for the forwarding table.
    """
    say = log or (lambda _m: None)
    from .snmp import SnmpError, as_text

    out = BridgeStp(host=getattr(switch, "host", ""),
                    name=getattr(switch, "name", ""))
    client = getattr(switch, "client", None)
    if client is None:
        out.error = "no SNMP session"
        return out
    try:
        protocol = _int(client.get_one(OID_STP_PROTOCOL))
        changes = _int(client.get_one(OID_STP_TOP_CHANGES))
        ticks = _int(client.get_one(OID_STP_TIME_SINCE_CHANGE))
        root = client.get_one(OID_STP_DESIGNATED_ROOT)
    except SnmpError as exc:
        out.error = str(exc)
        return out
    except Exception as exc:
        out.error = "%s: %s" % (type(exc).__name__, exc)
        return out

    if protocol is None and changes is None:
        return out

    out.running = True
    out.protocol = PROTOCOL.get(protocol or 0, "protocol %s" % protocol)
    out.topology_changes = changes
    out.seconds_since_change = ticks / 100.0 if ticks is not None else None
    out.designated_root = as_text(root) if root is not None else ""

    try:
        bridge_map = switch.bridge_to_ifindex()
    except Exception:
        bridge_map = {}

    def column(root_oid):
        found = {}
        try:
            for oid, value in client.walk(root_oid):
                index = _int(oid.rsplit(".", 1)[-1])
                if index is not None:
                    found[index] = value
        except Exception:
            return {}
        return found

    states = column(OID_STP_PORT_STATE)
    enabled = column(OID_STP_PORT_ENABLE)
    costs = column(OID_STP_PORT_COST)
    for bridge_port, raw in states.items():
        ifindex = bridge_map.get(bridge_port, bridge_port)
        out.ports[ifindex] = {
            "bridge_port": bridge_port,
            "state": PORT_STATE.get(_int(raw) or 0, "state %s" % raw),
            "enabled": _int(enabled.get(bridge_port)) == 1,
            "cost": _int(costs.get(bridge_port)),
        }
    say("  stp: %s" % out.summary())
    return out


def read_safely(switch, **kwargs) -> BridgeStp:
    """Spanning tree is enrichment; it never takes a discovery down with it."""
    try:
        return read(switch, **kwargs)
    except Exception as exc:
        return BridgeStp(host=getattr(switch, "host", ""),
                         name=getattr(switch, "name", ""),
                         error="%s: %s" % (type(exc).__name__, exc))


def check(port: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn one port's spanning-tree state into a verdict worth reading.

    Forwarding is the quiet case. Blocking is not a fault - it is the network
    doing its job on a redundant path - but it is the answer to "the cable is
    fine, so why is there no traffic", so it is surfaced rather than hidden.
    """
    from .linkcheck import VERDICT_MATCH, VERDICT_UNKNOWN, VERDICT_WARN

    if not port:
        return {"verdict": VERDICT_UNKNOWN,
                "detail": "no spanning-tree state for this port"}
    state = port.get("state", "")
    out: Dict[str, Any] = {"a": state, "verdict": VERDICT_MATCH}
    if state in CARRIES_TRAFFIC:
        out["confidence"] = "high"
        return out
    if state == "disabled":
        return {"a": state, "verdict": VERDICT_WARN, "confidence": "high",
                "detail": "spanning tree is disabled on this port",
                "evidence": ["dot1dStpPortState = disabled"]}
    if state == "broken":
        return {"a": state, "verdict": VERDICT_WARN, "confidence": "high",
                "detail": "the bridge reports this port as broken",
                "evidence": ["dot1dStpPortState = broken"]}
    return {"a": state, "verdict": VERDICT_WARN, "confidence": "high",
            "detail": "the link is up but spanning tree has it %s, so it carries "
                      "no traffic" % state,
            "evidence": ["dot1dStpPortState = %s" % state]}

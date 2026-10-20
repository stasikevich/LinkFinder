"""Data model shared by every discovery method.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List

UNKNOWN = ""


@dataclass
class Neighbor:
    """One discovered link: a local port and the switch port it is plugged into."""

    # Local side
    host: str = UNKNOWN              # target we interrogated (IP or name)
    local_iface: str = UNKNOWN       # eth0, vmnic3, NIC.Slot.1-1, ...
    local_mac: str = UNKNOWN
    local_speed: str = UNKNOWN       # "10000Mb/s" / "25G"
    local_role: str = UNKNOWN        # physical / bond-slave / bond / mgmt
    local_parent: str = UNKNOWN      # bond0 when this iface is a slave
    local_mtu: str = UNKNOWN
    local_netns: str = ""            # the pod/container holding this port, if any

    # Remote side (the answer)
    switch: str = UNKNOWN            # system name, else chassis id
    port: str = UNKNOWN              # port id, human readable
    port_desc: str = UNKNOWN
    chassis_id: str = UNKNOWN
    chassis_id_subtype: str = UNKNOWN
    port_id_subtype: str = UNKNOWN
    sys_desc: str = UNKNOWN          # switch model / firmware
    mgmt_ip: str = UNKNOWN           # switch management address
    vlan: str = UNKNOWN              # PVID / native vlan
    capabilities: str = UNKNOWN

    # Provenance
    proto: str = UNKNOWN             # LLDP / CDP
    source: str = UNKNOWN            # which method produced this (lldpctl, tcpdump, redfish, snmp-fdb...)
    confidence: str = "high"         # high | medium | low
    note: str = UNKNOWN

    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return bool(self.switch or self.chassis_id) and bool(self.port)

    @property
    def best_port(self) -> str:
        """Some switches advertise a bare index as Port ID and the real name as the
        description; the name is what you need to patch a cable."""
        if self.port.isdigit() and self.port_desc and not self.port_desc.isdigit():
            return self.port_desc
        return self.port or self.port_desc

    def fill_switch_name(self) -> "Neighbor":
        if not self.switch and self.chassis_id:
            self.switch = self.chassis_id
        if not self.port and self.port_desc:
            self.port = self.port_desc
        return self

    def key(self):
        return (self.host, self.local_iface, self.switch, self.port)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProbeAttempt:
    method: str
    ok: bool
    detail: str = UNKNOWN
    seconds: float = 0.0


@dataclass
class TargetResult:
    target: str
    kind: str = UNKNOWN              # linux / esxi / windows / bmc / switch
    neighbors: List[Neighbor] = field(default_factory=list)
    attempts: List[ProbeAttempt] = field(default_factory=list)
    error: str = UNKNOWN
    device_name: str = UNKNOWN       # the interrogated device's own name
    device_chassis: str = UNKNOWN    # and its own chassis id, when it tells us
    device_descr: str = UNKNOWN
    device_serial: str = UNKNOWN     # the number an inventory is keyed by
    extra: Dict[str, Any] = field(default_factory=dict)

    def add(self, nb: Neighbor) -> None:
        nb.fill_switch_name()
        if not nb.host:
            nb.host = self.target
        for existing in self.neighbors:
            if existing.key() == nb.key():
                return
        self.neighbors.append(nb)

    @property
    def resolved(self) -> bool:
        return any(n.resolved for n in self.neighbors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "kind": self.kind,
            "error": self.error,
            "device_name": self.device_name,
            "device_chassis": self.device_chassis,
            "device_descr": self.device_descr,
            "neighbors": [n.to_dict() for n in self.neighbors],
            "attempts": [asdict(a) for a in self.attempts],
        }


def results_to_json(results: List[TargetResult]) -> str:
    return json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False)


def _only_known(cls, held: Dict[str, Any]) -> Dict[str, Any]:
    """Keys this dataclass has, so an older or newer file still loads."""
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in (held or {}).items() if k in allowed}


def results_from_json(text: str) -> List[TargetResult]:
    """Read back what --json wrote, so a scan can be checked against a map later.

    A saved scan outlives the window it was taken in, which is the only way
    to hold one run against another without doing the whole thing again.
    """
    out: List[TargetResult] = []
    for held in json.loads(text) or []:
        if not isinstance(held, dict):
            continue
        result = TargetResult(**_only_known(TargetResult, dict(
            held, neighbors=[], attempts=[])))
        for raw in held.get("neighbors") or []:
            result.neighbors.append(Neighbor(**_only_known(Neighbor, raw)))
        for raw in held.get("attempts") or []:
            result.attempts.append(ProbeAttempt(**_only_known(ProbeAttempt, raw)))
        out.append(result)
    return out

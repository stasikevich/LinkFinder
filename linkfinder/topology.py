"""The topology graph: nodes, edges, identity and merging.

Pure data. Nothing here talks to a device - discovery fills it in, the renderer
reads it back out, and the JSON in between is the contract between the two.

Identity is chassis-id first because one device wears many addresses: a
router-on-a-stick answers on an address per VLAN, and keying on IP alone draws
it as a dozen separate switches.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1

STATE_UNSCANNED = "unscanned"
STATE_LIMIT = "limit-reached"
STATE_OUT_OF_SCOPE = "out-of-scope"
STATE_UNREACHABLE = "unreachable"
STATE_SCANNED = "scanned"

STATE_RANK = {
    STATE_UNSCANNED: 0,
    STATE_LIMIT: 1,
    STATE_OUT_OF_SCOPE: 2,
    STATE_UNREACHABLE: 3,
    STATE_SCANNED: 4,
}

CONFIDENCE_INFERRED = "inferred"

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"
VERDICT_WARN = "warn"

# Only these put a link on the problem list; "unknown" is an absence of
# evidence, not a fault, and saying otherwise would cry wolf on every link
# where we could only reach one end.
BAD_VERDICTS = (VERDICT_MISMATCH, VERDICT_WARN)
CONFIDENCE_REPORTED = "reported"
CONFIDENCE_CONFIRMED = "confirmed"

DISCOVERY_PROTOS = ("LLDP", "CDP", "MNDP/LLDP")
IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
HEXSEP_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F:.\- ]*[0-9a-fA-F]$")


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def normalise_chassis_id(value: str, subtype: str = "") -> str:
    """Fold the many spellings of a chassis id into one comparable string.

    Handles MACs written with colons, dashes, dots or nothing at all, in either
    case, and leaves genuinely non-MAC chassis ids (names, serials, addresses)
    as trimmed lower-case text - a chassis id is not always a MAC.
    """
    if not value:
        return ""
    text = str(value).strip().strip('"')
    if not text:
        return ""
    if IPV4_RE.match(text):
        return text
    hexonly = re.sub(r"[^0-9a-fA-F]", "", text)
    looks_mac = len(hexonly) == 12 and bool(HEXSEP_RE.match(text))
    if (subtype or "").lower() in ("mac", "macaddr", "mac-address", "4") and len(hexonly) == 12:
        looks_mac = True
    if looks_mac:
        return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2)).lower()
    return " ".join(text.split()).lower()


def is_mac_chassis(normalised: str) -> bool:
    return bool(re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", normalised or ""))


def _best(current: str, candidate: str) -> str:
    """Keep the more informative of two values; never let blank win."""
    candidate = (candidate or "").strip()
    current = (current or "").strip()
    if not candidate:
        return current
    if not current:
        return candidate
    if candidate.lower() == current.lower():
        return current
    return candidate if len(candidate) > len(current) else current


def _union(current: List[str], extra: Iterable[str]) -> List[str]:
    out = list(current)
    for item in extra or []:
        if item and item not in out:
            out.append(item)
    return out


def _earliest(a: str, b: str) -> str:
    pair = [x for x in (a, b) if x]
    return min(pair) if pair else ""


def _latest(a: str, b: str) -> str:
    pair = [x for x in (a, b) if x]
    return max(pair) if pair else ""


@dataclass
class Node:
    id: str
    kind: str = "unknown"
    name: str = ""
    chassis_id: str = ""
    chassis_id_raw: str = ""
    mgmt_ips: List[str] = field(default_factory=list)
    vendor: str = ""
    state: str = STATE_UNSCANNED
    depth: Optional[int] = None
    sources: List[str] = field(default_factory=list)
    serial: str = ""
    first_seen: str = ""
    last_seen: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def primary_ip(self) -> str:
        return self.mgmt_ips[0] if self.mgmt_ips else ""

    @property
    def label(self) -> str:
        return self.name or self.primary_ip or self.chassis_id or self.id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "name": self.name,
            "chassis_id": self.chassis_id, "chassis_id_raw": self.chassis_id_raw,
            "mgmt_ips": list(self.mgmt_ips), "vendor": self.vendor,
            "state": self.state, "depth": self.depth, "sources": list(self.sources),
            "serial": self.serial,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "error": self.error, "extra": dict(self.extra),
        }


@dataclass
class Edge:
    id: str
    a: str
    a_iface: str
    b: str
    b_iface: str
    protos: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    reported_by: List[str] = field(default_factory=list)
    vlan: str = ""
    speed: str = ""
    attrs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""

    @property
    def confidence(self) -> str:
        if len(self.reported_by) >= 2:
            return CONFIDENCE_CONFIRMED
        if any(p in DISCOVERY_PROTOS for p in self.protos):
            return CONFIDENCE_REPORTED
        return CONFIDENCE_INFERRED

    @property
    def problems(self) -> List[str]:
        """Which overlays came back with a verdict worth showing."""
        return sorted(name for name, layer in self.attrs.items()
                      if (layer or {}).get("verdict") in BAD_VERDICTS)

    def note(self, layer: str, **fields: Any) -> Dict[str, Any]:
        """Record one overlay on this link - VLAN, MTU, LACP, STP, optics.

        One physical link stays one edge; everything we learn about it goes
        into a named layer beside the others. Without this each new check
        would have added its own column to Edge, and the renderer would have
        needed a new case for every one of them.
        """
        layer_data = self.attrs.setdefault(layer, {})
        for key, value in fields.items():
            if value is not None and value != "":
                layer_data[key] = value
        return layer_data

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "a": self.a, "a_iface": self.a_iface,
            "b": self.b, "b_iface": self.b_iface,
            "protos": list(self.protos), "sources": list(self.sources),
            "reported_by": list(self.reported_by), "confidence": self.confidence,
            "vlan": self.vlan, "speed": self.speed,
            "attrs": {k: dict(v) for k, v in self.attrs.items()},
            "problems": list(self.problems),
            "first_seen": self.first_seen, "last_seen": self.last_seen,
        }


def node_key(chassis_id: str = "", mgmt_ip: str = "", name: str = "", mac: str = "") -> str:
    """Identity, best source first: chassis id, then address, then name."""
    chassis = normalise_chassis_id(chassis_id)
    if chassis:
        return "chassis:%s" % chassis
    if mgmt_ip:
        return "ip:%s" % mgmt_ip.strip()
    mac = normalise_chassis_id(mac)
    if mac:
        return "chassis:%s" % mac
    if name:
        return "name:%s" % " ".join(str(name).split()).lower()
    return ""


def edge_key(a: str, a_iface: str, b: str, b_iface: str) -> str:
    """One cable is one edge regardless of which end reported it."""
    ends = sorted([(a or "?", a_iface or "?"), (b or "?", b_iface or "?")])
    return "%s|%s::%s|%s" % (ends[0][0], ends[0][1], ends[1][0], ends[1][1])


class Topology:
    def __init__(self, seed: str = ""):
        self.seed = seed
        self.seeds: List[str] = [seed] if seed else []
        self.stopped_because = ""
        self.generated_at = now_stamp()
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, Edge] = {}
        self.extra: Dict[str, Any] = {}
        self._alias: Dict[str, str] = {}

    def resolve(self, node_id: str) -> str:
        seen = set()
        while node_id in self._alias and node_id not in seen:
            seen.add(node_id)
            node_id = self._alias[node_id]
        return node_id

    def get(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(self.resolve(node_id))

    def _find_by_serial(self, serial: str) -> Optional[Node]:
        """One box can answer with several chassis MACs; a serial still pins it down."""
        if not serial or len(serial) < 4:
            return None
        for node in self.nodes.values():
            if node.serial and node.serial.lower() == serial.lower():
                return node
        return None

    def _find_by_ip(self, mgmt_ips: Iterable[str], chassis: str) -> Optional[Node]:
        """An address only identifies a node while we have no chassis id for it."""
        for ip in mgmt_ips or []:
            for node in self.nodes.values():
                if ip not in node.mgmt_ips:
                    continue
                if not node.chassis_id or not chassis or node.chassis_id == chassis:
                    return node
        return None

    def add_node(self, chassis_id: str = "", mgmt_ip: str = "", name: str = "",
                 kind: str = "", vendor: str = "", state: str = STATE_UNSCANNED,
                 source: str = "", depth: Optional[int] = None, error: str = "",
                 mgmt_ips: Optional[List[str]] = None, mac: str = "", serial: str = "",
                 extra: Optional[Dict[str, Any]] = None) -> Node:
        addresses = [ip for ip in ([mgmt_ip] if mgmt_ip else []) + list(mgmt_ips or []) if ip]
        chassis = normalise_chassis_id(chassis_id) or normalise_chassis_id(mac)
        key = node_key(chassis_id=chassis_id, mgmt_ip=addresses[0] if addresses else "",
                       name=name, mac=mac)
        if not key:
            key = "unknown:%d" % (len(self.nodes) + 1)

        target = self.nodes.get(self.resolve(key))
        if target is None and serial:
            twin = self._find_by_serial(serial)
            if twin is not None:
                self._alias[key] = twin.id
                target = twin
        if target is None and chassis:
            older = self._find_by_ip(addresses, chassis)
            if older is not None and not older.chassis_id:
                target = self._promote(older, key, chassis, chassis_id)
        if target is None and not chassis:
            target = self._find_by_ip(addresses, "")

        stamp = now_stamp()
        if target is None:
            target = Node(id=key, chassis_id=chassis, chassis_id_raw=str(chassis_id or mac or ""),
                          first_seen=stamp)
            self.nodes[key] = target
        self._merge_into(target, chassis, chassis_id or mac, addresses, name, kind, vendor,
                         state, source, depth, error, extra, stamp)
        if serial and not target.serial:
            target.serial = serial
        return target

    def _promote(self, node: Node, new_key: str, chassis: str, chassis_raw: str) -> Node:
        """An IP-keyed node that just told us its chassis id becomes chassis-keyed."""
        old_key = node.id
        existing = self.nodes.get(new_key)
        if existing is not None and existing is not node:
            self._alias[old_key] = new_key
            self.nodes.pop(old_key, None)
            self._merge_node_into_node(existing, node)
            self._repoint_edges()
            return existing
        self.nodes.pop(old_key, None)
        node.id = new_key
        node.chassis_id = chassis
        node.chassis_id_raw = str(chassis_raw or "")
        self.nodes[new_key] = node
        self._alias[old_key] = new_key
        self._repoint_edges()
        return node

    def _merge_node_into_node(self, keep: Node, drop: Node) -> None:
        keep.mgmt_ips = _union(keep.mgmt_ips, drop.mgmt_ips)
        keep.sources = _union(keep.sources, drop.sources)
        keep.serial = keep.serial or drop.serial
        keep.name = _best(keep.name, drop.name)
        keep.vendor = _best(keep.vendor, drop.vendor)
        if keep.kind in ("", "unknown"):
            keep.kind = drop.kind
        keep.first_seen = _earliest(keep.first_seen, drop.first_seen)
        keep.last_seen = _latest(keep.last_seen, drop.last_seen)
        if STATE_RANK.get(drop.state, 0) > STATE_RANK.get(keep.state, 0):
            keep.state = drop.state
            keep.error = drop.error
        if keep.depth is None or (drop.depth is not None and drop.depth < keep.depth):
            keep.depth = drop.depth if drop.depth is not None else keep.depth
        for key, value in (drop.extra or {}).items():
            keep.extra.setdefault(key, value)

    def _merge_into(self, node: Node, chassis: str, chassis_raw: str, addresses: List[str],
                    name: str, kind: str, vendor: str, state: str, source: str,
                    depth: Optional[int], error: str, extra: Optional[Dict[str, Any]],
                    stamp: str) -> None:
        if chassis and not node.chassis_id:
            node.chassis_id = chassis
            node.chassis_id_raw = str(chassis_raw or "")
        node.mgmt_ips = _union(node.mgmt_ips, addresses)
        node.sources = _union(node.sources, [source] if source else [])
        node.name = _best(node.name, name)
        node.vendor = _best(node.vendor, vendor)
        if kind and kind != "unknown" and node.kind in ("", "unknown"):
            node.kind = kind
        node.first_seen = _earliest(node.first_seen, stamp)
        node.last_seen = _latest(node.last_seen, stamp)
        if depth is not None and (node.depth is None or depth < node.depth):
            node.depth = depth
        if STATE_RANK.get(state, 0) >= STATE_RANK.get(node.state, 0):
            node.state = state
            node.error = error if state != STATE_SCANNED else ""
        if state == STATE_SCANNED:
            node.error = ""
        for key, value in (extra or {}).items():
            node.extra[key] = value

    def add_edge(self, a: str, a_iface: str, b: str, b_iface: str, proto: str = "",
                 source: str = "", reported_by: str = "", vlan: str = "",
                 speed: str = "",
                 attrs: Optional[Dict[str, Dict[str, Any]]] = None) -> Edge:
        a, b = self.resolve(a), self.resolve(b)
        key = edge_key(a, a_iface, b, b_iface)
        stamp = now_stamp()
        edge = self.edges.get(key)
        if edge is None:
            ends = sorted([(a or "?", a_iface or "?"), (b or "?", b_iface or "?")])
            edge = Edge(id=key, a=ends[0][0], a_iface=ends[0][1],
                        b=ends[1][0], b_iface=ends[1][1], first_seen=stamp)
            self.edges[key] = edge
        edge.protos = _union(edge.protos, [proto] if proto else [])
        edge.sources = _union(edge.sources, [source] if source else [])
        edge.reported_by = _union(edge.reported_by,
                                  [self.resolve(reported_by)] if reported_by else [])
        edge.vlan = _best(edge.vlan, vlan)
        edge.speed = _best(edge.speed, speed)
        for layer, fields in (attrs or {}).items():
            edge.note(layer, **fields)
        edge.last_seen = _latest(edge.last_seen, stamp)
        return edge

    def _repoint_edges(self) -> None:
        rebuilt: Dict[str, Edge] = {}
        for edge in self.edges.values():
            a, b = self.resolve(edge.a), self.resolve(edge.b)
            key = edge_key(a, edge.a_iface, b, edge.b_iface)
            ends = sorted([(a, edge.a_iface), (b, edge.b_iface)])
            edge.id, edge.a, edge.a_iface, edge.b, edge.b_iface = (
                key, ends[0][0], ends[0][1], ends[1][0], ends[1][1])
            edge.reported_by = _union([], [self.resolve(r) for r in edge.reported_by])
            existing = rebuilt.get(key)
            if existing is None:
                rebuilt[key] = edge
                continue
            existing.protos = _union(existing.protos, edge.protos)
            existing.sources = _union(existing.sources, edge.sources)
            existing.reported_by = _union(existing.reported_by, edge.reported_by)
            existing.vlan = _best(existing.vlan, edge.vlan)
            existing.speed = _best(existing.speed, edge.speed)
            for layer, fields in edge.attrs.items():
                existing.note(layer, **fields)
            existing.first_seen = _earliest(existing.first_seen, edge.first_seen)
            existing.last_seen = _latest(existing.last_seen, edge.last_seen)
        self.edges = rebuilt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "seed": self.seed,
            "seeds": list(self.seeds),
            "extra": dict(self.extra),
            "stopped_because": self.stopped_because,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, sort_keys=False)

    def summary(self) -> str:
        by_state: Dict[str, int] = {}
        for node in self.nodes.values():
            by_state[node.state] = by_state.get(node.state, 0) + 1
        states = ", ".join("%d %s" % (count, state) for state, count in sorted(by_state.items()))
        by_conf: Dict[str, int] = {}
        for edge in self.edges.values():
            by_conf[edge.confidence] = by_conf.get(edge.confidence, 0) + 1
        links = ", ".join("%d %s" % (count, conf) for conf, count in sorted(by_conf.items()))
        return "%d device(s) [%s], %d link(s) [%s]" % (
            len(self.nodes), states or "-", len(self.edges), links or "-")


    def merge_from(self, data: Dict[str, Any]) -> int:
        """Absorb another topology through the same identity and merge rules."""
        mapping: Dict[str, str] = {}
        for raw in data.get("nodes", []) or []:
            node = self.add_node(
                chassis_id=raw.get("chassis_id") or raw.get("chassis_id_raw") or "",
                mgmt_ips=list(raw.get("mgmt_ips") or []),
                name=raw.get("name") or "", kind=raw.get("kind") or "",
                vendor=raw.get("vendor") or "", state=raw.get("state") or STATE_UNSCANNED,
                serial=raw.get("serial") or "", depth=raw.get("depth"),
                error=raw.get("error") or "", extra=dict(raw.get("extra") or {}))
            node.sources = _union(node.sources, raw.get("sources") or [])
            node.first_seen = _earliest(node.first_seen, raw.get("first_seen") or "")
            node.last_seen = _latest(node.last_seen, raw.get("last_seen") or "")
            if raw.get("id"):
                mapping[raw["id"]] = node.id
        for raw in data.get("edges", []) or []:
            a = mapping.get(raw.get("a"), raw.get("a") or "?")
            b = mapping.get(raw.get("b"), raw.get("b") or "?")
            protos = raw.get("protos") or [""]
            edge = None
            for proto in protos:
                edge = self.add_edge(a, raw.get("a_iface") or "", b, raw.get("b_iface") or "",
                                     proto=proto, vlan=raw.get("vlan") or "",
                                     speed=raw.get("speed") or "",
                                     attrs=raw.get("attrs") or None)
            if edge is None:
                continue
            edge.sources = _union(edge.sources, raw.get("sources") or [])
            edge.reported_by = _union(
                edge.reported_by,
                [self.resolve(mapping.get(r, r)) for r in (raw.get("reported_by") or [])])
            edge.first_seen = _earliest(edge.first_seen, raw.get("first_seen") or "")
            edge.last_seen = _latest(edge.last_seen, raw.get("last_seen") or "")
        for one in (data.get("seeds") or ([data["seed"]] if data.get("seed") else [])):
            if one and one not in self.seeds:
                self.seeds.append(one)
        if not self.seed and self.seeds:
            self.seed = self.seeds[0]
        self.extra.update(data.get("extra") or {})
        reason = (data.get("stopped_because") or "").strip()
        if reason and reason not in self.stopped_because:
            self.stopped_because = "; ".join(
                x for x in (self.stopped_because, reason) if x)
        return len(data.get("nodes") or [])


def merge_files(paths: List[str]) -> Topology:
    """Combine several scans into one graph: the usual way to build a site map."""
    combined = Topology()
    for path in paths:
        combined.merge_from(load_topology(path))
    combined.generated_at = now_stamp()
    return combined


def load_topology(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported topology schema_version %r (this build reads %d)"
                         % (data.get("schema_version"), SCHEMA_VERSION))
    return data

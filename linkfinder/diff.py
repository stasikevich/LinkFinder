"""What changed between two scans.

The question after "where is this plugged in" is almost always "and was it
plugged in there yesterday". A topology JSON is already a dated, complete
record, so comparing two of them needs no database and no agent - just the two
files.

Matching is the whole problem. Node ids are derived from whatever identity was
known at the time, so the same switch can be `chassis:00:05:80:...` in one file
and `ip:198.51.100.1` in another. Devices are therefore matched on the
identities they carry rather than on their id, and anything that cannot be
matched is reported as added or removed rather than quietly paired with the
wrong device.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DEVICE_ADDED = "device added"
DEVICE_REMOVED = "device removed"
LINK_ADDED = "link added"
LINK_REMOVED = "link removed"
DEVICE_CHANGED = "device changed"
LINK_CHANGED = "link changed"
LINK_MOVED = "link moved"

NODE_FIELDS = ("name", "kind", "state", "vendor", "serial")
EDGE_FIELDS = ("vlan", "speed", "confidence")


@dataclass
class Change:
    kind: str
    what: str
    detail: str = ""
    fields: List[Tuple[str, Any, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "what": self.what, "detail": self.detail,
                "fields": [{"field": f, "before": b, "after": a}
                           for f, b, a in self.fields]}


@dataclass
class Diff:
    before_at: str = ""
    after_at: str = ""
    changes: List[Change] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.changes

    def of_kind(self, kind: str) -> List[Change]:
        return [c for c in self.changes if c.kind == kind]

    def summary(self) -> str:
        if self.quiet:
            return "no change"
        counts: Dict[str, int] = {}
        for change in self.changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return ", ".join("%d %s" % (counts[k], k) for k in sorted(counts))

    def to_dict(self) -> Dict[str, Any]:
        return {"before_at": self.before_at, "after_at": self.after_at,
                "summary": self.summary(),
                "changes": [c.to_dict() for c in self.changes]}


def _identities(node: Dict[str, Any]) -> List[str]:
    """Every handle this device can be recognised by, strongest first."""
    out: List[str] = []
    chassis = (node.get("chassis_id") or "").strip().lower()
    if chassis:
        out.append("chassis:" + chassis)
    serial = (node.get("serial") or "").strip().lower()
    if serial:
        out.append("serial:" + serial)
    for address in node.get("mgmt_ips") or []:
        if address:
            out.append("ip:" + str(address).strip().lower())
    name = (node.get("name") or "").strip().lower()
    if name:
        out.append("name:" + name)
    return out


def match_nodes(before: List[Dict[str, Any]], after: List[Dict[str, Any]]
                ) -> Tuple[Dict[str, str], List[Dict], List[Dict]]:
    """Pair devices across two scans, and say which ones are only in one.

    A device is matched on the strongest identity the two files share, so a
    switch that gained a management address between scans is still the same
    switch, while two devices that merely share a name are not paired on that
    alone unless nothing better exists.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for node in before:
        for handle in _identities(node):
            index.setdefault(handle, node)

    pairs: Dict[str, str] = {}
    taken = set()
    unmatched_after: List[Dict] = []
    for node in after:
        found = None
        for handle in _identities(node):
            candidate = index.get(handle)
            if candidate is not None and candidate["id"] not in taken:
                found = candidate
                break
        if found is None:
            unmatched_after.append(node)
            continue
        pairs[node["id"]] = found["id"]
        taken.add(found["id"])

    unmatched_before = [n for n in before if n["id"] not in taken]
    return pairs, unmatched_before, unmatched_after


def _edge_ends(edge: Dict[str, Any], mapping: Dict[str, str]) -> Tuple[str, str]:
    """An edge keyed by the before-scan's node ids, so the two files line up."""
    a = mapping.get(edge.get("a", ""), edge.get("a", ""))
    b = mapping.get(edge.get("b", ""), edge.get("b", ""))
    ends = sorted([(a, edge.get("a_iface", "")), (b, edge.get("b_iface", ""))])
    return ("%s|%s" % ends[0], "%s|%s" % ends[1])


def _label(node: Optional[Dict[str, Any]], fallback: str = "?") -> str:
    if not node:
        return fallback
    ips = node.get("mgmt_ips") or []
    return node.get("name") or (ips[0] if ips else "") or node.get("chassis_id") or \
        node.get("id") or fallback


def _pretty_end(ends: set, lookup: Dict[str, Dict]) -> str:
    """"node-id|iface" back into something a person reads."""
    out = []
    for item in sorted(ends):
        node_id, _sep, iface = str(item).partition("|")
        out.append("%s %s" % (_label(lookup.get(node_id), node_id), iface))
    return ", ".join(out) or "?"


def _ptp_grandmasters(node: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for iface, port in ((node.get("extra") or {}).get("ptp") or {}).items():
        gm = (port or {}).get("gm_identity")
        if gm:
            out[iface] = gm
    return out


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> Diff:
    """Everything that moved between two topology files."""
    out = Diff(before_at=str(before.get("generated_at") or ""),
               after_at=str(after.get("generated_at") or ""))
    old_nodes = list(before.get("nodes") or [])
    new_nodes = list(after.get("nodes") or [])
    pairs, gone, arrived = match_nodes(old_nodes, new_nodes)
    old_by_id = {n["id"]: n for n in old_nodes}
    new_by_id = {n["id"]: n for n in new_nodes}

    for node in sorted(arrived, key=_label):
        out.changes.append(Change(DEVICE_ADDED, _label(node),
                                  detail=node.get("kind") or ""))
    for node in sorted(gone, key=_label):
        out.changes.append(Change(DEVICE_REMOVED, _label(node),
                                  detail=node.get("kind") or ""))

    for new_id, old_id in pairs.items():
        new_node, old_node = new_by_id[new_id], old_by_id[old_id]
        moved: List[Tuple[str, Any, Any]] = []
        for key in NODE_FIELDS:
            was, now = old_node.get(key) or "", new_node.get(key) or ""
            if was != now:
                moved.append((key, was, now))
        was_ips = sorted(old_node.get("mgmt_ips") or [])
        now_ips = sorted(new_node.get("mgmt_ips") or [])
        if was_ips != now_ips:
            moved.append(("mgmt_ips", ", ".join(was_ips), ", ".join(now_ips)))
        was_gm, now_gm = _ptp_grandmasters(old_node), _ptp_grandmasters(new_node)
        for iface in sorted(set(was_gm) | set(now_gm)):
            if was_gm.get(iface) != now_gm.get(iface):
                moved.append(("ptp grandmaster on %s" % iface,
                              was_gm.get(iface, "none"), now_gm.get(iface, "none")))
        if moved:
            out.changes.append(Change(DEVICE_CHANGED, _label(new_node), fields=moved))

    reverse = {v: k for k, v in pairs.items()}
    old_edges = {_edge_ends(e, {}): e for e in (before.get("edges") or [])}
    new_edges = {_edge_ends(e, pairs): e for e in (after.get("edges") or [])}

    def edge_label(edge: Dict[str, Any], lookup: Dict[str, Dict]) -> str:
        return "%s %s - %s %s" % (
            _label(lookup.get(edge.get("a", ""))), edge.get("a_iface", "?"),
            _label(lookup.get(edge.get("b", ""))), edge.get("b_iface", "?"))

    appeared = {k: e for k, e in new_edges.items() if k not in old_edges}
    vanished = {k: e for k, e in old_edges.items() if k not in new_edges}

    # A cable repatched to the next port is one event, not a removal plus an
    # unrelated arrival; reading it as two is how a short report turns noisy.
    # The pair has to share exactly one end - same device and interface on one
    # side, something different on the other.
    moved_keys = set()
    for new_key in sorted(appeared):
        ends = set(new_key)
        partner = next((k for k in sorted(vanished)
                        if k not in moved_keys and len(ends & set(k)) == 1), None)
        if partner is None:
            continue
        moved_keys.add(partner)
        was, now = vanished[partner], appeared[new_key]
        out.changes.append(Change(
            LINK_MOVED, edge_label(now, new_by_id),
            detail="was %s" % edge_label(was, old_by_id),
            fields=[("endpoint", _pretty_end(set(partner) - ends, old_by_id),
                     _pretty_end(ends - set(partner), new_by_id))]))
        appeared[new_key] = None

    for key, edge in sorted(appeared.items()):
        if edge is not None:
            out.changes.append(Change(LINK_ADDED, edge_label(edge, new_by_id)))
    for key, edge in sorted(vanished.items()):
        if key not in moved_keys:
            out.changes.append(Change(LINK_REMOVED, edge_label(edge, old_by_id)))

    for key, edge in sorted(new_edges.items()):
        old_edge = old_edges.get(key)
        if old_edge is None:
            continue
        moved = []
        for field_name in EDGE_FIELDS:
            was, now = old_edge.get(field_name) or "", edge.get(field_name) or ""
            if was != now:
                moved.append((field_name, was, now))
        was_attrs = old_edge.get("attrs") or {}
        now_attrs = edge.get("attrs") or {}
        for layer in sorted(set(was_attrs) | set(now_attrs)):
            was = (was_attrs.get(layer) or {}).get("verdict", "unknown")
            now = (now_attrs.get(layer) or {}).get("verdict", "unknown")
            if was != now:
                moved.append(("%s verdict" % layer, was, now))
        if moved:
            out.changes.append(Change(LINK_CHANGED, edge_label(edge, new_by_id),
                                      fields=moved))
    return out


def render(diff: Diff) -> str:
    """The report, grouped so the eye lands on what moved."""
    head = "Changes between %s and %s" % (diff.before_at or "the earlier scan",
                                          diff.after_at or "the later scan")
    if diff.quiet:
        return head + "\n\nNothing changed."
    lines = [head, "", diff.summary(), ""]
    for kind in (DEVICE_ADDED, DEVICE_REMOVED, LINK_ADDED, LINK_REMOVED,
                 LINK_MOVED, DEVICE_CHANGED, LINK_CHANGED):
        rows = diff.of_kind(kind)
        if not rows:
            continue
        lines.append("%s:" % kind.upper())
        for change in rows:
            suffix = " (%s)" % change.detail if change.detail else ""
            lines.append("  %s%s" % (change.what, suffix))
            for name, was, now in change.fields:
                lines.append("      %-26s %s -> %s" % (name, was or "none",
                                                       now or "none"))
        lines.append("")
    return "\n".join(lines).rstrip()


def compare_files(before_path: str, after_path: str) -> Diff:
    from .topology import load_topology

    return compare(load_topology(before_path), load_topology(after_path))

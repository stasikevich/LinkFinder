"""What does not add up on this network, gathered into one answer.

The pieces already existed and almost nobody could reach them: duplicate
addresses and MACs lived behind one command-line path, rings behind
another, and neither appeared on the map at all. A detector nobody can
open is a detector nobody has.

Nothing here decides anything. Each finding carries what was seen and how
sure it is, because every one of these can be innocent - a gateway that
answers for a subnet looks exactly like a stolen address until somebody
who knows the bench looks at it.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# What each kind is about, in the words a person would use to ask for it.
ABOUT = {
    "duplicate address": "two devices answering on one address",
    "duplicate mac": "one hardware address in two places",
    "ring": "a loop in the cabling",
    "link": "the two ends of a cable disagree",
}


def _nodes_for_address(topology: Dict[str, Any], macs: Any) -> List[str]:
    """Which drawn devices carry any of these hardware addresses."""
    wanted = {str(m).lower() for m in (macs or []) if str(m).strip()}
    if not wanted:
        return []
    out = []
    for node in topology.get("nodes") or []:
        known = {str(node.get("chassis_id") or "").lower()}
        known |= {str(v).lower()
                  for v in ((node.get("extra") or {}).get("macs") or {}).values()}
        if known & wanted:
            out.append(str(node.get("id")))
    return out


def from_scan(results) -> List[Dict[str, Any]]:
    """Duplicate addresses and MACs, from what the scanned machines had heard."""
    from . import duplicates

    out: List[Dict[str, Any]] = []
    try:
        found = duplicates.find(results)
    except Exception as why:
        # Never return "nothing wrong" because the check itself broke.
        return [{"kind": "duplicate address", "what": "could not be checked",
                 "verdict": "UNCLEAR",
                 "detail": "looking for duplicates failed: %s" % why,
                 "evidence": "", "confidence": "", "macs": [], "devices": []}]
    for one in found.get("ips") or []:
        out.append({"kind": "duplicate address",
                    "what": str(one.get("address") or ""),
                    "verdict": one.get("verdict", ""),
                    "detail": one.get("detail", ""),
                    "evidence": one.get("evidence", ""),
                    "confidence": one.get("confidence", ""),
                    "macs": one.get("macs") or []})
    for one in found.get("macs") or []:
        out.append({"kind": "duplicate mac",
                    "what": str(one.get("mac") or ""),
                    "verdict": one.get("verdict", ""),
                    "detail": one.get("detail", ""),
                    "evidence": one.get("evidence", ""),
                    "confidence": one.get("confidence", ""),
                    "macs": [one.get("mac")] if one.get("mac") else []})
    return out


def from_map(topology: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rings, and the cables whose two ends disagree."""
    from . import loops
    from .topology import Topology

    out: List[Dict[str, Any]] = []
    # loops works on the graph object, not the plain record. Handing it the
    # wrong one raised, and a bare except swallowed it - which is how a
    # detector reports "nothing wrong" for ever.
    graph = Topology()
    try:
        graph.merge_from(topology)
        rings = loops.examine(graph)
    except Exception as why:
        return [{"kind": "ring", "what": "could not be checked",
                 "verdict": "UNCLEAR",
                 "detail": "looking for rings failed: %s" % why,
                 "evidence": "", "confidence": "", "macs": [], "devices": []}]
    for one in rings:
        out.append({"kind": "ring",
                    "what": one.get("kind", "ring"),
                    "verdict": one.get("verdict", ""),
                    "detail": one.get("detail", ""),
                    "evidence": one.get("evidence", ""),
                    "confidence": one.get("confidence", ""),
                    "macs": []})
    for edge in topology.get("edges") or []:
        problems = edge.get("problems") or []
        if not problems:
            continue
        said = [p for p in problems
                if str((p or {}).get("verdict", "")).lower() != "match"]
        if not said:
            continue
        out.append({
            "kind": "link",
            "what": "%s %s - %s %s" % (edge.get("a", ""), edge.get("a_iface", ""),
                                       edge.get("b", ""), edge.get("b_iface", "")),
            "verdict": said[0].get("verdict", ""),
            "detail": "; ".join(str(p.get("detail") or p.get("what") or "")
                                for p in said),
            "evidence": "both ends of the cable were read",
            "confidence": "HIGH",
            "macs": [],
            "devices": [str(edge.get("a") or ""), str(edge.get("b") or "")]})
    return out


def _as_text(value: Any) -> str:
    """Evidence arrives as a line or as several; a reader wants a sentence."""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value if str(v).strip())
    return str(value or "")


def gather(topology: Optional[Dict[str, Any]] = None,
           results: Any = None) -> List[Dict[str, Any]]:
    """Everything that does not add up, worst first."""
    out: List[Dict[str, Any]] = []
    if results:
        out.extend(from_scan(results))
    if topology:
        out.extend(from_map(topology))
        for one in out:
            if "devices" not in one:
                one["devices"] = _nodes_for_address(topology, one.get("macs"))
    for one in out:
        one["evidence"] = _as_text(one.get("evidence"))
        one["detail"] = _as_text(one.get("detail"))
    order = {"MISMATCH": 0, "CONFLICT": 0, "SUSPECT": 1, "WARN": 1,
             "LIKELY": 2, "UNCLEAR": 3}
    out.sort(key=lambda f: (order.get(str(f.get("verdict", "")).upper(), 4),
                            f.get("kind", "")))
    return out


def summary(found: List[Dict[str, Any]]) -> str:
    """One line a person can read before deciding to look."""
    if not found:
        return "nothing on this map contradicts itself"
    counted: Dict[str, int] = {}
    for one in found:
        counted[one["kind"]] = counted.get(one["kind"], 0) + 1
    return ", ".join("%d %s" % (n, ABOUT.get(k, k))
                     for k, n in sorted(counted.items()))

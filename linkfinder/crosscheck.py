"""Did pressing Map give the same answer as pressing Scan?

A scan is the links off one device; a map is those same links drawn between
devices. The map is allowed to know MORE - it asks more devices, and the far
end may report a cable we never heard - but it may never know LESS about a
device both of them reached. Anything the scan resolved and the map does not
carry is a defect in the conversion, not a difference of opinion, and it is
reported as one.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

# local port, who is on the other end, which port of theirs
Link = Tuple[str, str, str]


def far_identity(chassis: str = "", mgmt_ip: str = "", name: str = "") -> str:
    """One name for a far end, chosen the same way on both sides of the check.

    Chassis id first because it is the only thing that survives a rename and
    a re-addressing, then the address, then what the device calls itself.
    """
    for part in (chassis, mgmt_ip, name):
        text = str(part or "").strip().lower()
        if text:
            return text
    return ""


def links_from_scan(results) -> Dict[str, Set[Link]]:
    """Every link the scan resolved, keyed by the address that was scanned."""
    from .neighbors import neighbour_identity

    out: Dict[str, Set[Link]] = {}
    for result in results or []:
        target = str(getattr(result, "target", "") or "")
        if not target:
            continue
        out.setdefault(target, set())
        for nb in getattr(result, "neighbors", None) or []:
            if not nb.resolved:
                continue
            chassis, mgmt_ip, name, _kind = neighbour_identity(nb)
            who = far_identity(chassis, mgmt_ip, name)
            if not who:
                continue
            out[target].add((nb.local_iface or "", who, nb.best_port or ""))
    return out


def shared_addresses(topology: Dict[str, Any]) -> Set[str]:
    """Addresses that more than one device on the map holds.

    Link-local fronthaul addresses repeat behind every radio head, so keying
    links by address alone would quietly merge two devices into one and hide
    a link that went missing. Such an address is not compared at all, and the
    check says why rather than returning a clean result it cannot stand by.
    """
    owners: Dict[str, Set[str]] = {}
    for node in topology.get("nodes") or []:
        for address in node.get("mgmt_ips") or []:
            owners.setdefault(str(address), set()).add(str(node.get("id") or ""))
    return {address for address, held in owners.items() if len(held) > 1}


def links_from_map(topology: Dict[str, Any]) -> Dict[str, Set[Link]]:
    """The same shape, read off the finished map, keyed by every address a node holds."""
    nodes = {str(n.get("id") or ""): n for n in topology.get("nodes") or []}
    out: Dict[str, Set[Link]] = {}
    for edge in topology.get("edges") or []:
        for near, near_iface, far, far_iface in (("a", "a_iface", "b", "b_iface"),
                                                 ("b", "b_iface", "a", "a_iface")):
            node = nodes.get(str(edge.get(near) or "")) or {}
            other = nodes.get(str(edge.get(far) or "")) or {}
            others = other.get("mgmt_ips") or []
            who = far_identity(other.get("chassis_id") or "",
                               others[0] if others else "",
                               other.get("name") or "")
            if not who:
                continue
            link = (edge.get(near_iface) or "", who, edge.get(far_iface) or "")
            for address in node.get("mgmt_ips") or []:
                out.setdefault(str(address), set()).add(link)
    return out


def compare(results, topology: Dict[str, Any],
            reused: Optional[Set[str]] = None) -> List[str]:
    """What the scan knows and the map lost, in words an engineer can act on.

    A device the crawl took from the scan instead of asking again cannot
    disagree with it, so comparing the two would always pass and would prove
    nothing. Those addresses are named and left out.
    """
    scanned = links_from_scan(results)
    mapped = links_from_map(topology)
    ambiguous = shared_addresses(topology)
    reached = set()
    for node in topology.get("nodes") or []:
        for address in node.get("mgmt_ips") or []:
            reached.add(str(address))

    complaints: List[str] = []
    for target in sorted(scanned):
        wanted = scanned[target]
        if not wanted:
            continue
        if target not in reached:
            continue
        if target in (reused or set()):
            complaints.append(
                "%s: not compared - the map reused the scan for this device"
                " instead of asking it again" % target)
            continue
        if target in ambiguous:
            complaints.append(
                "%s: not compared - more than one device on the map holds"
                " this address" % target)
            continue
        have = mapped.get(target, set())
        for iface, who, port in sorted(wanted - have):
            complaints.append(
                "%s: the scan found %s on %s, the map has no such link"
                % (target, "%s - %s" % (who, port) if port else who, iface))
    return complaints

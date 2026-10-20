"""Rings in the topology, and whether anything is actually wrong with one.

A redundant path is not a fault. A ring with spanning tree blocking one leg is
a correctly built network, and a tool that called it a loop would be wrong in
the most expensive way - it would be ignored the next time, when it was right.

So the ring itself is free arithmetic on the graph already built, and is only
ever reported as a fact. What turns it into a finding is a second, independent
signal: every port around the ring forwarding, a bridge whose topology changed
seconds ago, a change counter still moving while we watch, or a MAC that
jumped ports between two reads.

Deciding whether a network is chronically unstable needs observations over
days, which is what comparing snapshots is for. This answers the narrower and
more useful question: is it churning right now.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

RECENT_CHANGE_SECONDS = 120
MAX_CYCLES = 12


def cycles(topology, limit: int = MAX_CYCLES) -> List[List[str]]:
    """Fundamental cycles of the graph, as lists of node ids.

    A spanning forest is grown and every edge that would close a ring is a
    back edge; walking the two ends up to their common ancestor recovers the
    ring. That finds one independent cycle per redundant path, which is what a
    person wants to see, rather than the exponential set of every ring that
    can be drawn through them.
    """
    adjacency: Dict[str, List[Tuple[str, str]]] = {}
    for edge in topology.edges.values():
        # A cable from a device back into itself is a real thing to draw, but
        # it is not a redundant path between two devices, which is all a ring
        # means here. Left in, it reads as a loop that needs breaking.
        if edge.a == edge.b:
            adjacency.setdefault(edge.a, [])
            continue
        adjacency.setdefault(edge.a, []).append((edge.b, edge.id))
        adjacency.setdefault(edge.b, []).append((edge.a, edge.id))

    parent: Dict[str, Optional[str]] = {}
    parent_edge: Dict[str, str] = {}
    depth: Dict[str, int] = {}
    seen_edges = set()
    found: List[List[str]] = []

    for root in sorted(adjacency):
        if root in parent:
            continue
        parent[root] = None
        depth[root] = 0
        stack = [root]
        while stack:
            node = stack.pop()
            for neighbour, edge_id in adjacency.get(node, []):
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                if neighbour not in parent:
                    parent[neighbour] = node
                    parent_edge[neighbour] = edge_id
                    depth[neighbour] = depth[node] + 1
                    stack.append(neighbour)
                    continue
                ring = _ring_between(node, neighbour, parent, depth)
                if ring and len(ring) > 2:
                    found.append(ring)
                    if len(found) >= limit:
                        return found
    return found


def _ring_between(one: str, other: str, parent: Dict[str, Optional[str]],
                  depth: Dict[str, int]) -> List[str]:
    left, right = [one], [other]
    a, b = one, other
    while depth.get(a, 0) > depth.get(b, 0):
        a = parent.get(a)
        if a is None:
            return []
        left.append(a)
    while depth.get(b, 0) > depth.get(a, 0):
        b = parent.get(b)
        if b is None:
            return []
        right.append(b)
    while a != b:
        a, b = parent.get(a), parent.get(b)
        if a is None or b is None:
            return []
        left.append(a)
        right.append(b)
    return left + list(reversed(right[:-1]))


def _edges_on(topology, ring: List[str]) -> List[Any]:
    wanted = set()
    for i, node in enumerate(ring):
        wanted.add(frozenset((node, ring[(i + 1) % len(ring)])))
    return [e for e in topology.edges.values()
            if frozenset((e.a, e.b)) in wanted]


def describe_ring(topology, ring: List[str]) -> str:
    """The ring as a line somebody can follow round.

    Told apart by address where a name is shared: two machines really can
    both answer to "server1", and a ring written as server1 - sw - server1
    then reads as nonsense or, worse, as a loop that is not there.
    """
    from .render_topology import unique_labels

    named = unique_labels([node.to_dict() for node in topology.nodes.values()])
    labels = [named.get(node_id) or node_id for node_id in ring]
    return " - ".join(labels + [labels[0]])


ENDPOINT_KINDS = ("host", "bmc", "phone")


def forwards(node) -> bool:
    """Can this device pass a frame from one of its ports to another?

    A ring only matters where traffic can go round it. A switch forwards; a
    plain host does not - its NICs are endpoints, and a cable between two of
    them carries nothing onward. Without this, every direct cable that also
    has a path through a switch is reported as a ring, which on a lab with a
    few parallel cables buries the one ring that would really hurt.

    A Linux host that runs a bridge does forward, so the probe records its
    bridges and they count.
    """
    if (getattr(node, "extra", {}) or {}).get("bridges"):
        return True
    # Refuse only where we positively know the device is an endpoint. A
    # chassis id seen in somebody's neighbour table could be a switch we
    # never reached, and silently dropping a ring through it would hide the
    # one finding this check exists for.
    return getattr(node, "kind", "") not in ENDPOINT_KINDS


def ring_can_loop(topology, ring) -> bool:
    """True when every device on the ring would actually pass traffic round it."""
    for node_id in ring:
        node = topology.nodes.get(node_id)
        if node is None or not forwards(node):
            return False
    return True


def fold_same_ring(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One line per ring, however many spare cables close it.

    Four parallel cables between the same pair of machines make four
    independent cycles, and the graph is right to say so. A person reading
    it sees the same three devices listed four times and stops reading. It
    is one ring, held shut by four cables, and that is what it should say.
    """
    first: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for one in found:
        key = str(one.get("detail") or "")
        if key not in first:
            first[key] = dict(one)
            first[key]["paths"] = 1
            order.append(key)
        else:
            first[key]["paths"] += 1
    out = []
    for key in order:
        one = first[key]
        spare = one.pop("paths", 1)
        if spare > 1:
            one["detail"] = "%s - %d separate cables close it" % (key, spare)
            one["evidence"] = list(one.get("evidence") or []) + [
                "%d redundant paths between the same devices" % spare]
        out.append(one)
    return out


def examine(topology, blocked_ports: Optional[Dict[str, List[str]]] = None,
            log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """Rings in the graph, each with whether spanning tree is holding it open.

    `blocked_ports` maps a node id to the interface names that bridge reported
    as not forwarding. Without it the ring is still reported, as a ring, with
    the honest note that we could not see whether anything is blocking.
    """
    say = log or (lambda _m: None)
    blocked_ports = blocked_ports or {}
    out: List[Dict[str, Any]] = []
    skipped = 0
    for ring in cycles(topology):
        if not ring_can_loop(topology, ring):
            skipped += 1
            continue
        edges = _edges_on(topology, ring)
        holding = []
        for edge in edges:
            for node_id, iface in ((edge.a, edge.a_iface), (edge.b, edge.b_iface)):
                if iface in blocked_ports.get(node_id, []):
                    holding.append("%s %s" % (
                        topology.nodes[node_id].label
                        if node_id in topology.nodes else node_id, iface))
        path = describe_ring(topology, ring)
        if holding:
            out.append({
                "kind": "ring", "verdict": "match", "confidence": "high",
                "detail": "%s - spanning tree is holding it open at %s"
                          % (path, ", ".join(sorted(holding))),
                "evidence": ["dot1dStpPortState is not forwarding on %s" % port
                             for port in sorted(holding)],
            })
            continue
        knew = any(node_id in blocked_ports for node_id in ring)
        out.append({
            "kind": "ring", "verdict": "warn" if knew else "unknown",
            "confidence": "high" if knew else "low",
            "detail": "%s - every port around it is forwarding" % path if knew
                      else "%s - no spanning-tree state available for it" % path,
            "evidence": ["ring of %d devices in the discovered topology"
                         % len(ring)],
        })
    out = fold_same_ring(out)
    for finding in out:
        say("  loop: %s" % finding["detail"])
    if skipped:
        say("  %d cycle(s) pass through a device that does not forward - "
            "not reported as rings" % skipped)
    return out


def sample_instability(switch, seconds: float = 5.0,
                       sleeper: Callable[[float], None] = time.sleep,
                       log: Optional[Callable[[str], None]] = None
                       ) -> Dict[str, Any]:
    """Two reads a few seconds apart: is this bridge churning right now?

    The same technique the port counters use, for the same reason. One reading
    of a topology-change counter is a number with no meaning; the difference
    between two is an event rate. A MAC that is on a different port in the
    second read has moved while we were looking.
    """
    say = log or (lambda _m: None)
    from .snmp import SnmpError
    from .stp import OID_STP_TIME_SINCE_CHANGE, OID_STP_TOP_CHANGES

    out: Dict[str, Any] = {"host": getattr(switch, "host", ""),
                           "name": getattr(switch, "name", ""), "signals": []}
    client = getattr(switch, "client", None)
    if client is None:
        return out

    def changes() -> Optional[int]:
        try:
            value = client.get_one(OID_STP_TOP_CHANGES)
            return int(value) if value is not None else None
        except (SnmpError, TypeError, ValueError):
            return None

    def fdb() -> Dict[str, Any]:
        try:
            return {mac: where for mac, where in (switch.fdb() or {}).items()}
        except Exception:
            return {}

    first_changes, first_fdb = changes(), fdb()
    try:
        ticks = client.get_one(OID_STP_TIME_SINCE_CHANGE)
        since = int(ticks) / 100.0 if ticks is not None else None
    except (SnmpError, TypeError, ValueError):
        since = None
    if since is not None and since < RECENT_CHANGE_SECONDS:
        out["signals"].append({
            "name": "recent topology change",
            "detail": "the last spanning-tree change was %.0f s ago" % since,
        })

    sleeper(seconds)
    second_changes, second_fdb = changes(), fdb()

    if first_changes is not None and second_changes is not None:
        moved = second_changes - first_changes
        if moved > 0:
            out["signals"].append({
                "name": "topology changing",
                "detail": "dot1dStpTopChanges rose by %d during a %.0f s sample"
                          % (moved, seconds),
            })

    jumped = [mac for mac, where in second_fdb.items()
              if mac in first_fdb and first_fdb[mac] != where]
    if jumped:
        out["signals"].append({
            "name": "MAC moved",
            "detail": "%d address(es) changed port during the sample: %s"
                      % (len(jumped), ", ".join(sorted(jumped)[:4])),
        })

    out["verdict"] = "warn" if len(out["signals"]) >= 2 else "match"
    out["confidence"] = "high" if len(out["signals"]) >= 3 else (
        "medium" if len(out["signals"]) >= 2 else "high")
    if out["signals"]:
        say("  loop: %s has %d instability signal(s)"
            % (out["name"] or out["host"], len(out["signals"])))
    return out


def render(rings: List[Dict[str, Any]],
           churn: Optional[List[Dict[str, Any]]] = None) -> str:
    """Rings first, then anything that is moving while we watch."""
    lines: List[str] = []
    if rings:
        lines.append("Rings in the topology")
        # Parallel cables between the same two devices produce one independent
        # cycle each, which is correct arithmetic and a useless thing to read
        # three times. Identical wording is folded into one line with a count.
        seen: Dict[str, int] = {}
        order: List[str] = []
        for finding in rings:
            detail = finding["detail"]
            if detail not in seen:
                order.append(detail)
            seen[detail] = seen.get(detail, 0) + 1
        for detail in order:
            times = seen[detail]
            lines.append("  %s%s" % (detail, "" if times == 1
                                     else "  (x%d - parallel links)" % times))
    for bridge in churn or []:
        if not bridge.get("signals"):
            continue
        verdict = bridge.get("verdict", "match")
        head = ("STP instability on %s" if verdict == "warn"
                else "Activity on %s") % (bridge.get("name") or bridge.get("host"))
        lines.append(head)
        for signal in bridge["signals"]:
            lines.append("  %s" % signal["detail"])
        lines.append("  Confidence: %s" % bridge.get("confidence", "medium").upper())
        if verdict != "warn":
            lines.append("  One signal on its own is movement, not instability.")
    return "\n".join(lines)


def blocked_on_rings(topology, rings, community: str = "public",
                     log=None) -> Dict[str, List[str]]:
    """Spanning-tree state, read only for the bridges a ring passes through.

    Demand-driven on purpose: a network with no ring needs none of this, and
    a network with one needs it for a handful of devices rather than all of
    them.
    """
    from .netaddr import is_reachable_mgmt
    from .probe_snmp import build_switches
    from .stp import read_safely as read_stp

    say = log or (lambda _m: None)
    wanted = {node_id for ring in rings for node_id in ring}
    out: Dict[str, List[str]] = {}
    for node_id in sorted(wanted):
        node = topology.nodes.get(node_id)
        if node is None:
            continue
        address = next((ip for ip in node.mgmt_ips if is_reachable_mgmt(ip)), "")
        if not address:
            continue
        switch = build_switches([address], community)[0]
        if not switch.identify():
            continue
        tree = read_stp(switch, log=say)
        if not tree.running:
            continue
        names = {}
        try:
            names = switch.if_name()
        except Exception:
            names = {}
        out[node_id] = [names.get(ifindex, str(ifindex))
                        for ifindex, port in tree.ports.items()
                        if port.get("state") != "forwarding"]
    return out

"""When the two ends of a cable do not tell the same story.

LLDP is each device's own account of what it can see, and two accounts of the
same cable can disagree. A switch that says "my Eth1/17 faces server01 eno1"
while server01 says "my eno1 faces Eth1/18" means one of them is stale, one of
them is lying, or the cable moved between the two readings. Either way the map
is about to be wrong, and silently picking a winner would make it wrong without
saying so.

This does not try to resolve the disagreement. It records that there is one, on
the link, with both accounts and who gave them, and leaves the judgement to
somebody who can walk to the rack.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re

from typing import Any, Callable, Dict, List, Optional, Tuple


def _pair(edge) -> Tuple[str, str]:
    return tuple(sorted([edge.a, edge.b]))


def _side(edge, node_id: str) -> Tuple[str, str]:
    """(our interface, their interface) seen from one end of the edge."""
    if edge.a == node_id:
        return edge.a_iface, edge.b_iface
    return edge.b_iface, edge.a_iface


# A host-side virtual port - a VM's tap (vnet), a bridge, a container veth - is
# not the physical end of a cable, so a real port appearing to "face" one of
# these as well as its true peer is not a contradiction. Counting them is what
# lit a working fronthaul link red: enp9s0 faced iperf2 AND vnet1, and vnet1 is
# a VM tap on the same host, not a second cable.
_VIRTUAL_IFACE = re.compile(
    r"^(vnet|veth|tap\d|tun\d|virbr|docker|br-|cni|flannel|cali|cilium|lxc|"
    r"macvtap|vif|fwbr|fwln|fwpr|ovs-|qbr|qvo|qvb|vhost|vmnet)", re.I)


def _virtual_iface(name: str) -> bool:
    return bool(_VIRTUAL_IFACE.match(str(name or "").strip().lower()))


def find(topology, log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """Links where one port is claimed to face more than one port on one device.

    A port has one cable in it. If the graph holds two edges putting the same
    local port against different remote ports of the same device, the two
    reports cannot both be current.
    """
    say = log or (lambda _m: None)
    by_pair: Dict[Tuple[str, str], List[Any]] = {}
    for edge in topology.edges.values():
        by_pair.setdefault(_pair(edge), []).append(edge)

    found: List[Dict[str, Any]] = []
    for pair, edges in by_pair.items():
        if len(edges) < 2:
            continue
        for node_id in pair:
            seen: Dict[str, List[Any]] = {}
            for edge in edges:
                local, _remote = _side(edge, node_id)
                if local and local != "?":
                    seen.setdefault(local, []).append(edge)
            for local, claims in seen.items():
                if _virtual_iface(local):
                    continue
                remotes = sorted({_side(e, node_id)[1] for e in claims})
                remotes = [r for r in remotes
                           if r and r != "?" and not _virtual_iface(r)]
                if len(remotes) < 2:
                    continue
                other = pair[0] if pair[1] == node_id else pair[1]
                detail = ("%s is reported facing %s on %s"
                          % (_label(topology, node_id) + " " + local,
                             " and ".join(remotes), _label(topology, other)))
                evidence = []
                for edge in claims:
                    who = ", ".join(_label(topology, r) for r in edge.reported_by) \
                        or "unknown"
                    evidence.append("%s - %s, reported by %s"
                                    % (local, _side(edge, node_id)[1], who))
                for edge in claims:
                    edge.note("lldp", verdict="mismatch", confidence="high",
                              a=local, b=", ".join(remotes), detail=detail,
                              evidence=evidence)
                found.append({"node": node_id, "iface": local, "other": other,
                              "remotes": remotes, "detail": detail,
                              "evidence": evidence})
                say("  lldp disagreement: %s" % detail)
    return found


def _label(topology, node_id: str) -> str:
    node = topology.nodes.get(node_id)
    return node.label if node is not None else node_id


def _ipv4_on(node, iface) -> List[Tuple[str, int]]:
    """(address, prefix) IPv4s configured on one interface of a node."""
    out: List[Tuple[str, int]] = []
    port = ((node.extra or {}).get("ports") or {}).get(iface) or {}
    for a in port.get("addresses") or []:
        a = str(a)
        if ":" in a or "/" not in a:          # skip IPv6 and address-less
            continue
        ip, _slash, mask = a.partition("/")
        if ip.count(".") == 3 and mask.isdigit():
            out.append((ip, int(mask)))
    return out


def _network(ip: str, prefix: int) -> str:
    import ipaddress
    try:
        return str(ipaddress.ip_network("%s/%d" % (ip, prefix), strict=False).network_address)
    except ValueError:
        return ""


def subnets(topology, log: Optional[Callable[[str], None]] = None
            ) -> List[Dict[str, Any]]:
    """The two ends of a cable that cannot talk: different subnet, or the same
    subnet with two different masks. A VLAN carries one subnet; a port on the
    wrong one is a config error that leaves a link "up" and dead."""
    say = log or (lambda _m: None)
    found: List[Dict[str, Any]] = []
    for edge in topology.edges.values():
        if edge.a == edge.b:
            continue
        na, nb = topology.get(edge.a), topology.get(edge.b)
        if na is None or nb is None:
            continue
        aa, bb = _ipv4_on(na, edge.a_iface), _ipv4_on(nb, edge.b_iface)
        if not aa or not bb:
            continue
        verdict = detail = ""
        for (ia, pa) in aa:
            for (ib, pb) in bb:
                neta, netb = _network(ia, pa), _network(ib, pb)
                if neta and netb and neta == netb and pa != pb:
                    verdict = "mismatch"
                    detail = ("same subnet, two masks: %s/%d vs %s/%d - one end "
                              "is misconfigured" % (ia, pa, ib, pb))
                elif neta and netb and neta != netb:
                    if not verdict:
                        verdict = "warn"
                        detail = ("ends are on different subnets: %s/%d vs %s/%d "
                                  "- they cannot talk over this link" % (ia, pa, ib, pb))
        if verdict:
            edge.note("subnet", verdict=verdict, confidence="high", detail=detail)
            found.append({"edge": edge.id, "verdict": verdict, "detail": detail})
    if found:
        say("  subnet: %d link(s) whose ends cannot talk" % len(found))
    return found

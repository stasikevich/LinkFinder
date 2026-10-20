"""One bundle, one link.

A bond is two or three cables doing the job of one. Drawn member by member
the map says a server has four separate connections to the same switch, which
is both wrong and the first thing an engineer stops trusting. Drawn as one
logical link with its members listed, it says what is actually there.

The physical rows stay physical: a table of neighbours still shows one line per
cable, because that is what you need to go and unplug something. Only the
topology folds, and only when there is evidence to fold on - a shared bond on
our side and the same device on the other.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

OID_IF_STACK = "1.3.6.1.2.1.31.1.2.1.3"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"


def bonds_from_ifaces(ifaces: Dict[str, Any],
                      known: Optional[Any] = None) -> Dict[str, List[str]]:
    """{bond0: [eno1, eno2]}, counting only masters that really are bonds.

    *known* is the set of names the kernel described as bonds in
    /proc/net/bonding. Either signal on its own is enough: if one of them
    fails to arrive, a real aggregation is still reported rather than
    vanishing without a word.
    """
    out: Dict[str, List[str]] = {}
    for name, iface in (ifaces or {}).items():
        master = getattr(iface, "master", "") or ""
        if not master or master == name:
            continue
        # A master is not automatically a bond. A bridge has members too, and
        # libvirt leaves one on every host it touches - calling that a link
        # aggregation is a claim the rest of the tool then acts on.
        owner = (ifaces or {}).get(master)
        kind = getattr(owner, "kind", "") if owner is not None else ""
        if kind not in ("bond", "team") and master not in (known or ()):
            continue
        out.setdefault(master, []).append(name)
    for members in out.values():
        members.sort()
    return out


def aggregators(switch, port_names: List[str],
                log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Which port-channel each named switch port belongs to, via ifStackTable.

    ifStackTable is plain IF-MIB and says which interface sits on top of which;
    for a LAG member the thing above it is the aggregator. That is one standard
    walk rather than a vendor MIB per platform, and a device that does not
    publish it simply yields nothing, which folds the bundle under its bond
    name alone.
    """
    from .snmp import SnmpError, as_text

    say = log or (lambda _m: None)
    wanted = {n.strip() for n in port_names if n and n.strip()}
    if not wanted:
        return {}
    try:
        labels = {index: as_text(value) for index, value in
                  ((int(oid.rsplit(".", 1)[-1]), value)
                   for oid, value in switch.client.walk(OID_IF_NAME))}
    except SnmpError as exc:
        say("  lag: %s did not answer SNMP (%s)" % (switch.host, exc))
        return {}
    index_of = {text: index for index, text in labels.items() if text in wanted}
    if not index_of:
        return {}

    above: Dict[int, int] = {}
    try:
        for oid, value in switch.client.walk(OID_IF_STACK):
            parts = oid.rsplit(".", 2)
            if len(parts) < 3:
                continue
            try:
                higher, lower = int(parts[-2]), int(parts[-1])
            except ValueError:
                continue
            if higher and lower and int(value or 0) == 1:
                above[lower] = higher
    except SnmpError as exc:
        say("  lag: no ifStackTable on %s (%s)" % (switch.host, exc))
        return {}

    out: Dict[str, str] = {}
    for name, index in index_of.items():
        parent = above.get(index)
        if parent and labels.get(parent) and labels[parent] != name:
            out[name] = labels[parent]
    return out


def _members_of(node, iface: str) -> str:
    """The bond an interface belongs to, according to what the node recorded."""
    for bond, members in ((node.extra or {}).get("bonds") or {}).items():
        if iface in members:
            return bond
    return ""


def fold(topology, log: Optional[Callable[[str], None]] = None) -> int:
    """Replace each set of bundle members with the one logical link they form.

    Runs over the finished graph rather than during the crawl, so it works just
    as well on a topology loaded back from JSON. Returns how many bundles were
    folded.
    """
    say = log or (lambda _m: None)
    groups: Dict[Tuple[str, str, str], List[Any]] = {}
    for edge in list(topology.edges.values()):
        a_node = topology.nodes.get(edge.a)
        b_node = topology.nodes.get(edge.b)
        if a_node is None or b_node is None:
            continue
        a_bond = _members_of(a_node, edge.a_iface)
        b_bond = _members_of(b_node, edge.b_iface)
        if a_bond:
            groups.setdefault((edge.a, a_bond, edge.b), []).append(edge)
        elif b_bond:
            groups.setdefault((edge.b, b_bond, edge.a), []).append(edge)

    folded = 0
    for (owner, bond, other), members in groups.items():
        if len(members) < 2:
            continue
        remote_ports = sorted({e.b_iface if e.a == owner else e.a_iface
                               for e in members})
        local_ports = sorted({e.a_iface if e.a == owner else e.b_iface
                              for e in members})
        far = sorted({(e.attrs.get("lacp") or {}).get("b", "") for e in members})
        far_name = next((f for f in far if f), "")
        for edge in members:
            del topology.edges[edge.id]
        bundle = topology.add_edge(
            owner, bond, other, far_name or "LAG",
            proto=members[0].protos[0] if members[0].protos else "",
            source="lag", vlan=members[0].vlan, speed=members[0].speed)
        for edge in members:
            bundle.protos = sorted(set(bundle.protos) | set(edge.protos))
            bundle.sources = sorted(set(bundle.sources) | set(edge.sources))
            bundle.reported_by = sorted(set(bundle.reported_by) | set(edge.reported_by))
            for layer, fields in edge.attrs.items():
                if layer != "lacp":
                    bundle.note(layer, **fields)
        bundle.note("lacp", a=bond, b=far_name or "unknown",
                    members=["%s - %s" % pair
                             for pair in zip(local_ports, remote_ports)]
                    if len(local_ports) == len(remote_ports)
                    else local_ports + remote_ports,
                    local_ports=local_ports, remote_ports=remote_ports,
                    verdict="match", confidence="high",
                    evidence=["%s has members %s" % (bond, ", ".join(local_ports))])
        folded += 1
        say("  lag: %s folds %d links into %s" % (bond, len(members),
                                                  far_name or "one bundle"))
    return folded


NO_PARTNER = ("00:00:00:00:00:00", "")


def connect_partners(topology, views,
                     log: Optional[Callable[[str], None]] = None) -> int:
    """The switch at the far end of a bond, which LACP names outright.

    A bond that has negotiated holds its partner's system MAC - the switch's
    own hardware address, read from the host, with no LLDP anywhere in it.
    The tool used this only to decide whether LACP had settled, and threw the
    identity away; on a host that hears no LLDP at all it was the one piece
    of evidence about where the uplink goes.
    """
    say = log or (lambda _m: None)
    from .ptp import normalise_mac

    owners: Dict[str, str] = {}
    for node in topology.nodes.values():
        chassis = normalise_mac(getattr(node, "chassis_id", "") or "")
        if chassis:
            owners.setdefault(chassis, node.id)

    added = 0
    for node_id, view in (views or []):
        if view is None:
            continue
        detail = (getattr(view, "extra", {}) or {}).get("bond_detail") or {}
        for bond, info in sorted(detail.items()):
            partner = normalise_mac(str(info.get("ad_partner_mac") or ""))
            if partner in NO_PARTNER:
                continue
            far = owners.get(partner)
            if far is None:
                born = topology.add_node(chassis_id=partner, kind="switch",
                                         state="unscanned", source="lacp")
                owners[partner] = far = born.id
            if far == node_id:
                continue
            before = len(topology.edges)
            edge = topology.add_edge(a=node_id, a_iface=bond, b=far, b_iface="",
                                     proto="LACP", source="lacp",
                                     reported_by=node_id)
            edge.note("neighbour", evidence="lacp",
                      detail="the bond has negotiated with this system MAC, "
                             "so the far end is this switch")
            if len(topology.edges) > before:
                added += 1
    if added:
        say("  LACP: %d bond(s) name the switch they negotiated with" % added)
    return added

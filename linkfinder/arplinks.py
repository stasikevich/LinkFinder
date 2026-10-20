"""Who is on the other end of a cable that has addresses on it.

neighbor6 answers the dark cable: carrier up, no address, nothing heard.
This answers its commoner twin - a cable with addresses at both ends and no
LLDP anywhere on it. Measured on a live pair of lab hosts: each held the
other's hardware address in its IPv4 neighbour cache, on the right port, and
the map drew nothing at all.

The rule that makes it safe is the count. One foreign address on a port is a
point-to-point cable and the answer is forced; twenty is a management segment,
where the cache proves only that a switch is in the way. Above the ceiling the
cache is dropped, never guessed at.

A locally administered address is not an identity - it is made up at boot, and
two unrelated machines can carry the same one. So it is never used to name a
new device; when the cache also holds the address it answers on, that is what
the device is keyed by, and the made-up MAC stays as evidence.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from typing import Any, Callable, Dict, List, Optional

# Four is deliberate and measured: a direct cable showed one or two entries
# (the far port, sometimes a bridge or a guest behind it), a management
# segment showed twenty to twenty-six. Nothing real sat in between.
MANY = 4

DEAD_STATES = ("FAILED", "INCOMPLETE")
NO_MAC = ("", "00:00:00:00:00:00")


def _alive(record: Dict[str, Any]) -> bool:
    state = str(record.get("state", "")).upper()
    mac = str(record.get("mac", "")).lower()
    return state not in DEAD_STATES and mac not in NO_MAC


def per_port(cache: Dict[str, Any], own: Optional[set] = None
             ) -> Dict[str, List[Dict[str, str]]]:
    """The neighbour cache regrouped as {port: [{mac, address}, ...]}.

    Entries that prove nothing are dropped here rather than downstream: a
    failed lookup, the all-zero address, and the host's own interfaces - a
    port that merely sees its own other leg is not a cable to anywhere.
    """
    mine = {str(m).lower() for m in (own or set())}
    found: Dict[str, List[Dict[str, str]]] = {}
    for address, record in (cache or {}).items():
        if not isinstance(record, dict) or not _alive(record):
            continue
        mac = str(record.get("mac", "")).lower()
        if mac in mine:
            continue
        port = str(record.get("dev", ""))
        if not port:
            continue
        found.setdefault(port, []).append({"mac": mac, "address": str(address)})
    return found


def _ipv4(address: str) -> bool:
    """A v4 address names a device; a link-local v6 one only repeats its MAC."""
    return "." in address and ":" not in address


def peers_worth_drawing(cache: Dict[str, Any], own: Optional[set] = None
                        ) -> Dict[str, Dict[str, Any]]:
    """Ports whose cache is small enough that it says something.

    Shape matches what the IPv6 path hands to the shared drawing step, so
    one piece of code decides what a cable is.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for port, rows in per_port(cache, own).items():
        distinct = {row["mac"] for row in rows}
        if not distinct or len(distinct) > MANY:
            continue
        best: Dict[str, Dict[str, str]] = {}
        for row in rows:
            held = best.get(row["mac"])
            # Prefer the v4 address: it is the one a person can reach.
            if held is None or (_ipv4(row["address"])
                                and not _ipv4(held["address"])):
                best[row["mac"]] = row
        out[port] = {"peers": list(best.values())}
    return out


def addresses_for(found: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{mac: {address}} so a device nobody scanned still gets a reachable name."""
    hints: Dict[str, Dict[str, str]] = {}
    for port in found.values():
        for peer in port.get("peers") or []:
            if _ipv4(peer.get("address", "")):
                hints.setdefault(peer["mac"], {"address": peer["address"]})
    return hints


def already_answered(topology, node_id: str, extra: Dict[str, Any]) -> set:
    """Ports of this device that a protocol has already settled.

    The cache is the last rung of the ladder, not a parallel one. A bonded
    pair whose legs LLDP had already named came back a second time as
    bond-to-leg guesses, because a bond and its legs carry one address and
    the cache cannot tell them apart. If a protocol answered for a port -
    or for any leg of it - there is nothing left here to work out.
    """
    taken = set()
    for edge in topology.edges.values():
        for side in ("a", "b"):
            if getattr(edge, side) == node_id:
                taken.add(str(getattr(edge, side + "_iface") or ""))
    for bond, legs in (extra.get("bonds") or {}).items():
        if taken & set(legs or []):
            taken.add(bond)
        if bond in taken:
            taken |= set(legs or [])
    return taken


def connect(topology, views, log: Optional[Callable[[str], None]] = None) -> int:
    """Draw the cables the IPv4 neighbour cache can prove."""
    say = log or (lambda _m: None)
    from . import oui
    from .ptp import normalise_mac
    from .neighbor6 import draw

    owners: Dict[str, str] = {}
    cards: Dict[str, Dict[str, Any]] = {}
    ports: Dict[str, Dict[str, Any]] = {}
    hints: Dict[str, Dict[str, str]] = {}
    for node_id, view in (views or []):
        if view is None:
            continue
        extra = getattr(view, "extra", {}) or {}
        cards[node_id] = extra
        for where in ("macs", "perm_macs"):
            for name, mac in (extra.get(where) or {}).items():
                owners.setdefault(normalise_mac(mac), "%s|%s" % (node_id, name))
        mine = set((extra.get("macs") or {}).values())
        mine |= set((extra.get("perm_macs") or {}).values())
        found = peers_worth_drawing(extra.get("neighbours") or {}, own=mine)
        settled = already_answered(topology, node_id, extra)
        found = {port: rows for port, rows in found.items()
                 if port not in settled}
        if found:
            ports[node_id] = found
            hints.update(addresses_for(found))

    for node in topology.nodes.values():
        chassis = normalise_mac(getattr(node, "chassis_id", "") or "")
        if chassis:
            owners.setdefault(chassis, "%s|" % node.id)
        for address in getattr(node, "mgmt_ips", []) or []:
            for mac, hint in hints.items():
                if hint.get("address") == address:
                    owners.setdefault(normalise_mac(mac), "%s|" % node.id)

    # A made-up address must not become a device's name. Where the cache also
    # holds the IPv4 address, the device is keyed by that instead and the MAC
    # is only how we matched it.
    #
    # Only for a port with ONE answer on it. Without that the rule ran on
    # every entry and a management gateway - which is on the segment but is
    # not on the end of anybody's cable - arrived as a device.
    alone = set()
    for found in ports.values():
        for port in found.values():
            peers = port.get("peers") or []
            if len({p.get("mac") for p in peers}) == 1 and peers:
                alone.add(peers[0].get("mac"))
    for mac, hint in hints.items():
        key = normalise_mac(mac)
        if key in owners or mac not in alone:
            continue
        if oui.mac_kind(mac) != "locally administered":
            continue
        born = topology.add_node(mgmt_ip=hint["address"], state="unscanned",
                                 source="arp", kind="unknown")
        owners[key] = "%s|" % born.id

    return draw(topology, ports, owners, cards, hints, {},
                proto="ARP", source="arp", label="neighbour cache", log=say)

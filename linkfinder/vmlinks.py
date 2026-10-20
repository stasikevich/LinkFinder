"""A virtual machine, and the host it is actually running on.

Guests came back as separate boxes floating beside everything else, with
no cable to anything. That reads as "we could not work out where this is",
when in fact both halves were already in hand: the guest knows it is a KVM
guest, and the hypervisor is carrying a tap device wearing the guest's own
hardware address.

Two shapes, both exact, neither a guess:

  macvtap  the host's device carries the guest's address unchanged
  vnet     libvirt names the host side fe:54:00 + the guest's last five
           bytes, which is a convention, not a coincidence

So the match is made on the address and nothing else. A guest with no
matching tap stays unconnected rather than being attached to the likeliest
host - "probably over there" is the answer this is meant to replace.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .ptp import normalise_mac

# The host side of a libvirt tap. The guest keeps 52:54:00 - the QEMU
# range - and the host end differs only in that first byte.
HOST_TAP = "fe:54:00"
GUEST_RANGE = "52:54:00"


def _addresses_of(node) -> Dict[str, str]:
    """Every port on this node and the address it wears.

    `macs` holds the physical ones only, which is right for folding two
    sightings of a machine together and wrong here: a tap is exactly the
    kind of port it leaves out, and a tap is the whole point.
    """
    out: Dict[str, str] = {}
    for port, held in ((node.extra or {}).get("ports") or {}).items():
        mac = str((held or {}).get("mac") or "")
        if mac:
            out[str(port)] = mac
    for port, mac in ((node.extra or {}).get("macs") or {}).items():
        out.setdefault(str(port), str(mac))
    return out


def _tail(mac: str) -> str:
    """The last five bytes, which both ends of a tap share."""
    text = normalise_mac(mac)
    return text[3:] if len(text) == 17 else ""


def is_guest(node) -> bool:
    """Whether this node said for itself that it is a virtual machine."""
    virtual = (node.extra or {}).get("virtual") or {}
    if str(virtual.get("virt") or "").strip():
        return True
    maker = str(virtual.get("sys_vendor") or "").lower()
    return maker in ("qemu", "kvm", "bochs", "vmware, inc.", "innotek gmbh")


def taps_of(node) -> Dict[str, str]:
    """Every tap-shaped port on this node, by the guest address it carries.

    A tap is not a cable to anywhere else, so these never appear as links
    of their own; they exist to be matched.
    """
    out: Dict[str, str] = {}
    for port, mac in _addresses_of(node).items():
        text = normalise_mac(mac)
        if not text:
            continue
        if text.startswith(HOST_TAP) or text.startswith(GUEST_RANGE):
            out.setdefault(_tail(text), port)
    return out


def guest_ports(node) -> Dict[str, str]:
    """The guest's own ports whose address came out of the QEMU range.

    A guest nobody could log into has no ports at all - only the address
    its neighbour heard. That is still the address a tap is carrying, and
    leaving it out meant the machine we knew least about was also the one
    left floating.
    """
    out: Dict[str, str] = {}
    for port, mac in _addresses_of(node).items():
        text = normalise_mac(mac)
        if text.startswith(GUEST_RANGE):
            out[_tail(text)] = port
    if not out:
        chassis = normalise_mac(getattr(node, "chassis_id", "") or "")
        if chassis.startswith(GUEST_RANGE):
            out[_tail(chassis)] = "?"
    return out


def _physical(node) -> set:
    """The ports this host has in hardware.

    `macs` is collected from the physical ones only, which makes it the
    thing that tells a tap from an uplink: an address learned on a port
    that is NOT in here is sitting on something virtual, and that means
    the machine is running on this host rather than out on the network
    somewhere behind it.
    """
    return {str(p) for p in ((node.extra or {}).get("macs") or {})}


def by_bridge_table(topology) -> List[Tuple[Any, str, Any, str]]:
    """Guests found by the address their host's bridge actually learned."""
    hosts = [(n, (n.extra or {}).get("fdb") or {}, _physical(n))
             for n in topology.nodes.values()
             if (n.extra or {}).get("fdb")]
    out: List[Tuple[Any, str, Any, str]] = []
    if not hosts:
        return out
    for node in topology.nodes.values():
        for port, mac in _addresses_of(node).items():
            key = normalise_mac(mac)
            if not key:
                continue
            for host, table, hardware in hosts:
                if host.id == node.id:
                    continue
                held = table.get(key)
                if not held:
                    continue
                where = str(held.get("dev") or "")
                if not where or where in hardware:
                    continue      # behind an uplink: out there, not in here
                out.append((node, port, host, where))
                break
    return out


def connect(topology, views: Optional[List[Tuple[str, Any]]] = None,
            log: Optional[Callable[[str], None]] = None) -> int:
    """Join every guest to the host running it. Returns links drawn.

    The bridge table first, because it is the host saying what it has
    actually seen on which port. The address convention second, for hosts
    whose table could not be read.
    """
    say = log or (lambda _m: None)
    drawn = 0
    joined = set()
    for node, port, host, where in by_bridge_table(topology):
        topology.add_edge(node.id, port, host.id, where,
                          proto="bridge", source="fdb", reported_by=host.id)
        joined.add((node.id, port))
        drawn += 1

    hosts: List[Tuple[Any, Dict[str, str]]] = []
    for node in topology.nodes.values():
        taps = taps_of(node)
        if taps and not is_guest(node):
            hosts.append((node, taps))
    for node in list(topology.nodes.values()):
        for tail, port in guest_ports(node).items():
            if (node.id, port) in joined:
                continue
            for host, taps in hosts:
                if host.id == node.id or tail not in taps:
                    continue
                topology.add_edge(node.id, port, host.id, taps[tail],
                                  proto="KVM", source="virtual",
                                  reported_by=node.id)
                drawn += 1
                break
    if drawn:
        say("  %d virtual machine port(s) joined to the host running them"
            % drawn)
    return drawn

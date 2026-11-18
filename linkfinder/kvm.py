"""The virtual machines a host is actually running, named by libvirt.

A KVM guest seen only in a neighbour cache is a bare MAC and, at best, one
address - often just a link-local one, which reads on the map as "something is
wrong here". But the host running it knows exactly what it is: libvirt holds the
guest's name, its interfaces and the bridge each one sits on, and can be asked
for the guest's address too. So on a host that runs `virsh`, the guests are read
straight from it and drawn as named machines (iperf-4G, not fe80::...), attached
to the host that carries them. Everything here needs root (libvirt's socket is
root-only) and is a read; a host without virsh simply yields nothing.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

Run = Callable[[str], str]

RUNNING = "virsh list --name --state-running 2>/dev/null"
IFLIST = "virsh domiflist %s 2>/dev/null"
# --source arp reads the host's own neighbour table (no guest agent needed);
# lease covers libvirt's own DHCP. Both are tried, arp last so a lease wins.
IFADDR = "virsh domifaddr %s --source %s 2>/dev/null"


def _ipv4(address: str) -> bool:
    return "." in address and ":" not in address


def parse_iflist(text: str) -> List[Dict[str, str]]:
    """Rows of `virsh domiflist`: interface, type, source, model, mac."""
    out: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.split()
        # the MAC in the last column is the marker of a real data row
        if len(parts) >= 5 and re.match(r"^[0-9a-fA-F:]{17}$", parts[-1]):
            out.append({"iface": parts[0], "type": parts[1],
                        "source": parts[2], "mac": parts[-1].lower()})
    return out


def parse_ifaddr(text: str) -> Dict[str, str]:
    """{mac: address} from `virsh domifaddr`, the routable v4 preferred."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and re.match(r"^[0-9a-fA-F:]{17}$", parts[1]):
            mac = parts[1].lower()
            address = parts[3].split("/")[0]
            if address and (mac not in out or _ipv4(address)):
                out[mac] = address
    return out


def read_guests(run: Run) -> List[Dict[str, Any]]:
    """Each running guest with its interfaces and whatever address libvirt knows.

    One record per domain: {name, interfaces: [{iface, type, source, mac,
    address?}]}. An interface with no address libvirt could resolve is still
    returned - the name alone is worth drawing.
    """
    names = [n.strip() for n in (run(RUNNING) or "").splitlines() if n.strip()]
    guests: List[Dict[str, Any]] = []
    for name in names:
        ifaces = parse_iflist(run(IFLIST % name))
        if not ifaces:
            continue
        addrs: Dict[str, str] = {}
        for source in ("lease", "arp"):
            for mac, address in parse_ifaddr(run(IFADDR % (name, source))).items():
                if mac not in addrs or _ipv4(address):
                    addrs[mac] = address
        for iface in ifaces:
            if iface["mac"] in addrs:
                iface["address"] = addrs[iface["mac"]]
        guests.append({"name": name, "interfaces": ifaces})
    return guests


def collect(run: Run, log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """The running guests, or [] when virsh is absent or the host runs none."""
    say = log or (lambda _m: None)
    if not (run("command -v virsh 2>/dev/null") or "").strip():
        return []
    guests = read_guests(run)
    if guests:
        say("  KVM: %d running guest(s): %s"
            % (len(guests), ", ".join(g["name"] for g in guests)))
    return guests


def connect(topology, views: Optional[List] = None,
            log: Optional[Callable[[str], None]] = None) -> int:
    """Draw each host's libvirt guests as named machines. Returns guests named.

    The node is keyed by the guest's own hardware address, so vmlinks can then
    attach it to the host over the matching tap. A VM is given its IPv4 and only
    its IPv4 - a bare link-local address on a machine reads as a fault, and the
    libvirt name is the identity that matters.
    """
    say = log or (lambda _m: None)
    # Every hardware address an already-drawn node owns, so a guest that was
    # also scanned directly (as its own target) is not drawn a second time.
    owned = set()
    for n in topology.nodes.values():
        chassis = str(getattr(n, "chassis_id", "") or "").lower()
        if chassis:
            owned.add(chassis)
        for src in ("macs", "perm_macs"):
            for m in ((getattr(n, "extra", {}) or {}).get(src) or {}).values():
                if m:
                    owned.add(str(m).lower())
    made = 0
    for _node_id, view in (views or []):
        extra = getattr(view, "extra", {}) or {}
        for guest in extra.get("kvm_domains") or []:
            name = str(guest.get("name") or "")
            ifaces = guest.get("interfaces") or []
            if not ifaces:
                continue
            if any(str(i.get("mac") or "").lower() in owned for i in ifaces):
                continue      # already on the map under its own scan
            # the bridge interface is the guest's routable leg (and carries its
            # v4); the macvtap/direct one is management. Key on the bridge.
            ordered = sorted(ifaces,
                             key=lambda i: 0 if i.get("type") == "bridge" else 1)
            mac, v4 = str(ordered[0].get("mac") or ""), ""
            for iface in ordered:
                address = str(iface.get("address") or "")
                if _ipv4(address):
                    v4, mac = address, str(iface.get("mac") or mac)
                    break
            if not mac:
                continue
            topology.add_node(chassis_id=mac, kind="vm", name=name,
                              state="unscanned", source="libvirt", mgmt_ip=v4)
            for i in ifaces:
                if i.get("mac"):
                    owned.add(str(i["mac"]).lower())
            made += 1
    if made:
        say("  %d KVM guest(s) named from libvirt" % made)
    return made

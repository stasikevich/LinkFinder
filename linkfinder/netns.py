"""Ports that are on the host but not in the view we were given.

A container, a CNI pod or an SR-IOV guest can hold a real port inside its own
network namespace. From the outside the port is invisible, and - worse - a
physical port of the same card can sit in the root namespace looking dead:
carrier up, no address, no frame, no answer. Reporting that as an unplugged
cable is confidently wrong, which is the one thing a troubleshooting tool
must never be.

Two traps decide how this has to be written, both measured rather than
assumed on a packaged base station:

- **/sys/class/net does not follow `nsenter -n`.** sysfs is filtered by the
  namespace the mount belongs to, so reading it inside a namespace silently
  returns the root's ports. Everything here goes through netlink (`ip`),
  which is namespace-correct. Entering the mount namespace too is not an
  option: a container's filesystem frequently has no shell.

- **The same interface name means different ports in different
  namespaces.** On the host this was built against, the root and a pod both
  had `ens3f0np0`: the root's was the physical function at PCI 0000:c1:00.0,
  the pod's was its virtual function at 0000:c1:01.0 - a different MAC and a
  different ifindex. So the namespace is part of a port's identity, never a
  decoration on its name.

Because of the second trap a dark physical port can be explained rather than
merely reported: sysfs lists the virtual functions each physical function
owns, and `ip -d` names the parent device of every port inside a namespace.
Joining those two by PCI address is proof; a name match would prove nothing,
since the names deliberately collide.

Everything here is a read. `nsenter` needs root; without it the honest answer
is that ports may be hidden, not that there are none.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import re
from typing import Any, Callable, Dict, List, Optional

from .sshx import Iface

MAX_SPACES = 96

VIRTUAL_KINDS = ("veth", "bridge", "bond", "dummy", "vlan", "vxlan", "macvlan",
                 "macvtap", "ipvlan", "tun", "gre", "sit", "team", "ppp",
                 "bareudp", "geneve", "wireguard", "openvswitch", "vrf",
                 "ip6tnl", "nlmon", "ifb", "netdevsim")

LIST_SCRIPT = r"""
[ "$(id -u 2>/dev/null)" = "0" ] || { echo "DENIED|not root"; exit 0; }
command -v nsenter >/dev/null 2>&1 || { echo "DENIED|no nsenter"; exit 0; }
command -v ip >/dev/null 2>&1 || { echo "DENIED|no ip"; exit 0; }
self=$(readlink /proc/self/ns/net 2>/dev/null | tr -cd '0-9')
echo "SELF|$self"
seen=" "
count=0
for p in /proc/[0-9]*; do
  ino=$(readlink "$p/ns/net" 2>/dev/null | tr -cd '0-9')
  [ -n "$ino" ] || continue
  case "$seen" in *" $ino "*) continue ;; esac
  seen="$seen$ino "
  [ "$ino" = "$self" ] && continue
  count=$((count+1))
  [ "$count" -gt %(cap)d ] && { echo "CAPPED|$count"; break; }
  pid=$(basename "$p")
  h=$(nsenter -t "$pid" -u hostname 2>/dev/null)
  echo "NS|$ino|$pid|$h"
  nsenter -t "$pid" -n ip -d -o link show 2>/dev/null | sed "s|^|LINK\|$ino\||"
done
"""

DETAIL_SCRIPT = r"""
for pair in %(pairs)s; do
  ino=$(echo "$pair" | cut -d: -f1)
  pid=$(echo "$pair" | cut -d: -f2)
  nsenter -t "$pid" -n ip -o addr show 2>/dev/null | sed "s|^|ADDR\|$ino\||"
  nsenter -t "$pid" -n ip neigh show 2>/dev/null | sed "s|^|NB\|$ino\||"
  nsenter -t "$pid" -n ip -6 neigh show 2>/dev/null | sed "s|^|NB6\|$ino\||"
done
"""

VF_SCRIPT = r"""
for d in /sys/class/net/*; do
  n=$(basename "$d")
  [ -e "$d/device" ] || continue
  link=$(readlink "$d/device" 2>/dev/null)
  [ -n "$link" ] && echo "PF|$n|$(basename "$link")"
  for v in "$d"/device/virtfn*; do
    [ -e "$v" ] || continue
    t=$(readlink "$v" 2>/dev/null)
    [ -n "$t" ] && echo "VF|$n|$(basename "$t")"
  done
done
"""

LINK_HEAD = re.compile(r"^(\d+):\s+([^:@]+)(?:@\S+)?:\s+<([^>]*)>\s*(.*)$")
ADDR_LINE = re.compile(r"^\d+:\s+(\S+)\s+(inet6?)\s+(\S+)")
NEIGH_LINE = re.compile(r"^(\S+)\s+dev\s+(\S+)\s+lladdr\s+([0-9a-fA-F:]{17})\s+(.+)$")

# The tail of a neighbour line carries flags as well as the state - "router"
# and "proxy" sit in front of it - so the state is picked out by name.
NEIGH_STATES = ("PERMANENT", "NOARP", "REACHABLE", "STALE", "NONE",
                "INCOMPLETE", "DELAY", "PROBE", "FAILED")

EMPTY_MAC = "00:00:00:00:00:00"


class Space:
    """One network namespace, and what it turned out to be holding."""

    def __init__(self, inode: str, pid: str, name: str):
        self.inode = inode
        self.pid = pid
        self.name = name
        self.ifaces: Dict[str, Iface] = {}
        self.neighbours: List[Dict[str, str]] = []

    @property
    def label(self) -> str:
        """What to call it on screen. A pod's own hostname is its pod name,
        which is the one string an operator will recognise."""
        return self.name or ("namespace %s" % self.inode)

    def hardware(self) -> List[Iface]:
        """The ports in here that lead out of the machine."""
        return [i for i in self.ifaces.values() if i.physical]

    def to_dict(self) -> Dict[str, Any]:
        return {"inode": self.inode, "name": self.name, "pid": self.pid,
                "ifaces": {n: {"mac": i.mac, "pci": i.pci,
                               "operstate": i.operstate, "carrier": i.carrier,
                               "addrs": list(i.addrs), "mtu": i.mtu,
                               "kind": i.kind}
                           for n, i in sorted(self.ifaces.items())},
                "neighbours": list(self.neighbours)}


def parse_ip_link(line: str) -> Optional[Iface]:
    """One `ip -d -o link show` record.

    Netlink rather than sysfs, because sysfs lies inside a namespace. With
    -o the whole record arrives as one line and the original newlines come
    through as a backslash, so the detail section is simply the tail.

    A port counts as hardware when it has a PCI parent. Where iproute2 is too
    old to report one, fall back on what the port is not: a veth names its
    peer as `name@ifN`, and every other virtual type names itself.
    """
    hit = LINK_HEAD.match(line.strip())
    if not hit:
        return None
    name, flags, rest = hit.group(2), hit.group(3), hit.group(4)
    if name == "lo":
        return None
    words = rest.replace("\\", " ").split()
    found: Dict[str, str] = {}
    for at, word in enumerate(words):
        if word in ("mtu", "state", "link/ether", "parentdev") \
                and at + 1 < len(words):
            found.setdefault(word, words[at + 1])

    kind = ""
    for word in words:
        if word in VIRTUAL_KINDS:
            kind = word
            break
    paired = "@if" in line.split(">")[0]
    pci = found.get("parentdev", "")

    return Iface(
        name=name,
        mac=found.get("link/ether", ""),
        operstate=found.get("state", "").lower(),
        carrier="0" if "NO-CARRIER" in flags else ("1" if "LOWER_UP" in flags
                                                   else ""),
        mtu=found.get("mtu", ""),
        kind="" if kind == "veth" else kind,
        physical=bool(pci) or not (paired or kind),
        pci=pci)


def parse(text: str) -> Dict[str, Space]:
    """The namespaces, their names and their ports, from the bulk read."""
    spaces: Dict[str, Space] = {}
    for raw in (text or "").splitlines():
        parts = raw.split("|", 2)
        if len(parts) < 2:
            continue
        if parts[0] == "NS":
            bits = raw.split("|")
            if len(bits) >= 3:
                spaces[bits[1]] = Space(bits[1], bits[2],
                                        bits[3].strip() if len(bits) > 3 else "")
        elif parts[0] == "LINK" and len(parts) >= 3:
            space = spaces.get(parts[1])
            iface = parse_ip_link(parts[2]) if space else None
            if space is not None and iface is not None:
                space.ifaces[iface.name] = iface
    return spaces


def add_detail(spaces: Dict[str, Space], text: str) -> None:
    """Addresses and neighbour caches, folded into the namespaces.

    A neighbour entry here is worth more than anywhere else: inside the
    namespace the far ends are already REACHABLE, so what the root namespace
    could only get by probing is sitting in a kernel table.
    """
    for raw in (text or "").splitlines():
        parts = raw.split("|", 2)
        if len(parts) < 3:
            continue
        space = spaces.get(parts[1])
        if space is None:
            continue
        if parts[0] == "ADDR":
            hit = ADDR_LINE.match(parts[2].strip())
            iface = space.ifaces.get(hit.group(1)) if hit else None
            if hit and iface is not None and hit.group(3) not in iface.addrs:
                iface.addrs.append(hit.group(3))
        elif parts[0] in ("NB", "NB6"):
            hit = NEIGH_LINE.match(parts[2].strip())
            if not hit or hit.group(3).lower() == EMPTY_MAC:
                continue
            words = [w.upper() for w in hit.group(4).split()]
            state = ([w for w in words if w in NEIGH_STATES]
                     or [words[-1] if words else ""])[0]
            if state in ("FAILED", "INCOMPLETE"):
                continue
            entry = {"ip": hit.group(1), "iface": hit.group(2),
                     "mac": hit.group(3).lower(), "state": state}
            if entry not in space.neighbours:
                space.neighbours.append(entry)


def parse_vfs(text: str) -> Dict[str, str]:
    """Each virtual function's PCI address mapped to the port that owns it."""
    owner: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        parts = raw.split("|")
        if len(parts) >= 3 and parts[0] == "VF" and parts[2]:
            owner[parts[2]] = parts[1]
    return owner


def parse_pfs(text: str) -> Dict[str, str]:
    """Each root-namespace port mapped to the PCI device behind it."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        parts = raw.split("|")
        if len(parts) >= 3 and parts[0] == "PF" and parts[2]:
            out[parts[1]] = parts[2]
    return out


def discover(run: Callable[[str], str],
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Every namespace on this host that is holding a port of its own.

    Returns the interesting namespaces, the virtual-function map, and - when
    we could not look - why, so the caller can say "may be hidden" rather
    than pretend the host had nothing. Addresses and neighbour caches are
    read in a second pass over only the namespaces that earned it: on a busy
    worker almost every namespace is a pod sandbox with a single veth.
    """
    say = log or (lambda _m: None)
    blank: Dict[str, Any] = {"spaces": {}, "vfs": {}, "refused": "",
                             "seen": 0, "capped": False}
    try:
        text = run("sh -c %s" % _quote(LIST_SCRIPT % {"cap": MAX_SPACES}))
    except Exception as exc:
        blank["refused"] = "%s: %s" % (type(exc).__name__, exc)
        return blank
    for line in (text or "").splitlines():
        if line.startswith("DENIED|"):
            blank["refused"] = line.split("|", 1)[1].strip()
            say("  namespaces: not read (%s) - ports inside containers may be "
                "hidden" % blank["refused"])
            return blank

    spaces = parse(text)
    blank["seen"] = len(spaces)
    blank["capped"] = "CAPPED|" in (text or "")
    keep = {ino: sp for ino, sp in spaces.items() if sp.hardware()}
    if not keep:
        if spaces:
            say("  namespaces: %d found, none holding a port of its own"
                % len(spaces))
        return blank

    pairs = " ".join("%s:%s" % (ino, sp.pid) for ino, sp in sorted(keep.items()))
    try:
        add_detail(keep, run("sh -c %s" % _quote(DETAIL_SCRIPT
                                                 % {"pairs": pairs})))
    except Exception:
        pass
    try:
        cards = run("sh -c %s" % _quote(VF_SCRIPT))
    except Exception:
        cards = ""
    vfs, pfs = parse_vfs(cards), parse_pfs(cards)

    say("  namespaces: %d of %d hold a port of their own - %s"
        % (len(keep), len(spaces),
           ", ".join("%s (%s)" % (sp.label,
                                  ", ".join(i.name for i in sp.hardware()))
                     for sp in sorted(keep.values(), key=lambda s: s.label))))
    return {"spaces": keep, "vfs": vfs, "pfs": pfs, "refused": "",
            "seen": len(spaces), "capped": blank["capped"]}


IN = " in "


def one_port(extra: Dict[str, Any], left: str, right: str) -> str:
    """Are these two port names the same piece of hardware?

    A physical function and one of its own virtual functions are not two
    ports with a cable between them - they are one port, and the card
    switches between them internally. Each will happily answer the other's
    neighbour query, which looks exactly like a cable looped back into the
    machine and is not.

    The join is by PCI address. Matching on the interface name would be
    worthless here: the pod deliberately gives its virtual function the same
    name as the physical one.

    Returns the physical port's name when they are one, otherwise "".
    """
    owner = (extra or {}).get("netns_vfs") or {}
    spaces = (extra or {}).get("netns") or {}
    if not owner:
        return ""

    def behind(port: str) -> str:
        name, _sep, where = port.partition(IN)
        if not where:
            return ""
        for space in spaces.values():
            if str(space.get("name") or "") != where:
                continue
            return str(((space.get("ifaces") or {}).get(name)
                        or {}).get("pci") or "")
        return ""

    for inner, outer in ((left, right), (right, left)):
        if IN in outer:
            continue
        pci = behind(inner)
        if pci and owner.get(pci) == outer:
            return outer
    return ""


def explain_dark(iface_name: str, found: Dict[str, Any]) -> str:
    """Why a port that looks dead is not, when a namespace holds its traffic.

    The join is by PCI address, which is proof. The interface names collide
    on purpose, so matching on them would say nothing at all.
    """
    owned = {pci for pci, pf in (found.get("vfs") or {}).items()
             if pf == iface_name}
    if not owned:
        return ""
    for space in (found.get("spaces") or {}).values():
        for inner in space.hardware():
            if inner.pci and inner.pci in owned:
                live = [n for n in space.neighbours if n["iface"] == inner.name]
                tail = ""
                if live:
                    tail = (" - %s is live in there at %s"
                            % (live[0]["mac"], live[0]["ip"]))
                return ("this port carries no traffic itself: a virtual "
                        "function of it (%s) is inside %s as %s%s"
                        % (inner.pci, space.label, inner.name, tail))
    return ""


def _quote(text: str) -> str:
    """Hand a script to a remote `sh -c` without the local shell touching it."""
    return "'" + text.replace("'", "'\"'\"'") + "'"

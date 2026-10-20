"""Turn a topology JSON into one self-contained HTML page.

Takes data, never a device: discovery -> topology JSON -> renderer -> HTML, so
the same JSON can feed another renderer or an external tool. The page carries
both the data and cytoscape.min.js inside it, because a file:// page may not
fetch a sibling file and must not depend on the network.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from .branding import signature

KIND_STYLE = {
    "switch": ["#1f77b4", "round-rectangle"],
    "router": ["#9467bd", "diamond"],
    "host": ["#2ca02c", "ellipse"],
    "bmc": ["#ff7f0e", "hexagon"],
    "ap": ["#17becf", "triangle"],
    "radio": ["#e377c2", "hexagon"],
    "phone": ["#8c564b", "vee"],
    "timing": ["#bcbd22", "round-diamond"],
    "serial": ["#8c8c8c", "round-tag"],
    "peripheral": ["#a0a0a0", "round-rectangle"],
    "pdu": ["#d62728", "round-rectangle"],
    "unknown": ["#7f7f7f", "ellipse"],
}

KIND_SIZE = {
    "switch": [62, 30],
    "router": [56, 32],
    "host": [48, 43],
    "bmc": [42, 44],
    "ap": [46, 43],
    "radio": [42, 46],
    "phone": [48, 40],
    "timing": [46, 46],
    "serial": [40, 26],
    "peripheral": [40, 30],
    "pdu": [34, 48],
    "unknown": [44, 40],
}

# What to call a kind in a sentence, where the bare key reads wrong.
KIND_WORD = {"timing": "timing source", "bmc": "management controller",
             "ap": "wireless access point", "host": "server",
             "serial": "console cable", "peripheral": "usb device",
             "pdu": "power / UPS"}

# The map marks a device by what it IS - a server, a switch, a phone, a UPS -
# with a clear original glyph, and names the maker in plain text on the panel.
# It does not draw company logos: those are trademarks, and a hand-drawn
# imitation only came out muddy. The one maker mapped here, APC, is mapped to
# a device type: an APC box seen only as a neighbour comes back as "AMERICAN
# POWER CONVERSION" with no kind, and it is a power unit, so it takes the UPS
# glyph - a device shape, not a brand mark.
VENDOR_GLYPH = {
    "american power conversion": "pdu", "apc": "pdu", "schneider": "pdu",
}

# Which kind's colour a device glyph borrows, so the tint stays consistent
# with what the thing is. Only the bench mini-PC, which is a kind of host.
GLYPH_COLOUR = {"nuc": "host"}


def _vendor_glyph(maker: str) -> str:
    low = (" %s " % str(maker or "")).lower()
    for mark, glyph in VENDOR_GLYPH.items():
        if mark in low:
            return glyph
    return ""


def glyph_for(node: Dict[str, Any], kind: str, maker: str) -> str:
    """The drawing to use: the device's own shape, by what it is.

    A bench mini-PC takes the NUC shape rather than the plain server, and an
    APC power unit seen only by its maker takes the UPS shape - both are
    device types, not brands. Everything else is drawn by its kind. The maker
    is named in text on the panel, not drawn as a logo.
    """
    if kind == "host" and _is_nuc(node):
        return "nuc"
    return _vendor_glyph(maker) or kind

ICON_TINT = 0.13

VERDICT_COLOUR = {
    "mismatch": "#c5221f",
    "warn": "#bf6a02",
    "match": "#1a7f37",
    "unknown": "#9aa4af",
}

PTP_COLOUR = {
    "boundary": "#0f766e",
    "grandmaster": "#6f42c1",
    "MASTER": "#1a7f37",
    "SLAVE": "#1f6feb",
    "LISTENING": "#bf6a02",
    "PASSIVE": "#bf6a02",
    "UNCALIBRATED": "#bf6a02",
    "FAULTY": "#c5221f",
    "": "#9aa4af",
}

STATE_BORDER = {
    "scanned": ["#1a1a1a", 2],
    "unreachable": ["#d62728", 3],
    "out-of-scope": ["#a0aec0", 3],
    "limit-reached": ["#a0aec0", 3],
    "unscanned": ["#999999", 1],
}

EDGE_STYLE = {
    "confirmed": ["#2b6cb0", "solid", 3],
    "reported": ["#4a5568", "dashed", 2],
    "inferred": ["#a0aec0", "dotted", 2],
}

# A handset hangs off a USB port, not off a switch port, and which USB port it
# is decides what the link can carry. Colour says it at a glance; the version
# itself is on the link when you click it.
USB_STYLE = {
    "USB 1": "#7f7f7f",
    "USB 2": "#111111",
    "USB 3": "#0057d9",
    "USB": "#8c564b",
}


def assets_dir() -> str:
    """PyInstaller unpacks bundled data beside the module under _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        packed = os.path.join(base, "linkfinder", "assets")
        if os.path.isdir(packed):
            return packed
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _asset(name: str) -> str:
    path = os.path.join(assets_dir(), name)
    if not os.path.exists(path):
        raise RuntimeError("missing asset %s in %s - the HTML has to carry it so the "
                           "page opens offline" % (name, assets_dir()))
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tint(colour: str, keep: float = ICON_TINT) -> str:
    """Wash a kind colour out towards white so an icon stays readable on it."""
    try:
        parts = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return "#ffffff"
    return "#%02x%02x%02x" % tuple(
        int(round(255 - (255 - c) * keep)) for c in parts)


_ICON_CACHE: Dict[str, str] = {}


# The bench mini-PCs, by what their own firmware calls them. Intel NUC, and
# the small-form Dell and HP boxes that do the same job on a bench - a NUC in
# everything but the brand name. Matched on the DMI product and vendor.
NUC_SIGNS = ("nuc", "pro micro", "optiplex micro", "elitedesk mini",
             "prodesk mini", "mini pc", "nuc kit")


def _is_nuc(node: Dict[str, Any]) -> bool:
    """Whether this host is a bench mini-PC rather than a rack server."""
    if str(node.get("kind") or "") != "host":
        return False
    hw = (node.get("extra") or {}).get("hardware") or {}
    text = ("%s %s" % (hw.get("product_name", ""),
                       hw.get("sys_vendor", ""))).lower()
    return any(sign in text for sign in NUC_SIGNS)


def icon_uris() -> Dict[str, str]:
    """Each device drawing as a base64 data URI, coloured for its kind.

    Base64 rather than inline SVG text keeps the markup out of the JSON and
    keeps the xmlns URL out of the page, so the offline checks stay honest.
    """
    if not _ICON_CACHE:
        # Every kind, plus the vendor and device glyphs that are not kinds of
        # their own (a Dell server is still the host kind). Each borrows the
        # colour of what it is, so the tint stays consistent.
        glyphs = {kind: colour for kind, (colour, _s) in KIND_STYLE.items()}
        for glyph, like in GLYPH_COLOUR.items():
            glyphs[glyph] = KIND_STYLE[like][0]
        for kind, colour in glyphs.items():
            svg = _asset(os.path.join("icons", kind + ".svg"))
            svg = svg.replace("__ACCENT__", colour)
            packed = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            _ICON_CACHE[kind] = "data:image/svg+xml;base64," + packed
        face = _asset(os.path.join("icons", "clock.svg"))
        for role, colour in PTP_COLOUR.items():
            svg = face.replace("__ACCENT__", colour)
            packed = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            _ICON_CACHE["clock:" + role] = "data:image/svg+xml;base64," + packed
    return dict(_ICON_CACHE)


def _port_number(identity: str) -> str:
    """The port a clock speaks from, which the identity carries after a dash.

    parentPortIdentity is aabbcc.fffe.ddeeff-1. We were throwing the number
    away with the dash and then printing "port not named", when the protocol
    had already said which port it was.
    """
    said = str(identity or "")
    if "-" not in said:
        return ""
    tail = said.rsplit("-", 1)[1].strip()
    return ("port %s" % tail) if tail.isdigit() else ""


def _clock_to_mac(identity: str) -> str:
    """A clock identity as a MAC, with the port number taken off first.

    parentPortIdentity carries the port it speaks from - aabbcc.fffe.ddeeff-13
    - and the device is the part before the dash. Only a dotted identity is
    trimmed, so a MAC written with dashes survives untouched.
    """
    from .ptp import clock_identity_to_mac

    said = str(identity or "")
    if "-" in said and "." in said.split("-")[0]:
        said = said.rsplit("-", 1)[0]
    return clock_identity_to_mac(said)


def ptp_roles(topology: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """What part each device plays in the clock tree, if any.

    A node's own ports say whether it transmits or receives time. Being the
    grandmaster is decided from the other side: a device is one when somebody
    else names its clock identity as theirs, which is how a switch that never
    ran our probe still turns up as the root of the tree.
    """
    from .ptp import normalise_mac

    # A clock names its parent by the MAC of the port the time comes out of,
    # which is not the chassis id, so a host indexed only by chassis went
    # missing from its own tree. Its ports are added first and the chassis
    # id last, so the device's own identity wins any collision.
    by_mac: Dict[str, str] = {}
    by_port: Dict[str, str] = {}
    for node in topology.get("nodes", []):
        extra = node.get("extra") or {}
        for iface, mac in (extra.get("macs") or {}).items():
            said = normalise_mac(mac or "")
            if said:
                by_mac.setdefault(said, node.get("id", ""))
                by_port.setdefault(said, iface)
    for node in topology.get("nodes", []):
        mac = normalise_mac(node.get("chassis_id") or "")
        if mac:
            by_mac[mac] = node.get("id", "")

    masters: Dict[str, str] = {}
    out: Dict[str, Dict[str, Any]] = {}
    for node in topology.get("nodes", []):
        ports = ((node.get("extra") or {}).get("ptp") or {})
        states, domains = [], []
        for port in ports.values():
            state = (port or {}).get("state") or (port or {}).get("role") or ""
            if state:
                states.append(state.upper())
            if (port or {}).get("domain", -1) >= 0:
                domains.append(port["domain"])
            # A free-running port announces its own clock as the best one
            # it knows. Believing that made the device the root of its own
            # tree; gm_present is the flag that says somebody else's time
            # actually arrived.
            if not (port or {}).get("gm_present"):
                continue
            gm = _clock_to_mac((port or {}).get("gm_identity") or "")
            owner = by_mac.get(normalise_mac(gm)) if gm else ""
            if owner and owner != node.get("id", ""):
                masters[owner] = "grandmaster"
        parents: List[Dict[str, Any]] = []
        for name, port in sorted(ports.items()):
            said = ((port or {}).get("state") or
                    (port or {}).get("role") or "").upper()
            if said != "SLAVE":
                continue
            up = _clock_to_mac((port or {}).get("parent_port") or
                               (port or {}).get("gm_identity") or "")
            seen = [p.get("mac") for p in ((port or {}).get("peers") or [])
                    if (p or {}).get("mac")]
            # Failing the management answer, the Sync frame itself carries the
            # master's port MAC in its source address, which is the same fact
            # arrived at by watching the wire instead of asking.
            up = up or (seen[0] if seen else "")
            said = normalise_mac(up) if up else ""
            who = by_mac.get(said)
            leaves = by_port.get(said, "")
            # Another port of the same device is a real answer - a cable
            # looped back, or two ports sharing a segment - and dropping it
            # left the operator with a blank where a timing loop was.
            if who and (who != node.get("id", "") or leaves != name):
                parents.append({"follows": who, "through": name,
                                "out_of": leaves,
                                # Kept aside, not used yet: the cable names
                                # the port better, and only if it cannot do
                                # so is the clock's own number worth saying.
                                "from_clock": _port_number(
                                    (port or {}).get("parent_port") or ""),
                                "under": (port or {}).get("domain")})
        if states or domains:
            out[node.get("id", "")] = {
                "role": "SLAVE" if "SLAVE" in states else
                        ("MASTER" if "MASTER" in states else
                         (states[0] if states else "")),
                "domains": sorted(set(domains)),
                "parents": parents,
            }
            first = parents[0] if parents else {}
            out[node.get("id", "")].update(
                {"follows": first.get("follows", ""),
                 "through": first.get("through", ""),
                 "out_of": first.get("out_of", ""),
                 "under": first.get("under")})
    # A device that takes time from one side and hands it out on another is a
    # boundary clock, not the root. Saying grandmaster there would put two
    # roots on a tree that has one.
    for node_id, role in masters.items():
        seat = out.setdefault(node_id, {"domains": []})
        seat["role"] = "boundary" if seat.get("role") == "SLAVE" else role
    _name_the_far_port(topology, out)
    # Last resort, after the cable has had its say: the protocol told us the
    # port NUMBER, which is worth more than "port not named".
    for seat in out.values():
        for link in seat.get("parents") or []:
            if not link.get("out_of") and link.get("from_clock"):
                link["out_of"] = link["from_clock"]
        first = (seat.get("parents") or [{}])[0]
        if not seat.get("out_of"):
            seat["out_of"] = first.get("out_of", "")
    return out


def _name_the_far_port(topology: Dict[str, Any],
                       roles: Dict[str, Dict[str, Any]]) -> None:
    """Which port of the parent the time leaves by, from the cable we drew.

    A switch's clock identity is its chassis, not a port, so the management
    answer cannot say which socket it came out of. The link already records
    both ends, and the slave's own port names the one we are standing on.
    """
    for node_id, seat in roles.items():
        for link in seat.get("parents") or []:
            if link.get("out_of") or link["follows"] == node_id:
                continue
            for edge in topology.get("edges", []):
                ends = {edge.get("a"): edge.get("a_iface"),
                        edge.get("b"): edge.get("b_iface")}
                if node_id not in ends or link["follows"] not in ends:
                    continue
                if ends.get(node_id) != link.get("through"):
                    continue
                link["out_of"] = ends.get(link["follows"]) or ""
                break
        if seat.get("parents"):
            seat["out_of"] = seat["parents"][0].get("out_of", "")


NOT_A_NAME = ("n/a", "na", "unknown", "none", "-", "null", "not advertised")


def node_label(node: Dict[str, Any]) -> str:
    """What to write under a device on the map.

    Its own name first, then an address somebody could type. Failing both,
    the maker beats the MAC: "Super Micro" is a thing a reader recognises
    and twelve hex digits is not. Where two of a maker turn up the MAC comes
    back automatically, because unique_labels adds it to a repeated label.
    """
    from .oui import vendor

    ips = node.get("mgmt_ips") or []
    name = str(node.get("name") or "").strip()
    if name.lower() in NOT_A_NAME:
        name = ""
    chassis = node.get("chassis_id") or ""
    if name or ips:
        return name or ips[0]
    # Only the maker is known. On its own that reads like a device name, so
    # the tail of the MAC goes with it: recognisable, unique, and visibly
    # not a hostname somebody could type.
    maker = vendor(chassis)
    if maker and chassis:
        tail = ":".join(str(chassis).split(":")[-3:])
        return "%s %s" % (maker, tail) if tail else maker
    return maker or chassis or node.get("id") or "?"


def unique_labels(nodes) -> Dict[str, str]:
    """A label per node id, with same-named devices told apart.

    Two switches of the same model, or two hosts built from one image, can
    answer to the same sysName. That is what they really are called, not a
    mistake. But a panel saying one of them is cabled to "the other one" is
    useless when there are two, so a repeated name carries its address.
    """
    plain = {node.get("id", ""): node_label(node) for node in nodes or []}
    seen: Dict[str, int] = {}
    for label in plain.values():
        seen[label] = seen.get(label, 0) + 1
    out: Dict[str, str] = {}
    for node in nodes or []:
        node_id = node.get("id", "")
        label = plain[node_id]
        if seen.get(label, 0) > 1:
            # The address is the usual way to tell two of a kind apart, but a
            # device already labelled by its address needs its MAC instead -
            # two fronthaul islands really do both use 169.254.12.11.
            ips = node.get("mgmt_ips") or []
            for extra in ((ips[0] if ips else ""), node.get("chassis_id"),
                          node_id):
                if extra and extra != label:
                    label = "%s (%s)" % (label, extra)
                    break
        out[node_id] = label
    return out


def off_net(address: str) -> bool:
    """Is this an address no machine outside its own link can open?

    Nothing counts as off the network too: the safe direction for a
    predicate that decides whether to offer somebody a command.
    """
    text = str(address or "").strip().lower()
    if not text:
        return True
    return (text.startswith("169.254.") or text.startswith("fe80:")
            or text.startswith("127.") or text == "::1")


def reach_plans(topology: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """For each device, where one would have to stand to reach it.

    A fronthaul address lives only inside the container that holds the port,
    so the question "what is its address" has no useful answer on its own -
    the answer is an address *and* a machine to be on. Both are already in
    the scan; this only joins them up.

    The join is by MAC and never by address. Two base stations really do
    both number their radios 169.254.12.11, and keying on the address sends
    the reader to the wrong host with a command that looks right.
    """
    from .ptp import normalise_mac

    inside: Dict[str, Dict[str, Any]] = {}
    for node in topology.get("nodes", []):
        extra = node.get("extra") or {}
        host = node_label(node)
        through = next(iter(node.get("mgmt_ips") or []), "")
        for space in (extra.get("netns") or {}).values():
            for entry in space.get("neighbours") or []:
                mac = normalise_mac(entry.get("mac", ""))
                if mac:
                    inside[mac] = {"host": host, "through": through,
                                   "pod": space.get("name") or "",
                                   "pid": str(space.get("pid") or ""),
                                   "port": entry.get("iface") or ""}

    out: Dict[str, Dict[str, Any]] = {}
    for node in topology.get("nodes", []):
        if node.get("state") == "scanned":
            continue
        ips = [ip for ip in (node.get("mgmt_ips") or []) if ip]
        held = inside.get(normalise_mac(node.get("chassis_id") or ""))
        direct = [ip for ip in ips if not off_net(ip)]
        out[node.get("id", "")] = {"addresses": ips, "direct": direct,
                                   "held": held or {}}
    return out


def _reach_lines(plan: Dict[str, Any]) -> List[str]:
    """What to do next - and nothing at all when there is nothing to do."""
    if not plan:
        return []
    held = plan.get("held") or {}
    direct = plan.get("direct") or []
    # A device that lives inside a pod's namespace is only reached by jumping
    # through the host it sits on, never by a plain ssh to its address - so the
    # namespace path is given first and the direct line is not offered for it,
    # which on a radio read only the wrong way.
    if held:
        where = held.get("pod") or "a container"
        address = next(iter(plan.get("addresses") or []), "?")
        out = ["reachable only inside %s on %s"
               % (where, held.get("host") or "another host"), ""]
        hop = held.get("through") or held.get("host") or "<host>"
        out.append("    ssh %s" % hop)
        if held.get("pid"):
            out.append("    nsenter -t %s -n ssh <user>@%s"
                       % (held["pid"], address))
            out.append("")
            out.append("the process id changes when the container restarts")
        return out
    if direct:
        return ["    ssh %s" % direct[0]]
    # Only a way IN earns a place on the panel. A device nobody can reach has
    # no address on it already - saying so again in a sentence is a paragraph
    # that is true of most unscanned devices and useful about none of them.
    # The full wording stays in the topology JSON for anything that wants it.
    if plan.get("addresses"):
        return ["%s is not reachable from here" % plan["addresses"][0]]
    return []


HOT_FRACTION = 0.9

SKIP_ON_PANEL = {"neighbours": "ARP cache", "netns": "container ports",
                 "dhcp": "DHCP leases"}


def drawn_port(name: Any) -> str:
    """The port as a person reads it, without the namespace it is held in.

    A port inside a container is recorded as "ens2f0np0 in bbu-7fb5f8b7f7-fpg7d"
    because the same interface name means a different port in each namespace,
    so the full string has to stay the key. It is no name to draw across a
    map: the tail is a replicaset hash that changes on every restart. Which
    namespace holds the port is said on the device's own panel instead.
    """
    return str(name or "?").partition(" in ")[0] or "?"


def port_order(name: str) -> Tuple[Any, ...]:
    """Sort port names the way a person reads them, not the way bytes compare.

    Plain sorting puts a 24-port switch in the order 1, 10, 11, ... 2, 20 -
    which is the order nobody looks for a port in. Splitting the digits out
    and comparing them as numbers puts them back in rack order.
    """
    parts = re.split(r"(\d+)", str(name or ""))
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def _timing_lines(ports: Dict[str, Any]) -> List[str]:
    out = []
    for iface in sorted(ports, key=port_order):
        port = ports[iface] or {}
        said = port.get("summary") or port.get("state") or port.get("role") or ""
        steps = port.get("steps_removed")
        if steps and str(port.get("state") or "").upper() == "SLAVE":
            said += ", %d step%s from the grandmaster" % (steps, "" if steps == 1 else "s")
        if said:
            out.append("%-14s %s" % (iface, said))
    return out


def _ports_in_words(names: List[str]) -> str:
    """Name the ports, as a range when there are too many to read one by one."""
    names = [n for n in names if n]
    if len(names) <= 3:
        return ", ".join(names)
    return "%s .. %s (%d)" % (names[0], names[-1], len(names))


def _temperature_lines(cards: Dict[str, Any]) -> List[str]:
    """Which card is at what temperature - and which ports are on that card.

    A chip name and a PCI address do not tell anyone which link goes dark if
    the card is pulled. The card already reports the ports it owns, so they
    go on the same line and the question is answered in one place.
    """
    out = []
    for slot in sorted(cards):
        card = cards[slot] or {}
        now, top = card.get("celsius"), card.get("max_celsius")
        if now is None:
            continue
        ports = _ports_in_words(sorted(card.get("interfaces") or []))
        line = "%-9s %-28s %s" % (
            card.get("chip") or "card", ports or "(%s)" % slot,
            "%d of %d C" % (now, top) if top else "%d C" % now)
        if top and now >= top * HOT_FRACTION:
            line += "  - close to the limit the card itself reports"
        out.append(line)
    return out


def _running_gbps(port: Dict[str, Any]) -> float:
    """What the port settled at, in Gb/s, or 0 if it is not carrying."""
    if not isinstance(port, dict):
        return 0.0
    if str(port.get("carrier") or port.get("state") or "").lower() == "down":
        return 0.0
    digits = re.match(r"\s*(\d+)", str(port.get("speed") or ""))
    if not digits:
        return 0.0
    rate = int(digits.group(1))
    return rate / 1000.0 if rate > 0 else 0.0


def _rate_words(gbps: float) -> str:
    if gbps >= 1:
        return "%gG" % gbps
    return "%gM" % (gbps * 1000)


def _link_words(optic: Dict[str, Any], port: Dict[str, Any]) -> str:
    """What the link is doing, against what the module is rated for.

    Measured on a real estate: three 10G modules were carrying 1 Gb/s, and
    nothing anywhere said so - the line read "10G LR" because that is what
    the module calls itself. The rating and the rate are different numbers
    and only the second one is the network.
    """
    if not isinstance(port, dict) or not port:
        return ""
    running = _running_gbps(port)
    if not running:
        return "link down" if port else ""
    rated = optic.get("speed_gbps") or 0
    if rated and abs(float(rated) - running) > 0.001:
        return "up at %s - the module is rated %gG" % (_rate_words(running),
                                                       float(rated))
    return "up at %s" % _rate_words(running)


def _optics_lines(modules: Dict[str, Any],
                  ports: Optional[Dict[str, Any]] = None) -> List[str]:
    ports = ports or {}
    out, plain = [], []
    for iface in sorted(modules, key=port_order):
        optic = modules[iface] or {}
        bits = []
        if optic.get("speed_gbps"):
            bits.append("%sG" % optic["speed_gbps"])
        for key in ("form", "media"):
            if optic.get(key):
                bits.append(str(optic[key]))
        # The wavelength and the reach were read off the module and then
        # left out of the line, which is the part somebody matching a
        # module to a fibre actually needs: 850 nm will not light a
        # single-mode run and no amount of "10G SR" says so.
        if optic.get("wavelength_nm"):
            bits.append("%s nm" % optic["wavelength_nm"])
        for key, unit in (("reach_smf_km", "km"), ("reach_om3_m", "m OM3"),
                          ("reach_copper_m", "m copper")):
            if optic.get(key):
                bits.append("%s %s" % (optic[key], unit))
                break
        # What it is rated for is the module's word; what it settled at is
        # the network's.
        running = _link_words(optic, ports.get(iface) or {})
        maker = str(optic.get("vendor") or "")
        if bits or running:
            tail = [part for part in (" ".join(bits), running, maker) if part]
            out.append("%-14s %s" % (iface, ", ".join(tail)))
        elif maker:
            # Nothing but a maker's name: nine such lines say one thing nine
            # times. The ports are still named, on one line.
            plain.append((iface, maker))
    for maker in sorted(set(m for _i, m in plain)):
        named = [i for i, m in plain if m == maker]
        out.append("%d more %s module(s): %s"
                   % (len(named), maker, ", ".join(named)))
    return out


# The same words the page header and the command-line table use, so a device
# is not "interrogated" here and "scanned" two inches away.
STATE_WORDS = {
    "scanned": "scanned",
    "unscanned": "not scanned - a neighbour reported it",
    "unreachable": "unreachable - it did not answer",
    "out-of-scope": "out of scope - outside the range this walk was given",
    "limit-reached": "not scanned - the device limit was reached first",
}


def _identity_lines(node: Dict[str, Any]) -> List[str]:
    """Who this is and whether we got in - and nothing about how.

    The probe that happened to answer, the crawl depth and the internal node
    key are bookkeeping. They tell a person nothing about their network, and
    printing them is what made this panel unreadable.
    """
    from .oui import kind_from_maker, vendor_of

    out = []
    kind = node.get("kind") or "unknown"
    guess = kind_from_maker(vendor_of(node)["vendor"]) if kind == "unknown" else ""
    ips = ", ".join(node.get("mgmt_ips") or []) or "no management address"
    out.append("%-14s %s" % (guess or kind, ips))
    state = node.get("state") or ""
    error = str(node.get("error") or "").strip()
    # The gloss explains a state nobody asked about; a real error IS the
    # explanation, so it replaces the gloss rather than trailing after it.
    said = ("%s - %s" % (state or "unknown", error) if error
            else STATE_WORDS.get(state, state))
    if said:
        out.append("%-14s %s" % ("", said))
    from .oui import vendor_of

    said = vendor_of(node)
    if said["vendor"]:
        # Just the maker. Where it was read from mattered only when it was a
        # guess; a solid reading needs no "(the board's own record (DMI))"
        # trailing it, which was parentheses inside parentheses and pure noise.
        if said["vendor_confidence"] == "high":
            out.append("%-14s %s" % ("made by", said["vendor"]))
        else:
            out.append("%-14s %s (%s confidence)"
                       % ("made by", said["vendor"], said["vendor_confidence"]))
    elif said["mac_type"] in ("locally administered", "multicast") and kind != "radio":
        # Not for a radio: its node is keyed by the EUI-64 form of its MAC
        # (locally-administered bit set), so "nobody - locally administered"
        # was pure noise next to a model and maker already read from the log.
        out.append("%-14s %s" % ("made by", "nobody - this address is %s"
                                 % said["mac_type"]))

    # Where the address itself came from. A device only PTP ever mentioned
    # has a MAC derived from its clock identity and nothing more - saying so
    # stops the maker's name reading like an identification.
    if [str(x) for x in (node.get("sources") or [])] == ["ptp"]:
        out.append("%-14s only the timing protocol named it - this address "
                   "is its PTP clock identity, and nothing else on the "
                   "network reported it" % "")

    from .oui import kind_from_maker

    if not (node.get("kind") or "") or node.get("kind") == "unknown":
        guess = kind_from_maker(said["vendor"])
        if guess:
            out.append("%-14s guessed from the maker - nothing on the "
                       "device was asked" % "")

    for label, key in (("chassis", "chassis_id"), ("serial", "serial"),
                       ("model", "vendor")):
        value = str(node.get(key) or "").strip()
        if value:
            out.append("%-14s %s" % (label, value))
    for line in ((node.get("extra") or {}).get("identified") or {}
                 ).get("facts") or []:
        out.append("%-14s %s" % ("", line))
    return out


NO_PARTNER = ("00:00:00:00:00:00", "")

SETTLED = ("none", "", "monitoring")


MGMT_MARK = {"mgmt": " [mgmt]", "mgmt-leg": " [mgmt-leg]"}


def _port_lines(ports: Dict[str, Any],
                real: Optional[Dict[str, Any]] = None) -> List[str]:
    """Which real network card holds which address.

    A bridge, a veth or an overlay interface holds an address that no cable
    ever carries, so it cannot answer "which port is this link in" - the one
    question the panel exists for. Only cards with a hardware address are
    listed; a link-local address distinguishes no port and is folded away.
    """
    rows = []
    soft = []
    for name, port in (ports or {}).items():
        if real is not None and name not in real:
            soft.append(name)
            continue
        role = str((port or {}).get("role") or "")
        held = [a for a in ((port or {}).get("addresses") or [])
                if not str(a).lower().startswith(("fe80", "::1"))]
        held = held or list((port or {}).get("addresses") or [])
        rows.append((0 if role.startswith("mgmt") else 1, name, role, held, port))
    out, bare = [], []
    for _first, name, role, held, port in sorted(
            rows, key=lambda row: (row[0], port_order(row[1]))):
        if held and all(str(a).lower().startswith(("fe80", "::1")) for a in held):
            bare.append(name)
            continue
        # A port with no address is not an address this device answers on.
        # It is kept now so a lit cable is visible, and it has its own
        # section; repeating it here under this heading reads as nonsense.
        if not held and not role.startswith("mgmt"):
            continue
        said = [s for s in (str(port.get("state") or ""),
                            str(port.get("speed") or "")) if s]
        out.append("%-14s %-26s %s" % (name + MGMT_MARK.get(role, ""),
                                       ", ".join(held) or "no address",
                                       ", ".join(said)))
    if bare:
        out.append("%-14s %s" % ("", "%d port(s) hold only a link-local "
                                 "address: %s" % (len(bare), ", ".join(bare))))
    if soft:
        out.append("%-14s %s" % ("", "%d software interface(s), in the JSON: %s"
                                 % (len(soft), ", ".join(sorted(soft)))))
    return out


FLAPPED = 5             # measured: healthy legs sit at 0-1, sick ones in the teens


def _bond_findings(members: List[str], info: Dict[str, Any],
                   legs: Dict[str, Any]) -> List[str]:
    """The ways a bond can be wrong while still reporting itself as a bond.

    Each of these is silent from outside: the interface is up, traffic flows,
    and capacity or redundancy is quietly not what anybody thinks it is.
    """
    out: List[str] = []
    mode = str(info.get("mode") or "")
    lacp = "802.3ad" in mode

    down = [m for m in members
            if str((legs.get(m) or {}).get("mii", "")).lower() not in ("up", "")]
    if down and len(down) < len(members):
        out.append("%d of %d legs are down - the capacity is not what it looks"
                   % (len(down), len(members)))
    elif down and len(down) == len(members):
        out.append("every leg is down")

    if lacp and str(info.get("ad_partner_mac") or "").strip().lower() in NO_PARTNER:
        out.append("LACP is not negotiating - no partner answered, so the "
                   "switch side is not set up for 802.3ad")

    churning = [m for m in members
                if str((legs.get(m) or {}).get("actor_churn", "")).lower()
                not in SETTLED
                or str((legs.get(m) or {}).get("partner_churn", "")).lower()
                not in SETTLED]
    if lacp and churning:
        out.append("LACP has not settled on %s - a churning leg is up but "
                   "carries nothing" % ", ".join(sorted(churning)))

    # A leg that keeps coming back is not a healthy leg. Measured across the
    # benches: most sit at 0 or 1, and the ones people complain about sit in
    # the teens - a cable, an optic or a switch port that flaps. The count is
    # printed either way; above the line it is said out loud.
    flapping = []
    for member in members:
        said = str((legs.get(member) or {}).get("failures", "")).strip()
        if said.isdigit() and int(said) >= FLAPPED:
            flapping.append((member, int(said)))
    if flapping:
        out.append("%s dropped and came back repeatedly: %s"
                   % (_count(len(flapping), "leg"),
                      ", ".join("%s %d times" % (n, c)
                                for n, c in sorted(flapping))))

    groups = {str((legs.get(m) or {}).get("aggregator", "")) for m in members}
    groups.discard("")
    if lacp and len(groups) > 1:
        out.append("the legs are in %d different aggregators, so they are not "
                   "bundled together at all" % len(groups))
    return out


CARD = re.compile(r"^(.*?)f\d+(?:np\d+)?$")


def _count(n: int, thing: str) -> str:
    """'1 leg' rather than '1 leg(s)'; a report is read by people."""
    return "%d %s%s" % (n, thing, "" if n == 1 else "s")


def _card_of(port: str) -> str:
    """Which NIC a port lives on, from the name the kernel gives it."""
    found = CARD.match(str(port or ""))
    return found.group(1) if found else ""


def idle_lit(ports: Dict[str, Any]) -> List[str]:
    """Ports with a live cable in them that belong to nothing.

    The case this exists for: a bond loses legs, stays up on the last one,
    and nobody notices because everything still works. The ports are up, the
    light is good, and they are in no bond, no bridge and no address - which
    no report anywhere used to say out loud.
    """
    out = []
    for name in sorted(ports or {}, key=port_order):
        info = ports[name] or {}
        if str(info.get("carrier") or "") != "up":
            continue
        if [a for a in (info.get("addresses") or []) if a]:
            continue
        if str(info.get("role") or "") != "physical":
            continue
        out.append(name)
    return out


def _idle_lines(ports: Dict[str, Any], optics: Dict[str, Any]) -> List[str]:
    """One line per lit port that carries nothing, with the light on it."""
    out = []
    for name in idle_lit(ports):
        bits = []
        speed = str((ports[name] or {}).get("speed") or "")
        if speed:
            bits.append(speed)
        module = (optics or {}).get(name) or {}
        if module.get("rx_dbm") is not None:
            bits.append("light in %s dBm" % module["rx_dbm"])
        if module.get("part"):
            bits.append(str(module["part"]))
        out.append("%-14s %s" % (name, ", ".join(bits)
                                 or "up, in nothing"))
    if out:
        out.append("%-14s %s" % ("", "a cable is in each of these and it is "
                                     "in no bond, no bridge and no address"))
    return out


def _bond_lines(bonds: Dict[str, Any], detail: Dict[str, Any],
                ports: Optional[Dict[str, Any]] = None) -> List[str]:
    """What a bond is set to, how its legs are, and whether it is bonding."""
    out: List[str] = []
    spare = idle_lit(ports or {})
    for name in sorted(bonds or {}):
        members = bonds[name] or []
        info = (detail or {}).get(name) or {}
        legs = {leg["name"]: leg for leg in info.get("legs") or []}

        head = "%-14s %s" % (name, str(info.get("mode") or "mode not reported"))
        rate = str(info.get("lacp_rate") or "").split()[0] if info.get("lacp_rate") else ""
        if rate:
            head += ", lacp %s" % rate
        out.append(head)

        for member in sorted(set(members) | set(legs)):
            leg = legs.get(member) or {}
            bits = [str(leg.get("mii") or "state not reported")]
            speed = str(leg.get("speed") or "")
            if speed and speed.lower() != "unknown":
                bits.append(speed)
            failures = str(leg.get("failures") or "0").strip()
            if failures.isdigit() and int(failures) > 0:
                bits.append("%s link failure%s"
                            % (failures, "" if failures == "1" else "s"))
            out.append("  %-14s %s" % (member, ", ".join(bits)))

        held = sorted(set(members) | set(legs))
        out.extend("  " + line for line in _bond_findings(held, info, legs))
        # A bond on its last leg beside three lit ports of the same card is
        # the commonest quiet failure here, and neither half said it alone.
        cards = {_card_of(m) for m in held} - {""}
        beside = sorted(p for p in spare if _card_of(p) in cards)
        if beside and len(held) < len(held) + len(beside):
            out.append("  the bond has %s, and %s on the same card %s lit "
                       "and in nothing: %s"
                       % (_count(len(held), "leg"),
                          _count(len(beside), "more port"),
                          "is" if len(beside) == 1 else "are",
                          ", ".join(beside)))
    return out


def _v4_net(address: str) -> str:
    """'192.0.2.52/24' into the network it sits on, '192.0.2.0/24'."""
    text = str(address or "")
    if ":" in text or "/" not in text:
        return ""
    host, _, bits = text.partition("/")
    parts = host.split(".")
    if len(parts) != 4 or not bits.isdigit():
        return ""
    if not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return ""
    width = int(bits)
    if not 8 <= width <= 30:
        return ""
    packed = 0
    for part in parts:
        packed = (packed << 8) | int(part)
    base = packed & (0xFFFFFFFF << (32 - width)) & 0xFFFFFFFF
    return "%d.%d.%d.%d/%d" % (base >> 24 & 255, base >> 16 & 255,
                               base >> 8 & 255, base & 255, width)


def port_neighbours(topology: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, str]],
                                                       Dict[str, List[str]]]:
    """Which port each link was found on, and who the neighbours of a network are.

    The second half is what answers "which switch is this management port
    in" for a host that reported no neighbour of its own: the other hosts
    holding an address on the same network did report one, and that is
    worth saying as long as it is said as their evidence and not as ours.
    """
    labels = unique_labels(topology.get("nodes", []))
    kinds = {str(n.get("id") or ""): str(n.get("kind") or "")
             for n in topology.get("nodes", [])}
    found: Dict[str, Dict[str, str]] = {}
    for edge in topology.get("edges", []):
        for near, iface, far in (("a", "a_iface", "b"), ("b", "b_iface", "a")):
            node_id = str(edge.get(near) or "")
            name = str(edge.get(iface) or "")
            if not (node_id and name and name != "?"):
                continue
            far_id = str(edge.get(far) or "")
            held = found.setdefault(node_id, {})
            # One port can report more than one neighbour - a switch and a
            # machine behind it, both heard on the same cable. This kept
            # whichever came last, so the switch was thrown away half the
            # time and the port looked like it led to another server.
            if name in held and kinds.get(far_id) != "switch":
                continue
            held[name] = labels.get(far_id, "")
    # What a far end is matters here: the question this answers is "which
    # switch is my management port in", and the first host that happened to
    # be listed answered it with another host. Switches go first.
    kind_of = {labels.get(str(n.get("id") or ""), ""): str(n.get("kind") or "")
               for n in topology.get("nodes", [])}
    nets: Dict[str, List[str]] = {}
    for node in topology.get("nodes", []):
        mine = found.get(str(node.get("id") or "")) or {}
        ports = ((node.get("extra") or {}).get("ports") or {})
        for name, port in ports.items():
            far = mine.get(name) or ""
            if not far:
                continue
            for address in (port or {}).get("addresses") or []:
                net = _v4_net(address)
                # A link-local range is per cable, not a network two
                # machines can be said to share.
                if not net or str(address).startswith("169.254."):
                    continue
                if far not in nets.setdefault(net, []):
                    nets[net].append(far)
    for net, seen in nets.items():
        seen.sort(key=lambda label: 0 if kind_of.get(label) == "switch" else 1)
    return found, nets


def _silent_port_lines(node: Dict[str, Any], found: Dict[str, str],
                       nets: Dict[str, List[str]]) -> List[str]:
    """Ports with a cable in them that nothing ever reported a neighbour on.

    A port missing from the map looks the same as a port nobody looked at,
    and the difference is the whole answer, so the ports that came back
    empty are listed by name instead of being left out.
    """
    extra = node.get("extra") or {}
    real = extra.get("macs") or {}
    out: List[str] = []
    for name, port in sorted((extra.get("ports") or {}).items()):
        if name in found or name not in real:
            continue
        if str((port or {}).get("state") or "") != "up":
            continue
        role = str((port or {}).get("role") or "")
        hints = []
        for address in (port or {}).get("addresses") or []:
            net = _v4_net(address)
            seen = nets.get(net) or []
            if seen:
                hints.append("others on %s report %s"
                             % (net, ", ".join(sorted(seen)[:3])))
        # A management port faces a switch the walk was usually never given,
        # so "nothing answered" is the normal case and not a finding. It is
        # worth a line only when somebody else on that network names one.
        if role.startswith("mgmt") and not hints:
            continue
        if hints:
            out.append("%-14s %s" % (name + MGMT_MARK.get(role, ""),
                                     hints[0].strip()))
            out.extend("%-14s %s" % ("", h.strip()) for h in hints[1:])
        else:
            out.append(name + MGMT_MARK.get(role, ""))
    return out


# What the inventory adds that a scan cannot work out for itself: what the
# thing is FOR, whose it is, and which bench it belongs to.
NETBOX_LINES = (("recorded as", "name"), ("role", "role"),
                ("site", "site"), ("location", "location"),
                ("rack", "rack"), ("owner", "tenant"),
                ("asset tag", "asset_tag"), ("status", "status"))


def _self_cable_lines(node: Dict[str, Any]) -> List[str]:
    """Ports of this device that are cabled to each other.

    It draws as a loop on the picture and said nothing, so somebody had to
    ask what it meant. It is worth saying because the answer is not
    obvious: a cable from a machine back into itself is harmless while the
    two ports are separate, and a broadcast storm the moment they are put
    in one bridge or bond. Both halves of that are known here.
    """
    from .ptp import normalise_mac

    extra = node.get("extra") or {}
    mine = {normalise_mac(mac): port
            for port, mac in (extra.get("macs") or {}).items() if mac}
    joined = extra.get("bridges") or []
    bonded = set()
    for legs in (extra.get("bonds") or {}).values():
        bonded |= {str(leg) for leg in (legs or [])}

    # Only what is keyed by a port of this machine. The IPv4 cache is keyed
    # by address instead, and reading it the same way produced a line
    # headed fe80::... claiming to be a port.
    ports = set((extra.get("ports") or {})) | set(extra.get("macs") or {})
    seen = []
    for port, found in (extra.get("neigh6") or {}).items():
        if port not in ports:
            continue
        for peer in ((found or {}).get("peers") or []):
            far = mine.get(normalise_mac((peer or {}).get("mac", "")))
            # One cable, named once: both ends see each other, so without
            # this every pair was printed twice, once from each end.
            if far and far != port and {far, port} not in [set(p) for p in seen]:
                seen.append((port, far))
    if not seen:
        return []

    out = []
    for port, far in seen:
        out.append("%-14s a cable runs from here to %s on this same device"
                   % (port, far))
    together = [pair for pair in seen
                if set(pair) & bonded or (joined and set(pair) & set(joined))]
    if together:
        out.append("%-14s %s" % ("", "and they are joined at layer 2 - that "
                                     "is a loop that will flood"))
    else:
        out.append("%-14s %s" % ("", "they are in no bridge and no bond, so "
                                     "nothing is forwarded between them"))
    return out


# Readings that belong together on one line, in the order a person checks
# them. Matched by what the name contains rather than by a list of exact
# names: one box calls it board-temperature, the next board-temperature1 and
# board-temperature2, and a list of exact names quietly leaves the second
# box's readings in a heap at the bottom.
HEALTH_GROUPS = (
    ("temperature", ("temperature", "temp")),
    ("fans", ("fan",)),
    ("power", ("psu", "power", "voltage", "current")),
)


def _health_group(name: str) -> str:
    low = str(name or "").lower()
    for label, marks in HEALTH_GROUPS:
        if any(mark in low for mark in marks):
            return label
    return ""


def _reading(name: str, rec: Dict[str, Any]) -> str:
    value = rec.get("value")
    unit = str(rec.get("unit") or "")
    said = "%s %s" % (value, unit) if unit else str(value)
    short = name.replace("-state", "").replace("-speed", "").replace("-temperature", "")
    return "%s %s" % (short or name, said.strip())


def _health_lines(health: Dict[str, Any]) -> List[str]:
    """What the box says about its own temperature, fans and power.

    A failed power supply is the reading worth the whole section: a switch
    on one of its two feeds carries on exactly as before and says nothing,
    until the day the other one goes. So anything not reporting healthy is
    repeated on its own line, in plain words, rather than sitting in a row
    of numbers that all look alike.
    """
    if not health:
        return []
    out = []
    for label, _marks in HEALTH_GROUPS:
        said = [_reading(n, health[n]) for n in sorted(health, key=port_order)
                if _health_group(n) == label]
        if said:
            out.append("%-14s %s" % (label, ", ".join(said)))
    for name in sorted(health, key=port_order):
        if not _health_group(name):
            out.append("%-14s %s" % ("", _reading(name, health[name])))
    unwell = sorted(n for n, rec in health.items() if not rec.get("ok", True))
    for name in unwell:
        out.append("%-14s %s reports %s - this device is running on what is left"
                   % ("not well", name, health[name].get("value")))
    return out


def _plugged_in_lines(usb: Dict[str, Any]) -> List[str]:
    """What is on this machine's USB, by kind, with the port it sits on.

    Only handsets are drawn on the map, so everything else was visible
    nowhere at all. One line per phone, because that is the one a test
    depends on, and a count for the rest.
    """
    # Two different things are filed under "usb". On a host it is a device
    # per port; on a handset it is that handset's own record, flat, with
    # string values. Reading the second as the first threw, and a panel that
    # throws takes the whole page with it - no map at all, which is how this
    # was found. A device is a dict; anything else is not this section's.
    devices = {port: entry for port, entry in (usb or {}).items()
               if isinstance(entry, dict)}
    if not devices:
        return []
    out, others = [], {}
    for port, entry in sorted(devices.items()):
        kind = str((entry or {}).get("kind") or "peripheral")
        if kind != "phone":
            others[kind] = others.get(kind, 0) + 1
            continue
        from .handsets import describe

        # The phone and its serial, nothing more. The name already carries the
        # serial when that is all there is to call it by, so it is not then
        # repeated; the USB bus path and the adb state were noise on a panel
        # meant to answer "which phones are here".
        name = describe(entry)
        serial = str(entry.get("adb_serial") or entry.get("serial") or "")
        line = name
        if serial and serial.lower() not in name.lower():
            line = "%s, serial %s" % (name, serial)
        out.append("%-14s %s" % ("phone", line))
    for kind in sorted(others):
        out.append("%-14s %d" % (kind, others[kind]))
    return out


def _serial_lines(ports: Dict[str, Any]) -> List[str]:
    """The serial ports worth looking at: a real connection, radio cable first.

    Only the on-board UARTs and the USB-serial adapters that look like a radio
    console are shown; a Bluetooth COM port and other virtual noise are left in
    the JSON record but kept off the panel, so what is listed is the handful of
    ports a radio could actually be on. The full set stays in the topology JSON.
    """
    from .serialports import one_line

    records = [rec for rec in (ports or {}).values()
               if isinstance(rec, dict)
               and (rec.get("kind") == "uart" or rec.get("likely_radio"))]
    if not records:
        return []
    records.sort(key=lambda r: (not r.get("likely_radio"), str(r.get("port"))))
    out = []
    for rec in records:
        out.append(one_line(rec))
        heard = rec.get("heard")
        if heard:
            out.append("    heard at %d baud:" % rec.get("baud", 115200))
            for line in str(heard).splitlines()[:6]:
                out.append("      %s" % line)
        elif rec.get("baud"):
            out.append("    listened at %d baud - nothing printed "
                       "(idle, or needs Enter)" % rec["baud"])
    return out


def _radio_lines(identified: Dict[str, Any]) -> List[str]:
    """The radios this host reached inside a pod, named by what they told us.

    One line per radio: its link-local address, and - when its M-plane was up
    and answered - its own model and serial read over NETCONF. A radio that
    only answered the port probe still appears, just without a serial.
    """
    out = []
    for mac, rec in sorted((identified or {}).items()):
        if not isinstance(rec, dict) or rec.get("kind") != "radio":
            continue
        where = rec.get("address") or mac
        bits = [b for b in (rec.get("model"),
                "serial %s" % rec["serial"] if rec.get("serial") else "") if b]
        out.append("%-15s %s" % (where, ", ".join(bits) if bits
                                 else "a radio (its serial was not readable)"))
    return out


def _namespace_lines(spaces: Dict[str, Any]) -> List[str]:
    """Which cards this machine has handed to something else.

    A port inside a container is not on the host's own network: it is up, it
    carries traffic, and the host cannot see any of it. That changes what an
    engineer does next, so it is said here - in a panel, where a pod name can
    be read - rather than written along a link on the map.

    Only cards with a PCI address are listed. A namespace is full of dummy and
    veth interfaces that are not ports of anything.
    """
    out = []
    for _inode, space in sorted((spaces or {}).items(),
                                key=lambda kv: str((kv[1] or {}).get("name") or kv[0])):
        held = sorted((name for name, itf in ((space or {}).get("ifaces") or {}).items()
                       if (itf or {}).get("pci")), key=port_order)
        if not held:
            continue
        out.append(str((space or {}).get("name") or "a namespace"))
        out.append("%-14s %s" % ("", ", ".join(held)))
    if out:
        out.append("%-14s %s" % ("", "these ports are up and carrying traffic "
                                 "the host itself cannot see"))
    return out


def _pdu_lines(pdu: Dict[str, Any]) -> List[str]:
    """A rack PDU: what it is, and what is plugged into each socket.

    The outlet names are whatever somebody typed when the rack was built.
    They are labels, not hostnames - and on most benches they are the only
    written record of the wiring there is, which is why they are printed
    exactly as they were typed rather than tidied into something that
    looks like a device name.
    """
    if not pdu:
        return []
    out = []
    said = [str(pdu.get(k)) for k in ("model number", "serial number")
            if pdu.get(k)]
    if said:
        out.append("%-14s %s" % (said[0], "serial %s" % said[1]
                                 if len(said) > 1 else ""))
    for key, label in (("hardware revision", "hardware revision"),
                       ("manufacture date", "made"),
                       ("management uptime", "up for")):
        if pdu.get(key):
            out.append("%-14s %s %s" % ("", label, pdu[key]))
    outlets = pdu.get("outlets") or []
    if outlets:
        lit = sum(1 for o in outlets if str(o.get("state", "")).lower() == "on")
        out.append("%-14s %d outlet(s), %d on" % ("", len(outlets), lit))
        for one in outlets:
            out.append("%-14s %-3s %-34s %s"
                       % ("", one.get("outlet", "?"),
                          str(one.get("label") or "(not named)")[:34],
                          one.get("state", "?")))
    return out


def _recorded_bmc(inventory: Any) -> str:
    """The address NetBox holds on this device's IPMI interface, if any."""
    if not isinstance(inventory, dict):
        return ""
    for held in inventory.get("addresses") or []:
        if "ipmi" in str((held or {}).get("port", "")).lower():
            return str(held.get("address") or "")
    return ""


def _bmc_lines(found: Dict[str, Any], inventory: Any) -> List[str]:
    """What the host says its own controller is, and whether that is on record.

    Nothing here is written to NetBox. A disagreement is reported with both
    addresses and a person decides which is true; an empty record is offered
    the address to fill it with, as text, to copy.
    """
    if not found:
        return []
    recorded = _recorded_bmc(inventory)
    # Which tool was not installed is not an answer to anything. Either an
    # address is known or it is not: give the ones there are, say where each
    # came from, and say nothing at all when there are none.
    if found.get("tool") == "absent":
        out = []
        if recorded:
            out.append("%-14s %s" % (recorded, "from NetBox"))
        seen = found.get("seen_from")
        if seen:
            out.append("%-14s %s" % ("", "%s has it in its neighbour cache, "
                                         "so it is alive" % seen))
        return out
    out = []
    address = str(found.get("address") or "")
    if not address:
        out.append("%-14s %s" % ("", "a controller is present and no address "
                                     "is configured on it"))
        if recorded:
            out.append("%-14s %s" % (recorded, "NetBox records this address "
                                               "for it"))
        return out
    said = ["from the host"]
    if found.get("source"):
        said.append(str(found["source"]))
    # The channel is only worth a word when it is not the usual one.
    if found.get("channel") and str(found["channel"]).lower() != "default":
        said.append("channel %s" % found["channel"])
    out.append("%-14s %s" % (address, ", ".join(said)))
    # Who did the asking matters. A controller usually sits on a management
    # network the host itself is not on, so "the host could not reach it" is
    # a different sentence from "nothing is there" - and saying the second
    # sends somebody to a rack for no reason. Measured on a live server:
    # the host could not reach its own BMC, and another machine on the map
    # had that address in its neighbour cache the whole time.
    if "answers" in found:
        out.append("%-14s %s" % ("", "the host it lives in can reach it"
                                 if found["answers"] else
                                 "the host it lives in CANNOT reach it"))
    seen = found.get("seen_from")
    if seen:
        out.append("%-14s %s" % ("", "%s has it in its neighbour cache, so it "
                                     "is alive" % seen))
    elif found.get("answers") is False:
        out.append("%-14s %s" % ("", "and nothing else on this map has seen it "
                                     "either"))
    if found.get("mac"):
        out.append("%-14s %s" % ("", "hardware address %s" % found["mac"]))

    # Both sources, each on its own line with its own address. Where they
    # agree that is one line; where they differ it is two, and which is true
    # is a person's call, not this tool's.
    recorded = _recorded_bmc(inventory)
    if recorded and recorded == address:
        out.append("%-14s %s" % ("", "NetBox records the same"))
    elif recorded:
        out.append("%-14s %s" % (recorded, "from NetBox - these disagree"))
    return out


def _netbox_lines(record: Dict[str, Any]) -> List[str]:
    """One line each, and only for what the record actually holds.

    In the same two columns as every other section. This one wrote
    "role: Server" inline, so the panel changed shape halfway down for no
    reason a reader could see.
    """
    out = []
    for label, key in NETBOX_LINES:
        value = str(record.get(key) or "").strip()
        if value:
            out.append("%-14s %s" % (label, value))
    tags = [t for t in (record.get("tags") or []) if t]
    if tags:
        out.append("%-14s %s" % ("setups", ", ".join(sorted(tags))))
    how = str(record.get("matched_by") or "")
    if how:
        out.append("%-14s %s" % ("", "matched by %s" % how))
    return out


def device_facts(node: Dict[str, Any],
                 plan: Optional[Dict[str, Any]] = None,
                 found: Optional[Dict[str, str]] = None,
                 nets: Optional[Dict[str, List[str]]] = None
                 ) -> List[Dict[str, Any]]:
    """The few things worth reading off a device, as sections of short lines.

    The record behind a device holds every reading the probes took. Printed
    raw it is a screenful of JSON per host, which is not an answer to any
    question - so each section that has something to say says it in one line
    per port or per card, and the full record stays in the topology JSON for
    whatever reads it next.
    """
    extra = node.get("extra") or {}
    sections = [{"title": node_label(node), "lines": _identity_lines(node)}]
    # Optics is given the ports as well: the module says what it is rated
    # for, only the port says what the link settled at.
    for key, title, builder in (
            ("ptp", "Timing", _timing_lines),
            ("sensors", "Card temperature", _temperature_lines),
            ("optics", "Optics",
             lambda m: _optics_lines(m, extra.get("ports") or {}))):
        value = extra.get(key)
        if isinstance(value, dict) and value:
            lines = builder(value)
            if lines:
                sections.append({"title": title, "lines": lines})
    looped = _self_cable_lines(node)
    if looped:
        sections.append({"title": "Cabled to itself", "lines": looped})
    well = _health_lines(extra.get("health") or {})
    if well:
        sections.append({"title": "How the box itself is doing", "lines": well})
    plugged = _plugged_in_lines(extra.get("usb") or {})
    if plugged:
        sections.append({"title": "Plugged in", "lines": plugged})
    tty = _serial_lines(extra.get("serial") or {})
    if tty:
        sections.append({"title": "Serial ports", "lines": tty})
    rad = _radio_lines(extra.get("identified") or {})
    if rad:
        sections.append({"title": "Radios on this host", "lines": rad})
    detail = extra.get("radios_detail") or {}
    if detail:
        lines = []
        for serial, rec in sorted(detail.items()):
            bits = [rec.get("model"), "serial %s" % serial]
            if rec.get("hw_rev"):
                bits.append("HW %s" % rec["hw_rev"])
            if rec.get("sw_rev"):
                bits.append("SW %s" % rec["sw_rev"])
            lines.append(", ".join(b for b in bits if b))
        sections.append({"title": "Radios this host records", "lines": lines})
    elif extra.get("radios_in_logs"):
        sections.append({"title": "Radios this host records",
                         "lines": ["serial %s" % s
                                   for s in extra["radios_in_logs"]]})
    # The pod name used to be drawn along every one of these links. It is a
    # replicaset hash that changes on every restart, and it was the longest
    # thing on the picture; here it is a line somebody can read.
    handed = _namespace_lines(extra.get("netns") or {})
    if handed:
        sections.append({"title": "Ports held by something else",
                         "lines": handed})
    power = _pdu_lines(extra.get("pdu") or {})
    if power:
        sections.append({"title": "Power", "lines": power})
    inventory = extra.get("netbox")
    bmc = _bmc_lines(extra.get("ipmi") or {}, inventory)
    if bmc:
        sections.append({"title": "IPMI/BMC address", "lines": bmc})

    if isinstance(inventory, dict) and inventory:
        lines = _netbox_lines(inventory)
        if lines:
            sections.append({"title": "In NetBox", "lines": lines})

    held = _port_lines(extra.get("ports") or {}, extra.get("macs") or {})
    if held:
        sections.append({"title": "Addresses it answers on", "lines": held})

    quiet = _silent_port_lines(node, found or {}, nets or {})
    if quiet:
        sections.append({"title": "Ports with no neighbour found",
                         "lines": quiet})

    # The leases are not listed. Measured on a real estate, 15 of 16 of them
    # were already drawn as a link from this very host, and the link names
    # the port - so the list repeated the picture. The record stays in the
    # topology JSON, which the panel points at.

    # Not for a radio: it lives inside a pod's namespace and the only way in is
    # the jump through the host, which anyone working on radios already knows -
    # so the instructions were clutter on exactly the node that always has them.
    if node.get("kind") != "radio":
        getting = _reach_lines(plan or {})
        if getting:
            sections.append({"title": "How to reach it", "lines": getting})


    bond_lines = _bond_lines(extra.get("bonds") or {},
                             extra.get("bond_detail") or {},
                             extra.get("ports") or {})
    if bond_lines:
        sections.append({"title": "Bonds", "lines": bond_lines})

    idle = _idle_lines(extra.get("ports") or {}, extra.get("optics") or {})
    if idle:
        sections.append({"title": "Cabled and carrying nothing",
                         "lines": idle})

    held = []
    for key, value in sorted(extra.items()):
        if key in ("ptp", "sensors", "optics", "bonds", "bond_detail",
                   "netns_hidden", "identified", "netns_vfs", "ports", "netns",
                   "ipmi", "perm_macs", "health", "usb"):
            continue
        if isinstance(value, dict) and value:
            held.append("%s (%d)" % (SKIP_ON_PANEL.get(key, key), len(value)))
    if held:
        sections.append({"title": "Also recorded, in the JSON",
                         "lines": [", ".join(held)]})
    return sections


CONFIDENCE_WORDS = {
    "confirmed": "both ends reported it",
    "reported": "one end reported it",
    "inferred": "worked out, not reported by either end",
}

# "Worked out" is honest but vague, and the reader's next question is always
# "worked out from what". Each source answers it in its own words rather than
# every link claiming a forwarding table it never saw.
INFERRED_FROM = {
    "ipv6-neighbour": "worked out from what answered on the port, "
                      "not reported by either end",
    "ptp": "worked out from the timing traffic, not reported by either end",
    "snmp-fdb": "worked out from a forwarding table, not reported by either end",
    "switch-fdb": "worked out from a forwarding table, not reported by either end",
}


def _confidence_words(edge: Dict[str, Any]) -> str:
    """How sure we are, and - when it was worked out - from what."""
    how = str(edge.get("confidence") or "")
    if how != "inferred":
        return CONFIDENCE_WORDS.get(how, how)
    for source in edge.get("sources") or []:
        if source in INFERRED_FROM:
            return INFERRED_FROM[source]
    return CONFIDENCE_WORDS["inferred"]

VERDICT_WORDS = {
    "match": "agree",
    "mismatch": "do not match",
    "warn": "worth a look",
}


def _when(stamp: Any) -> str:
    """2026-10-07T13:54:27 reads as 2026-10-07 13:54; the seconds help nobody."""
    text = str(stamp or "").strip()
    if "T" in text:
        day, _sep, clock = text.partition("T")
        return "%s %s" % (day, clock[:5])
    return text


def _side(layer: Dict[str, Any], key: str) -> str:
    value = layer.get(key)
    return "" if value in (None, "", "?") else str(value)


def _checked_lines(attrs: Dict[str, Any], labels: Dict[str, str],
                   a_label: str, b_label: str) -> List[str]:
    """What the two ends were compared on - and nothing about what they were not.

    A row reading "unknown" is not a finding. Five of them in a column is how a
    panel teaches people to stop reading it, so a check with no answer is left
    out and the reason is given once, in a sentence.
    """
    decided, a_only, b_only = [], [], []
    for name in sorted(attrs or {}):
        layer = attrs[name] or {}
        verdict = str(layer.get("verdict") or "").lower()
        left, right = _side(layer, "a"), _side(layer, "b")
        detail = str(layer.get("detail") or "")
        owner = {"a": labels.get(layer.get("a_node", ""), ""),
                 "b": labels.get(layer.get("b_node", ""), "")}
        if verdict and verdict != "unknown":
            said = VERDICT_WORDS.get(verdict, verdict)
            if layer.get("compares") == "limit":
                # Not two ends of a cable: a reading against the part's own
                # ceiling. Printing it as "42 / 105 agree" invites the reader
                # to think two sides were compared and found equal.
                units = str(layer.get("units") or "")
                pair = ("%s of %s %s" % (left, right, units)).strip()
                said = "" if verdict == "match" else said
            else:
                pair = ("%s / %s" % (left, right)) if (left and right) else (left or right)
            decided.append(("%-9s %-16s %s" % (name.upper(), pair, said)).rstrip())
            if detail:
                decided.append("%-9s %s" % ("", detail))
            continue
        if not verdict and detail:
            # No verdict to give, but something was established all the same -
            # PTP saying a port most likely faces a cable is the whole answer
            # on a link no switch can see.
            decided.append("%-9s %s" % (name.upper(), detail))
            continue
        if left and not right:
            a_only.append(("%s %s" % (name.upper(), left), owner["a"]))
        elif right and not left:
            b_only.append(("%s %s" % (name.upper(), right), owner["b"]))
    if decided:
        return decided
    half = a_only + b_only
    if half:
        whose = sorted({name for _text, name in half if name})
        said = ("Only %s answered, so nothing could be compared." % whose[0]
                if len(whose) == 1
                else "Nothing could be compared - only one end answered.")
        return [said, "Known from that side: %s."
                % ", ".join(text for text, _name in half)]
    return ["Neither end reported anything to compare on this link."]


def link_facts(edge: Dict[str, Any], labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """The link as a person asks about it: what is plugged into what, then why.

    The two ends are stored sorted by node id, so neither of them is reliably
    the switch or reliably the host. Naming the device beside its own port is
    the only labelling that stays true whichever way round they landed.
    """
    a_label = labels.get(edge.get("a", ""), edge.get("a", "") or "?")
    b_label = labels.get(edge.get("b", ""), edge.get("b", "") or "?")
    width = max(len(a_label), len(b_label), 8)
    middle = edge.get("speed") or ""
    sections = [{"title": "Link", "lines": [
        "%-*s   %s" % (width, a_label, edge.get("a_iface") or "?"),
        "%-*s   %s" % (width, "", middle) if middle else "",
        "%-*s   %s" % (width, b_label, edge.get("b_iface") or "?"),
    ]}]
    sections[0]["lines"] = [line for line in sections[0]["lines"] if line.strip()]

    told = [labels.get(one, one) for one in (edge.get("reported_by") or [])]
    protos = ", ".join(edge.get("protos") or []) or "no protocol named"
    about = ["%-14s %s" % ("How we know", protos
                           + (", from " + " and ".join(told) if told else ""))]
    about.append("%-14s %s" % ("Confidence", _confidence_words(edge)))
    carries = usb_generation(edge)
    if carries:
        about.append("%-14s %s" % ("Carried over", carries))
    warned = usb_note(edge)
    if warned:
        about.append("%-14s %s" % ("", warned))
    if edge.get("vlan"):
        about.append("%-14s %s" % ("VLAN", edge["vlan"]))
    seen = _when(edge.get("last_seen"))
    if seen:
        about.append("%-14s %s" % ("Last seen", seen))
    sections.append({"title": "", "lines": about})

    sections.append({"title": "Checked",
                     "lines": _checked_lines(edge.get("attrs") or {}, labels,
                                             a_label, b_label)})
    return sections


def to_elements(topology: Dict[str, Any]) -> Dict[str, Any]:
    """Topology JSON into the nodes/edges arrays Cytoscape expects."""
    roles = ptp_roles(topology)
    labels = unique_labels(topology.get("nodes", []))
    plans = reach_plans(topology)
    found, nets = port_neighbours(topology)
    from .oui import kind_from_maker, vendor_of
    nodes = []
    for node in topology.get("nodes", []):
        kind = node.get("kind") or "unknown"
        if kind not in KIND_STYLE:
            kind = "unknown"
        maker = vendor_of(node)
        from_maker = ""
        if kind == "unknown":
            from_maker = kind_from_maker(maker.get("vendor", ""))
            kind = from_maker or kind
        colour, shape = KIND_STYLE[kind]
        # The maker's mark where it is known, else the plain kind. Colour and
        # kind stay as the device is; only the drawing changes.
        glyph = glyph_for(node, kind, maker.get("vendor", ""))
        icon_w, icon_h = KIND_SIZE[kind]
        border, width = STATE_BORDER.get(node.get("state") or "unscanned",
                                         STATE_BORDER["unscanned"])
        data = dict(node, label=labels.get(node.get("id", "")) or node_label(node), colour=colour,
                                   facts=device_facts(
                                       node, plans.get(node.get("id", "")),
                                       found.get(node.get("id", "")), nets),
                                   **maker, kindFromMaker=from_maker,
                                   shape=shape, border=border, borderWidth=width,
                                   icon=glyph, tint=tint(colour), iconW=icon_w,
                                   iconH=icon_h,
                                   netboxState=(
                                       node.get("netbox_state")
                                       or (node.get("extra") or {}).get(
                                           "netbox_state") or ""),
                                   ptpRole=(roles.get(node.get("id", ""))
                                            or {}).get("role", ""),
                                   ptpColour=PTP_COLOUR.get(
                                       (roles.get(node.get("id", ""))
                                        or {}).get("role", ""),
                                       PTP_COLOUR[""]),
                                   ptpDomains=", ".join(
                                       str(d) for d in (roles.get(
                                           node.get("id", "")) or {}).get(
                                           "domains", [])),
                                   ptpIcon="clock:" + (
                                       roles.get(node.get("id", ""))
                                       or {}).get("role", ""),
                                   ptpOff=0 if roles.get(node.get("id", ""))
                                   else 1)
        data["search"] = _identity_text(data)
        nodes.append({"data": data})
    edges = []
    for edge in topology.get("edges", []):
        colour, style, width = EDGE_STYLE.get(edge.get("confidence") or "reported",
                                              EDGE_STYLE["reported"])
        usb = usb_generation(edge)
        if usb:
            colour, style, width = USB_STYLE[usb], "solid", 3
        # A USB link has no port worth drawing: the colour already says which
        # generation it is and the legend says what the colour means, so the
        # label only ever read "usb - usb".
        label = "" if usb else "%s - %s" % (drawn_port(edge.get("a_iface")),
                                            drawn_port(edge.get("b_iface")))
        attrs = edge.get("attrs") or {}
        verdicts = [(layer, (data or {}).get("verdict", "unknown"))
                    for layer, data in sorted(attrs.items())]
        bad = [name for name, verdict in verdicts if verdict == "mismatch"]
        warn = [name for name, verdict in verdicts if verdict == "warn"]
        worst = "mismatch" if bad else ("warn" if warn else
                                        ("match" if verdicts else "unknown"))
        edges.append({"data": dict(edge, source=edge.get("a"), target=edge.get("b"),
                                   label=label, colour=colour, lineStyle=style,
                                   width=width, worst=worst,
                                   problems=", ".join(
                                       name.upper() for name in bad + warn),
                                   problemColour=VERDICT_COLOUR[worst],
                                   facts=link_facts(edge, labels),
                                   usb=usb, usbNote=usb_note(edge), ptptree=0, ptponly=_ptp_only(edge),
                                   ptpDomain=_edge_domain(edge, attrs))})
    cabled = {frozenset((e["data"].get("source"), e["data"].get("target")))
              for e in edges if not e["data"].get("ptponly")}
    edges.extend(ptp_tree(roles, labels, cabled))
    return {"nodes": nodes, "edges": edges}


def _usb_attrs(edge: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    protos = {str(p).upper() for p in (edge.get("protos") or []) if p}
    if "USB" not in protos and "ADB" not in protos:
        return None
    return ((edge.get("attrs") or {}).get("usb") or {})


def usb_generation(edge: Dict[str, Any]) -> str:
    """What this link is actually running at, not what it could run at.

    Linux reports both: the rate the two ends settled on and the version the
    device claims. The rate is what decides whether a capture keeps up, so it
    is what the colour follows; the claim is kept for the line below.
    """
    usb = _usb_attrs(edge)
    if usb is None:
        return ""
    try:
        rate = float(usb.get("speed_mbps") or usb.get("speed") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate >= 5000:
        return "USB 3"
    if rate >= 480:
        return "USB 2"
    if rate > 0:
        return "USB 1"
    said = str(usb.get("version") or "").strip()
    if said.startswith("3"):
        return "USB 3"
    if said.startswith("2"):
        return "USB 2"
    return "USB"


def usb_note(edge: Dict[str, Any]) -> str:
    """Said only when a device settled for less than it really can do.

    The rate itself is not worth printing - what a person wants to know is
    which USB this is. bcdUSB 2.00 means the device obeys the USB 2.0
    specification, not that it is capable of high speed: a UART bridge is a
    full-speed device and perfectly healthy. Measured on a real bench, treating
    2.00 as a promise accused working hardware, so only a device that can do
    USB 3 and is connected as USB 2 is worth a word.
    """
    usb = _usb_attrs(edge)
    if not usb:
        return ""
    said = str(usb.get("version") or "").strip()
    try:
        rate = float(usb.get("speed_mbps") or usb.get("speed") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if said.startswith("3") and 0 < rate < 5000:
        return "it can do USB 3 but is connected as USB 2 - the cable or the port"
    return ""


def _ptp_only(edge: Dict[str, Any]) -> int:
    """A link whose only evidence is a PTP frame, which is not a cable.

    Hearing a device's clock proves a shared layer-2 segment and nothing more,
    so such a link belongs to the PTP layer and would be a lie on the physical
    one, where every other line is a cable somebody reported.
    """
    protos = {str(p).upper() for p in (edge.get("protos") or []) if p}
    return 1 if protos == {"PTP"} else 0


def _already_drawn(parent: str, child: str, cabled) -> int:
    """Whether the cabling layer already joins these two devices.

    The clock link would then be a second line between the same pair, so it is
    left to the PTP layer; a port taking time from its own device is never a
    cable at all.
    """
    if parent == child:
        return 1
    return 1 if cabled and frozenset((parent, child)) in cabled else 0


def ptp_tree(roles: Dict[str, Dict[str, Any]],
             labels: Dict[str, str],
             cabled: Optional[Set[Any]] = None) -> List[Dict[str, Any]]:
    """The clock tree as its own links: who takes time from whom.

    Drawn from what each slave port names as its parent, not from the cables,
    because time does not have to follow the shape of the wiring and the
    question on this layer is only where the second came from.
    """
    out = []
    for node_id, seat in sorted(roles.items()):
        for link in seat.get("parents") or []:
            parent = link.get("follows") or ""
            if not parent:
                continue
            through = link.get("through") or "?"
            out_of = link.get("out_of") or ""
            under = link.get("under")
            out.append({"data": {
                "id": "ptp::%s::%s::%s" % (parent, node_id, through),
                "source": parent, "target": node_id,
                "label": ("%s -> %s" % (out_of, through)) if out_of else through,
                "ptptree": 1,
                "dup": _already_drawn(parent, node_id, cabled),
                "ptpColour": PTP_COLOUR.get(seat.get("role") or "",
                                            PTP_COLOUR[""]),
                "ptpDomain": "" if under is None else "domain %s" % under,
                "facts": [{"title": "Where the time comes from",
                           "lines": ["%-14s %s   %s"
                                     % ("out of", labels.get(parent, parent),
                                        out_of or "port not named"),
                                     "%-14s %s   %s"
                                     % ("into", labels.get(node_id, node_id),
                                        through),
                                     "%-14s %s" % ("domain",
                                                   "?" if under is None
                                                   else under)]
                           + (["", "both ports are on this same device - the "
                               "time is not coming from anywhere else"]
                              if parent == node_id else [])}]}})
    return out


def _edge_domain(edge: Dict[str, Any], attrs: Dict[str, Any]) -> str:
    layer = attrs.get("ptp") or {}
    detail = layer.get("a") or ""
    return str(detail) if detail else ""


def summarise(topology: Dict[str, Any]) -> str:
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    scanned = sum(1 for n in nodes if n.get("state") == "scanned")
    confirmed = sum(1 for e in edges if e.get("confidence") == "confirmed")
    return "%d devices (%d scanned), %d links (%d confirmed)" % (
        len(nodes), scanned, len(edges), confirmed)


JS_UNSAFE = (
    ("<", r"\u003c"),
    (">", r"\u003e"),
    ("&", r"\u0026"),
    (chr(0x2028), r"\u2028"),
    (chr(0x2029), r"\u2029"),
)


def embed_json(value: Any) -> str:
    """A device name holding a closing script tag would end the script block."""
    text = json.dumps(value, ensure_ascii=False)
    for raw, escaped in JS_UNSAFE:
        text = text.replace(raw, escaped)
    return text


TOKEN_RE = re.compile(r"__[A-Z_]+__")


def _day_of(stamp) -> str:
    """A timestamp as the day it names, which is the part anybody reads.

    These are written as 2026-10-10T18:08:20. To the second is noise in a
    column answering "has this been here a while"; the day is the answer.
    """
    text = str(stamp or "").strip()
    return text.split("T", 1)[0] if "T" in text else text


# "netbox" is the verdict for the device on that row: whether the inventory
# agrees, differs, has never heard of it, or was never asked. A table of what
# is on the bench is half an answer without it.
# "first seen" has been recorded against every link since the beginning and
# shown nowhere, so "when did this cable appear" was answerable from the
# file and not from the tool. A link that turned up this morning and one
# that has been there for months looked exactly alike.
TABLE_COLUMNS = ("device", "serial", "setup", "address", "kind", "netbox",
                 "local port", "connects to", "its port", "how sure",
                 "seen by", "vlan", "speed", "first seen")


def topology_table(topology: Dict[str, Any]) -> List[List[str]]:
    """The map as one row per link - the shape a spreadsheet can sort.

    The picture answers "how is this connected"; a table answers "how many
    are there and where". A device with no link still gets a row, otherwise
    counting the estate from this file would quietly miss it.
    """
    nodes = {n.get("id"): n for n in topology.get("nodes") or []}

    def about(node_id):
        node = nodes.get(node_id) or {}
        return (node_label(node), str(node.get("serial") or ""),
                ", ".join(node.get("setups") or []),
                ", ".join(node.get("mgmt_ips") or []),
                str(node.get("kind") or ""),
                str(node.get("netbox_state")
                    or (node.get("extra") or {}).get("netbox_state")
                    or "not checked"))

    rows: List[List[str]] = []
    linked = set()
    for edge in topology.get("edges") or []:
        for near, near_port, far, far_port in (
                (edge.get("a"), edge.get("a_iface"), edge.get("b"), edge.get("b_iface")),
                (edge.get("b"), edge.get("b_iface"), edge.get("a"), edge.get("a_iface"))):
            if near not in nodes:
                continue
            linked.add(near)
            name, serial, setup, address, kind, verdict = about(near)
            rows.append([name, serial, setup, address, kind, verdict,
                         str(near_port or ""), about(far)[0] if far in nodes
                         else str(far or ""), str(far_port or ""),
                         str(edge.get("confidence") or ""),
                         ", ".join(edge.get("protos") or []),
                         str(edge.get("vlan") or ""),
                         str(edge.get("speed") or ""),
                         _day_of(edge.get("first_seen"))])
    # A host whose ports announce nothing came back as one blank row saying
    # only "scanned" - which reads as "nothing is plugged into it". The
    # other machines on its network did report a switch, and that is worth
    # putting in the file, as their evidence and not as ours.
    found, nets = port_neighbours(topology)
    for node_id, node in nodes.items():
        name, serial, setup, address, kind, verdict = about(node_id)
        mine = found.get(str(node_id)) or {}
        guessed = []
        for port_name, port in ((node.get("extra") or {}).get("ports") or {}).items():
            if mine.get(port_name):
                continue                      # something already answered it
            for one in (port or {}).get("addresses") or []:
                if str(one).startswith("169.254."):
                    continue
                net = _v4_net(one)
                others = [w for w in (nets.get(net) or []) if w]
                if others:
                    guessed.append([name, serial, setup, address, kind, verdict,
                                    str(port_name), others[0], "?",
                                    "same network", "others on %s" % net,
                                    "", "", _day_of(node.get("first_seen"))])
                    break
        if guessed:
            rows.extend(guessed)
            continue
        if node_id in linked:
            continue
        rows.append([name, serial, setup, address, kind, verdict,
                     "", "", "", str(node.get("state") or ""), "", "", "",
                     _day_of(node.get("first_seen"))])
    rows.sort(key=lambda r: (r[0].lower(), r[5]))
    return rows


def topology_csv(topology: Dict[str, Any]) -> str:
    """The same table, as text a spreadsheet opens."""
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, lineterminator=chr(10))
    writer.writerow(TABLE_COLUMNS)
    writer.writerows(topology_table(topology))
    return buf.getvalue()


def render_html(topology: Dict[str, Any], library: Optional[str] = None,
                template: Optional[str] = None, live: str = "") -> str:
    """`live` is the base path of the window's own server, when it has one.

    With it the page writes a setup the moment it is typed. Without it - which
    is any saved copy of the file - the page is a viewer: it cannot write to
    disk, and pretending otherwise is what produced files nobody wanted.
    """
    page = template if template is not None else _asset("topology.html")
    values = {
        "__LIVE__": embed_json(live or ""),
        "__BYLINE__": html.escape(signature()),
        "__SEED__": html.escape(str(topology.get("seed") or "-")),
        "__GENERATED__": html.escape(str(topology.get("generated_at") or "-")),
        "__SUMMARY__": html.escape(summarise(topology)),
        "__KIND_STYLE__": embed_json(KIND_STYLE),
        "__PTP_STYLE__": embed_json(PTP_COLOUR),
        "__VERDICT_STYLE__": embed_json(VERDICT_COLOUR),
        "__EDGE_STYLE__": embed_json(EDGE_STYLE),
        "__USB_STYLE__": embed_json(USB_STYLE),
        "__ICONS__": embed_json(icon_uris()),
        "__ELEMENTS__": embed_json(to_elements(topology)),
        "__DATA__": embed_json(topology),
        "__CYTOSCAPE__": library if library is not None else _asset("cytoscape.min.js"),
    }
    return TOKEN_RE.sub(lambda m: values.get(m.group(0), m.group(0)), page)


def write_html(topology: Dict[str, Any], path: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_html(topology))
    return path


def render_file(json_path: str, html_path: str) -> str:
    """The published entry point: a topology JSON on disk becomes an HTML page."""
    from .topology import load_topology

    return write_html(load_topology(json_path), html_path)


# What a device IS, as opposed to what it happens to know. A host keeps its
# neighbours' addresses in its ARP cache and the addresses it leases out, so
# searching the whole record for an address answers with every device that has
# ever heard of it. These are the fields that identify the device itself.
IDENTITY_KEYS = ("label", "name", "id", "kind", "chassis_id", "chassis_id_raw",
                 "vendor", "serial", "state")


def _identity_text(data: Dict[str, Any]) -> str:
    """Everything a person might type to mean this one device, and nothing else."""
    bits = [str(data.get(key) or "") for key in IDENTITY_KEYS]
    bits.extend(str(ip) for ip in (data.get("mgmt_ips") or []))
    extra = data.get("extra") or {}

    macs = extra.get("macs") or {}
    bits.extend(list(macs.keys()) + [str(v) for v in macs.values()])

    for name, port in (extra.get("ports") or {}).items():
        bits.append(str(name))
        bits.append(str((port or {}).get("mac") or ""))
        bits.extend(str(a) for a in ((port or {}).get("addresses") or []))

    for name, optic in (extra.get("optics") or {}).items():
        bits.append(str(name))
        bits.extend(str((optic or {}).get(k) or "") for k in ("part", "serial", "vendor"))

    virtual = extra.get("virtual") or {}
    bits.extend(str(virtual.get(k) or "") for k in ("sys_vendor", "product_name"))
    bits.extend(str(b) for b in (extra.get("bridges") or []))

    for space in (extra.get("netns") or {}).values():
        bits.append(str((space or {}).get("name") or ""))
        for name, iface in ((space or {}).get("ifaces") or {}).items():
            bits.append(str(name))
            bits.append(str((iface or {}).get("mac") or ""))
            bits.extend(str(a) for a in ((iface or {}).get("addrs") or []))

    return " ".join(b for b in bits if b).lower()

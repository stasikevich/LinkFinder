"""Who is on the other end of a cable nobody is talking over.

A direct cable between two hosts defeats every other method here. Neither end
is a bridge, so there is no LLDP; no switch sits in the middle, so there is no
forwarding table; and a link with no addresses on it has an empty ARP cache.
Measured on a real pair of servers: six seconds of unfiltered capture on such
a port returned zero packets. The link is not quiet, it is silent.

IPv6 answers it without any of that. Every Ethernet interface already carries
a link-local address derived from its own MAC by EUI-64 - no address plan, no
daemon, nothing to configure. One echo to the all-nodes group makes whoever is
physically attached reply, and the reply decodes straight back to their MAC,
which the rest of the tool can match to a host and a port.

The ladder is the same shape as everywhere else: look at what is already
known, then listen, and only then speak. Speaking costs one packet, and only
on ports where the first two rungs found nothing.

One more measured fact shapes this: asking from ONE end fills in BOTH caches,
because the far end has to answer to be heard. So when both hosts are in the
same run the tool asks on one of them and merely reads the other.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

NEIGH6 = re.compile(
    r"^(?P<addr>[0-9a-fA-F:]+)\s+.*?lladdr\s+(?P<mac>[0-9a-fA-F:]{17})\s+(?P<state>\w+)")

DEAD_STATES = ("FAILED", "INCOMPLETE")

EVIDENCE_LIVE = "cache"
EVIDENCE_ASKED = "asked"

# A port on a management segment can hear dozens of devices - 35 on one lab
# guest. There the count is the finding; the roll call is not.
MAX_NAMED = 4


def _some_of(names):
    """Name a few and count the rest, so one line stays one line."""
    if len(names) <= MAX_NAMED:
        return ", ".join(names)
    return "%s and %d more" % (", ".join(names[:MAX_NAMED]),
                               len(names) - MAX_NAMED)


def is_dark(iface: Dict[str, Any]) -> bool:
    """A port with a cable in it and nothing to show for it.

    Carrier up, no IPv4 of its own, and no neighbour resolved by any earlier
    method. Those are the only ports worth spending a packet on.
    """
    if not iface.get("carrier"):
        return False
    if iface.get("ipv4"):
        return False
    return not iface.get("resolved")


def parse_neigh6(text: str) -> List[Dict[str, str]]:
    """`ip -6 neigh show dev X` into rows, dropping the ones that prove nothing."""
    out: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        found = NEIGH6.match(line.strip())
        if not found:
            continue
        state = found.group("state").upper()
        mac = found.group("mac").lower()
        if state in DEAD_STATES or mac == "00:00:00:00:00:00":
            continue
        out.append({"address": found.group("addr"), "mac": mac, "state": state})
    return out


def read(run: Callable[[str], str], iface: str) -> List[Dict[str, str]]:
    """Rung one: what this port already knows, for free."""
    try:
        return parse_neigh6(run("ip -6 neigh show dev %s" % iface))
    except Exception:
        return []


def ask(run: Callable[[str], str], iface: str) -> None:
    """Rung three: one echo to the all-nodes group, and only here.

    Nothing is read back from the ping itself - the answer lands in the
    kernel's neighbour cache, which is read the same way as rung one.
    """
    try:
        run("ping -6 -c 1 -W 1 -I %s ff02::1 >/dev/null 2>&1 || true" % iface)
    except Exception:
        pass


def discover(run: Callable[[str], str], ifaces: List[str],
             may_ask: bool = True,
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Dict[str, Any]]:
    """Find the far end of each dark port, spending as little as possible.

    Returns {interface: {"peers": [...], "evidence": "cache" or "asked"}}.
    A port already holding an entry is reported as "cache", which says more
    than the MAC alone: something has been talking over that cable.
    """
    say = log or (lambda _m: None)
    found: Dict[str, Dict[str, Any]] = {}
    silent: List[str] = []

    for iface in ifaces:
        known = read(run, iface)
        if known:
            found[iface] = {"peers": known, "evidence": EVIDENCE_LIVE}
        else:
            silent.append(iface)

    if not may_ask or not silent:
        return found

    say("  %d port(s) with a cable and no neighbour - asking once over IPv6"
        % len(silent))
    for iface in silent:
        ask(run, iface)
    try:
        run("sleep 1")
    except Exception:
        pass
    for iface in silent:
        answered = read(run, iface)
        if answered:
            found[iface] = {"peers": answered, "evidence": EVIDENCE_ASKED}
    return found


def _same_port(extra, macs, port: str, peers) -> str:
    """The name of the other side when it is this very port, else "".

    Every answer has to come from a port of this machine that shares the
    hardware with the one that asked - one straggler from somewhere else
    and this is a real loop after all.
    """
    from .netns import one_port
    from .ptp import normalise_mac

    heard = {normalise_mac(p.get("mac", "")) for p in peers or []}
    found = ""
    for name, mac in macs.items():
        if normalise_mac(mac) not in heard:
            continue
        same = one_port(extra, port, name)
        if not same:
            return ""
        # The container almost always reuses the physical port's name, so
        # spelling it out twice reads as two ports again.
        bare = name.partition(" in ")[0]
        found = name if bare == same else "%s (%s)" % (name, same)
    return found


def leases_by_mac(results) -> Dict[str, Dict[str, Any]]:
    """Every DHCP lease seen anywhere in this run, keyed by client MAC.

    A radio leased an address from one host and is cabled to another; the
    run holds both, so the lookup is across the run, not per host.
    """
    from .ptp import normalise_mac

    out: Dict[str, Dict[str, Any]] = {}
    for result in results or []:
        for mac, record in (((getattr(result, "extra", {}) or {}).get("dhcp")
                             or {}).items()):
            key = normalise_mac(mac)
            if key:
                out[key] = record
    return out


def from_lease(mac: str, leased: Dict[str, Dict[str, Any]]) -> str:
    """What to call a MAC that nothing else in the run can name.

    The address it was given is worth far more to a reader than the hex,
    because that is the address they will connect to next, and the maker is
    worth something even when there was no lease at all.
    """
    from .oui import vendor

    record = leased.get(mac) or {}
    name = str(record.get("hostname") or "")
    where = str(record.get("address") or "")
    made = vendor(mac)
    # The maker earns its place only while nothing better is known. Once the
    # device has given its own name, one more clause makes a line nobody
    # finishes reading.
    who = "%s %s" % (made, mac) if made and not name else mac
    if name and where:
        return "%s (%s, leased %s)" % (name, who, where)
    if where:
        return "%s (leased %s)" % (who, where)
    return who if made else ""


def name_peers(results) -> None:
    """Say what each dark port is facing, and how sure that is.

    A single responder on a port is the signature of a cable: nothing else is
    attached to hear the query. Several responders mean a shared segment - a
    splitter, a hub, or a switch that does not speak LLDP. Saying "cable"
    there would be a guess dressed as a fact.

    When the responder is a NIC of another host in the same run, or the
    chassis of a switch already on the map, it gets a name. When it is not -
    a radio unit, say - the MAC is the answer, and that is still the answer to
    "what is this port plugged into".
    """
    from .ptp import known_port_index, normalise_mac

    index = known_port_index(results)
    leased = leases_by_mac(results)
    for result in results or []:
        found = (getattr(result, "extra", {}) or {}).get("neigh6") or {}
        if not found:
            continue
        for nb in getattr(result, "neighbors", []) or []:
            port = found.get(nb.local_iface)
            if not port:
                continue
            peers = port.get("peers") or []
            if not peers:
                continue
            named, seats = [], []
            for peer in peers:
                mac = normalise_mac(peer.get("mac", ""))
                seat = index.get(mac)
                if seat and seat[0]:
                    named.append(("%s %s" % (seat[0], seat[1])).strip())
                    seats.append([seat[0], seat[1]])
                else:
                    named.append(from_lease(mac, leased) or mac)
            alone = len({normalise_mac(p.get("mac", "")) for p in peers}) == 1
            # A port can be cabled back into another port of this same box.
            # It is a real and deliberate arrangement - test rigs and bypass
            # paths do it - but reading "server23" on server23 and working it
            # out yourself is not the tool doing its job.
            extra = getattr(result, "extra", {}) or {}
            macs = extra.get("macs") or {}
            ours = {normalise_mac(m) for m in macs.values()}
            loop_back = alone and ours and all(
                normalise_mac(p.get("mac", "")) in ours for p in peers)
            # Before calling it a loop, check it is not this very port seen
            # from inside a container. A physical function hears its own
            # virtual function through the card, with no cable involved.
            itself = ""
            if loop_back:
                itself = _same_port(extra, macs, nb.local_iface, peers)
            if itself:
                loop_back = False
            evidence = ("internal" if itself else
                        "loopback" if loop_back else
                        "direct" if alone else "segment")
            nb.extra["neigh6_peer"] = _some_of(named)
            nb.extra["neigh6_peer_evidence"] = evidence
            if seats:
                nb.extra["neigh6_peer_ports"] = seats
            if nb.resolved:
                continue
            how = ("it was already in the neighbour cache"
                   if port.get("evidence") == EVIDENCE_LIVE
                   else "it answered an IPv6 neighbour query")
            if itself:
                # The probe may already have said this better, naming the
                # PCI function and what is alive on the far side. Only
                # explain it from scratch when nothing has.
                if not nb.extra.get("netns_explains"):
                    nb.note = ("this is one port, not two: %s is a virtual "
                               "function of it, and the card switches "
                               "between them with no cable involved" % itself)
            elif loop_back:
                nb.note = ("a cable from this host back into itself - the other "
                           "end is %s - %s" % (_some_of(named), how))
            elif alone:
                nb.note = ("no LLDP here; %s is the only thing on this port, so "
                           "most likely a direct cable - %s"
                           % (_some_of(named), how))
            else:
                nb.note = ("no LLDP here; %d devices answer on this port, so it is "
                           "a shared segment, not a cable - %s"
                           % (len(peers), _some_of(named)))


def _mac_forms(mac: str) -> List[str]:
    """A MAC and its U/L-flipped twin, for matching either form a radio uses."""
    if not mac:
        return []
    parts = mac.split(":")
    out = [mac]
    if len(parts) == 6:
        try:
            flipped = parts[:]
            flipped[0] = "%02x" % (int(parts[0], 16) ^ 0x02)
            if flipped != parts:
                out.append(":".join(flipped))
        except ValueError:
            pass
    return out


def _log_facts(info: Dict[str, Any]) -> List[str]:
    """One line of what a host's own log said about a radio - maker, revisions."""
    if not info:
        return []
    tail = []
    if info.get("made_by"):
        tail.append(info["made_by"])
    if info.get("hw_rev"):
        tail.append("HW %s" % info["hw_rev"])
    if info.get("sw_rev"):
        tail.append("SW %s" % info["sw_rev"])
    return ["from this host's log: %s" % ", ".join(tail)] if tail else []


def connect(topology, views, log=None) -> int:
    """Draw the cables this found between devices already on the map.

    The crawl keeps DeviceViews rather than scan results, and the port data
    survives on the device record rather than on its neighbour rows - a port
    with no protocol neighbour has no row to survive on. So the pairing is
    done here from `extra["neigh6"]` directly.
    """
    say = log or (lambda _m: None)
    from .ptp import normalise_mac
    from .oui import kind_from_maker, vendor as oui_vendor

    owners: Dict[str, str] = {}
    ports: Dict[str, Dict[str, Any]] = {}
    cards: Dict[str, Dict[str, Any]] = {}
    leased: Dict[str, Dict[str, Any]] = {}
    told: Dict[str, Dict[str, Any]] = {}
    logged: Dict[str, Dict[str, Any]] = {}  # radio device-info from a host's logs
    told_host: Dict[str, str] = {}          # which scanned host reached each one
    for node_id, view in (views or []):
        if view is None:
            continue
        for mac, record in ((getattr(view, "extra", {}) or {}).get("dhcp")
                            or {}).items():
            leased[normalise_mac(mac)] = record
            told_host.setdefault(normalise_mac(mac), node_id)
        for mac, record in ((getattr(view, "extra", {}) or {})
                            .get("identified") or {}).items():
            told[normalise_mac(mac)] = record
            told_host[normalise_mac(mac)] = node_id
        for mac, record in ((getattr(view, "extra", {}) or {})
                            .get("radio_detail_by_mac") or {}).items():
            logged[normalise_mac(mac)] = record
            told_host.setdefault(normalise_mac(mac), node_id)
        for where in ("macs", "perm_macs"):
            for name, mac in ((getattr(view, "extra", {}) or {}).get(where)
                              or {}).items():
                owners.setdefault(normalise_mac(mac), "%s|%s" % (node_id, name))
        cards[node_id] = getattr(view, "extra", {}) or {}
        found = (getattr(view, "extra", {}) or {}).get("neigh6") or {}
        if found:
            ports[node_id] = found

    for node in topology.nodes.values():
        chassis = normalise_mac(getattr(node, "chassis_id", "") or "")
        if chassis:
            owners.setdefault(chassis, "%s|" % node.id)

    added = draw(topology, ports, owners, cards, leased, told,
                 proto="ND", source="ipv6-neighbour",
                 label="IPv6 neighbours", log=say)

    # Fold what a probe learned about a far end onto its node by MAC, wherever
    # and however the node was born. The draw loop can only reach the device
    # that was the sole answer on a port; a radio sharing its port with a
    # second one was left bare. This reaches every node: a radio gets its
    # NETCONF serial and model, and - from the host's DHCP lease, which does
    # not age out - its address and model even when the M-plane did not answer,
    # so none is left "no management address".
    have = {}
    for node in topology.nodes.values():
        mac = normalise_mac(getattr(node, "chassis_id", "") or "")
        if mac:
            have[mac] = node
        src = told.get(mac) or {}
        lease = leased.get(mac) or {}
        info = logged.get(mac) or {}
        if not src and not lease and not info:
            continue
        if src.get("kind"):
            node.kind = src["kind"]
        model = src.get("model") or info.get("model") or lease.get("hostname")
        if model and not node.vendor:
            node.vendor = model
        # The model is a radio's name on the map too, so every radio reads the
        # same way - its model - not one by model and the next by a bare MAC.
        if model and node.kind == "radio" and not getattr(node, "name", ""):
            node.name = model
        serial = src.get("serial") or info.get("serial")
        if serial and not node.serial:
            node.serial = serial
        addr = src.get("address") or lease.get("address")
        if addr and addr not in (node.mgmt_ips or []):
            node.mgmt_ips.append(addr)
        facts = src.get("facts") or _log_facts(info)
        if facts and not node.extra.get("identified"):
            node.extra["identified"] = dict(src or info, facts=facts)

    # Every radio a scan knows of becomes a node, even when the neighbour cache
    # was too cold for the draw loop to place it this run. A radio is known ways
    # that do not age out: identified over NETCONF, named in the host's own logs
    # by the MAC it answers on, or held in the host's DHCP lease with a MAC that
    # names a radio maker. Either way its identity is already in hand, so the
    # map carries the same radios whichever the fronthaul happened to cache.
    radios = set(m for m, r in told.items() if r.get("kind") == "radio")
    radios |= set(logged)
    radios |= set(m for m in leased
                  if kind_from_maker(oui_vendor(m)) == "radio")

    # Which host port each radio answers on, read off the neighbour cache so
    # the cable to it carries a real interface, not a "?". The fronthaul port
    # is an internal one ("ens1f0np0 in bbu-..."); the far end only wants the
    # physical name, so the pod suffix is dropped. A radio logs its MAC in
    # either U/L form, so both are indexed.
    mac_port: Dict[str, Tuple[str, str]] = {}
    for host_id, portmap in ports.items():
        for iface, port in portmap.items():
            phys = str(iface).split(" in ", 1)[0]
            for peer in port.get("peers") or []:
                for form in _mac_forms(normalise_mac(peer.get("mac", ""))):
                    mac_port.setdefault(form, (host_id, phys))

    for mac in radios:
        if mac in have:
            continue
        record = told.get(mac) or {}
        lease = leased.get(mac) or {}
        info = logged.get(mac) or {}
        model = str(record.get("model") or info.get("model")
                    or lease.get("hostname") or "")
        born = topology.add_node(
            chassis_id=mac, kind="radio", state="unscanned",
            source="ipv6-neighbour", name=model, vendor=model,
            serial=str(record.get("serial") or info.get("serial") or ""),
            mgmt_ip=str(record.get("address") or lease.get("address") or ""))
        facts = record.get("facts") or _log_facts(info)
        if facts:
            born.extra["identified"] = dict(record or info, facts=facts)
        have[mac] = born
        seat = next((mac_port[f] for f in _mac_forms(mac) if f in mac_port), None)
        host = seat[0] if seat else told_host.get(mac)
        iface = seat[1] if seat else ""
        if host and host != born.id:
            topology.add_edge(a=host, a_iface=iface, b=born.id, b_iface="",
                              proto="ND", source="ipv6-neighbour", reported_by=host)
    return added


def draw(topology, ports, owners, cards, leased, told, proto, source, label,
         log=None):
    """Turn per-port peer lists into cables, whatever found the peers.

    Written for IPv6 neighbour discovery and now shared with the IPv4
    neighbour cache: the two differ in where the peers come from, not in
    what makes a peer a cable. One copy means one set of guard rails.
    """
    say = log or (lambda _m: None)
    from .netns import one_port
    from .ptp import normalise_mac
    from .oui import kind_from_maker, vendor as oui_vendor

    added = 0
    for node_id, found in ports.items():
        for iface, port in found.items():
            peers = port.get("peers") or []
            alone = len({normalise_mac(p.get("mac", "")) for p in peers}) == 1
            # A port inside a pod ("ens2f0np0 in bbu-...") reaches its radios
            # over the internal fronthaul on link-local addresses. That fabric
            # is a container's private plumbing, not the site, and every one
            # of those far ends was a floating box the map did not want. A
            # device we actually scanned still joins; only inventing a new
            # internal node from a pod port is dropped.
            internal = " in " in str(iface)
            for peer in peers:
                mac = normalise_mac(peer.get("mac", ""))
                seat = owners.get(mac)
                if not seat and internal:
                    continue
                if not seat and alone and mac:
                    # The far end answered but is nothing we scanned - a radio
                    # unit, an appliance, somebody else's box. Dropping it was
                    # why a fronthaul cable could be found and still be absent
                    # from the map. Only when it is the single answer on the
                    # port: a shared segment with thirty replies must not turn
                    # into thirty devices.
                    # If some host in this run leased it an address, that is
                    # its name and the address to reach it on - far better on
                    # a map than twelve hex digits.
                    record = leased.get(mac) or {}
                    # Anything the device said about itself beats a guess
                    # from its address, so the kind comes from there first.
                    spoke = told.get(mac) or {}
                    guess = spoke.get("kind") or kind_from_maker(oui_vendor(mac))
                    # A node is invented only for a far end a probe actually
                    # learned something about: a radio (identified over NETCONF,
                    # or named a radio by its maker OUI - the one guess worth
                    # keeping, because reading its serial is the whole point) or
                    # a host some scanned machine leased an address to. A bare
                    # MAC from an ARP/ND cache - a container's veth or bridge, or
                    # a box guessed only to be some vendor's server - is a
                    # floating guess the map does not want, so it is dropped.
                    if not spoke and not record and guess != "radio":
                        continue
                    born = topology.add_node(
                        chassis_id=mac, kind=guess or "unknown",
                        state="unscanned", source=source,
                        name=str(record.get("hostname") or ""),
                        # What the radio said about itself over NETCONF - its
                        # own serial and model - makes it a device like any
                        # other, and lets a serial match merge it across BBUs.
                        serial=str(spoke.get("serial") or ""),
                        vendor=str(spoke.get("model") or ""),
                        mgmt_ip=str(record.get("address")
                                    or spoke.get("address") or ""))
                    if spoke.get("facts"):
                        born.extra["identified"] = spoke
                    owners[mac] = seat = "%s|" % born.id
                if not seat:
                    continue
                far, _sep, far_iface = seat.partition("|")
                if far == node_id and far_iface in ("", iface):
                    continue
                # One port that a container also holds is not two ports with
                # a cable between them, and drawing it as a loop sends an
                # engineer hunting for a loop that is not there.
                if far == node_id and one_port(cards.get(node_id) or {},
                                               iface, far_iface):
                    continue
                before = len(topology.edges)
                edge = topology.add_edge(a=node_id, a_iface=iface, b=far,
                                         b_iface=far_iface, proto=proto,
                                         source=source, reported_by=node_id)
                if far == node_id:
                    edge.note("neighbour", evidence="loopback",
                              detail="a cable from this device back into itself")
                else:
                    edge.note("neighbour",
                              evidence="direct" if alone else "segment",
                              detail=("the only device answering on this port"
                                      if alone else
                                      "%d devices answer here, so it is a segment"
                                      % len(peers)))
                if len(topology.edges) > before:
                    added += 1
    if added:
        say("  %s: %d link(s) no protocol reports" % (label, added))
    return added

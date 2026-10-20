"""Cables no switch can report: server to server, found by listening to PTP.

LLDP answers "what is this port plugged into" only when something on the link
speaks it. A direct cable between two hosts has nobody to ask: neither end is
a bridge, and no switch sits in the middle holding the MAC in a forwarding
table. Those links are invisible to every other method here, and a map drawn
without them shows two servers that merely share a switch when in fact they
are cabled to each other.

PTP fills the gap because the hosts are already talking. An event frame is
multicast, so hearing one proves a shared layer-2 segment and no more; only
when two ports hear each other and nothing else is a cable the simplest
explanation. That difference is carried onto the edge rather than flattened.

The port data is read from the device record rather than from its neighbour
rows. A port with PTP but no LLDP is exactly a port with no neighbour to
report, and those rows are dropped on the way into the map - which is why
this used to find nothing at all.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

EVIDENCE_DETAIL = {
    "direct": "most likely a direct cable - each end hears only the other",
    "segment": "same layer-2 segment; a multicast frame does not prove a cable",
    "heard-by": "the far end hears this port's clock, so they share a segment",
}

WORTH_DRAWING = ("direct", "segment", "heard-by")


def own_clock(port: Dict[str, Any]) -> str:
    """portIdentity is "021122.fffe.000098-1"; the clock is before the dash."""
    return (port.get("port_identity") or "").split("-")[0].strip().lower()


class _Port:
    """The parts of a neighbour row that the PTP pairing actually reads.

    It wants somewhere to put its findings and a MAC to look the far end up
    by. The MAC comes out of the clock identity, which is that interface's
    own EUI-64, so the index maps a clock straight back to a host and a port.
    """

    def __init__(self, host: str, iface: str, port: Dict[str, Any]):
        from .ptp import clock_identity_to_mac

        self.host = host
        self.local_iface = iface
        self.local_mac = clock_identity_to_mac(own_clock(port))
        self.chassis_id = ""
        self.switch = ""
        self.resolved = False
        self.note = ""
        self.extra: Dict[str, Any] = {"ptp": port}


class _AsResult:
    """name_peers() reads a scan result; a crawl holds DeviceViews instead."""

    def __init__(self, view: Any):
        self.target = getattr(view, "target", "")
        self.device_name = getattr(view, "name", "") or self.target
        found = (getattr(view, "extra", {}) or {}).get("ptp") or {}
        self.neighbors = [_Port(self.device_name, iface, port)
                          for iface, port in sorted(found.items())
                          if isinstance(port, dict)]


def _index_nodes(topology) -> Dict[str, str]:
    """Every name a PTP peer might be known by, pointing at its node."""
    index: Dict[str, str] = {}
    for node in topology.nodes.values():
        labels = [getattr(node, "name", ""), getattr(node, "chassis_id", "")]
        labels.extend(getattr(node, "mgmt_ips", []) or [])
        for label in labels:
            if label:
                index.setdefault(str(label).strip().lower(), node.id)
    return index


def connect(topology, views: List[Tuple[str, Any]],
            log: Optional[Callable[[str], None]] = None) -> int:
    """Draw an edge for every PTP peering between two devices on this map.

    Returns how many links were added that nothing else could have found.
    """
    say = log or (lambda _m: None)
    pairs = [(node_id, _AsResult(view)) for node_id, view in (views or [])
             if view is not None]
    pairs = [(node_id, shim) for node_id, shim in pairs if shim.neighbors]
    if len(pairs) < 2:
        return 0

    from .ptp import name_peers

    name_peers([shim for _node_id, shim in pairs])
    index = _index_nodes(topology)

    added = 0
    for node_id, shim in pairs:
        for nb in shim.neighbors:
            evidence = nb.extra.get("ptp_peer_evidence") or ""
            if evidence not in WORTH_DRAWING:
                continue
            for seat in nb.extra.get("ptp_peer_ports") or []:
                seat = list(seat) + ["", ""]
                far = index.get(str(seat[0] or "").strip().lower())
                if not far or far == node_id:
                    continue
                before = len(topology.edges)
                edge = topology.add_edge(
                    a=node_id, a_iface=nb.local_iface, b=far, b_iface=seat[1],
                    proto="PTP", source="ptp", reported_by=node_id)
                edge.note("ptp", evidence=evidence,
                          detail=EVIDENCE_DETAIL.get(evidence, ""))
                if len(topology.edges) > before:
                    added += 1

    if added:
        say("  PTP: %d link(s) between devices no switch can report" % added)
    return added


def add_clock_roots(topology, log: Optional[Callable[[str], None]] = None) -> int:
    """A clock nobody scanned is still the root of somebody's tree.

    Every slave names its parent by clock identity, and that identity is a
    MAC. Where the MAC belongs to no device on the map the tree had no root
    at all and the layer showed a row of orphans; here two hosts were each
    taking time from a GNSS appliance that answers nothing we can ask.
    """
    from .ptp import clock_identity_to_mac, normalise_mac
    from .topology import STATE_UNSCANNED

    say = log or (lambda _m: None)
    known = set()
    for node in topology.nodes.values():
        here = [node.chassis_id or ""] + list((node.extra.get("macs") or {}).values())
        known.update(normalise_mac(m or "") for m in here if m)
    known.discard("")

    wanted: Dict[str, Tuple[str, str]] = {}
    for node in list(topology.nodes.values()):
        for iface, port in sorted((node.extra.get("ptp") or {}).items()):
            raw = str((port or {}).get("parent_port") or "")
            if "-" in raw and "." in raw.split("-")[0]:
                raw = raw.rsplit("-", 1)[0]
            mac = normalise_mac(clock_identity_to_mac(raw))
            if mac and mac not in known:
                wanted.setdefault(mac, (node.id, iface))

    for mac, (who, iface) in sorted(wanted.items()):
        topology.add_node(chassis_id=mac, kind="unknown",
                          state=STATE_UNSCANNED, source="ptp")
        say("  the clock %s hands time to %s on %s and was never scanned"
            % (mac, who, iface))
    return len(wanted)

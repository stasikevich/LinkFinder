"""Recursive topology discovery.

Breadth-first from one seed. The seed is depth 0, its neighbours depth 1, and
so on; at max_depth the devices are still interrogated but their neighbours are
no longer queued. Only management addresses that a discovery protocol actually
reported are ever followed - this is not a subnet scanner.

The visited set lives here for the duration of one crawl and has nothing to do
with the persistent switch inventory in store.py.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import handsets, hardware
from .neighbors import DeviceView, collect_device, neighbour_identity
from .linkcheck import stamp_sides
from .netaddr import mgmt_problem
from .topology import (STATE_LIMIT, STATE_OUT_OF_SCOPE, STATE_SCANNED, STATE_UNREACHABLE,
                       STATE_UNSCANNED, Node, Topology, node_key)

Confirm = Callable[[int, List[Tuple[str, str]]], bool]


@dataclass
class Limits:
    max_depth: int = 0
    max_devices: int = 25
    scope: List[str] = field(default_factory=list)

    def networks(self):
        nets = []
        for item in self.scope:
            try:
                nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
            except ValueError:
                continue
        return nets

    def in_scope(self, ip: str) -> bool:
        nets = self.networks()
        if not nets:
            return True
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in net for net in nets)


def always_yes(_depth: int, _candidates: List[Tuple[str, str]]) -> bool:
    return True


class Crawler:
    def __init__(self, opts, limits: Optional[Limits] = None,
                 confirm: Optional[Confirm] = None,
                 log: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 workers: int = 4):
        self.opts = opts
        self.limits = limits or Limits()
        self.confirm = confirm or always_yes
        self.say = log or (lambda _m: None)
        self.progress = progress or (lambda _done, _queued: None)
        self.workers = max(1, workers)
        self.topology = Topology()
        self.views: List[Tuple[str, Any]] = []
        self.visited: Set[str] = set()
        self.adopted: Set[str] = set()
        self.scanned_count = 0
        self.stopped_because = ""
        self.stop_event = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        """Asked to give up: finish the devices in flight, queue nothing more."""
        self.stop_event.set()

    def _already_done(self, ip: str) -> bool:
        if ("ip:%s" % ip) in self.visited:
            return True
        node = self.topology.get(node_key(mgmt_ip=ip))
        if node is None:
            for candidate in self.topology.nodes.values():
                if ip in candidate.mgmt_ips:
                    node = candidate
                    break
        return bool(node and node.state == STATE_SCANNED and node.id in self.visited)

    def _mark_visited(self, ip: str, node: Optional[Node]) -> None:
        self.visited.add("ip:%s" % ip)
        if node is not None:
            self.visited.add(node.id)

    def _record_device(self, view: DeviceView, depth: int) -> Node:
        state = STATE_SCANNED if view.reachable else STATE_UNREACHABLE
        return self.topology.add_node(
            chassis_id=view.chassis_id, mgmt_ip=view.target, name=view.name,
            serial=getattr(view, "serial", "") or "",
            kind=view.kind, vendor=view.vendor, state=state, depth=depth,
            source=",".join(view.sources) or "probe", error=view.error,
            extra=dict(getattr(view, "extra", {}) or {}))

    def _record_peripherals(self, device: Node, view: DeviceView) -> None:
        """Draw the handsets plugged into this host, and only those.

        A handset on a NUC is a real dependency of a real test, so it belongs
        on the map - but the link is USB, not Ethernet. It is given the usb
        layer and no speed, so nothing downstream can mistake it for a cable.
        Everything else on the bus is recorded and not drawn.
        """
        every = (getattr(view, "extra", {}) or {}).get("usb") or {}
        # Only handsets are drawn. A mouse, a keyboard and a BMC's network
        # gadget are plugged in, but they are not the bench: putting them on
        # the map buries the two phones a test actually depends on. The whole
        # list stays in the host's record, so the scan and the JSON still
        # have it - this is about what the picture is for.
        attached = dict((port, entry) for port, entry in every.items()
                        if str(entry.get("kind") or "") == "phone")
        # Four identical handsets on one NUC are normal, and four nodes all
        # reading ONEPLUS A5000 help nobody. Only the repeats carry a serial.
        seen_labels: Dict[str, int] = {}
        for entry in attached.values():
            name = handsets.describe(entry)
            seen_labels[name] = seen_labels.get(name, 0) + 1
        for port, entry in sorted(attached.items()):
            serial = entry.get("adb_serial") or entry.get("serial") or ""
            # A USB port path is NOT an identity. A BMC's virtual keyboard and
            # network gadget carry no serial and sit on the same path on every
            # board of a model, so keying on the path folded three servers'
            # gadgets into one node joined to all three hosts. Without a real
            # serial the thing is only identifiable WITHIN its host.
            if hardware.is_real(serial):
                chassis = "usb:%s" % serial
            else:
                serial = ""
                chassis = "usb:%s:%s" % (device.id, port)
            label = handsets.describe(entry)
            if seen_labels.get(label, 0) > 1:
                label = "%s (%s)" % (label, serial or port)
            far = self.topology.add_node(
                chassis_id=chassis,
                # A handset's adb serial is the serial an inventory holds for
                # it, so it is the node's serial too, not just a USB detail.
                serial=serial,
                name=label,
                kind=entry.get("kind") or "peripheral",
                vendor=entry.get("maker", ""), state=STATE_SCANNED,
                source="usb", depth=(device.depth or 0) + 1,
                extra={"usb": dict(entry)})
            # No port name on the link. A Windows PnP instance id is sixty
            # characters of USB\VID_04E8&PID_6860&MS_COMP_MTP&SAMSUNG_ANDROID
            # and it was drawn straight across the picture. The port is still
            # in the link's record and on the host's panel, where it can be
            # read rather than stepped over.
            self.topology.add_edge(
                a=device.id, a_iface="usb",
                b=far.id, b_iface="usb", proto="usb", source="usb",
                reported_by=device.id,
                attrs={"usb": {k: v for k, v in entry.items()
                               if k in ("version", "speed_mbps", "port", "adb",
                                        "kind", "not_on_bus")}})

    def _record_neighbours(self, device: Node, view: DeviceView,
                           depth: int) -> List[Tuple[str, str]]:
        """Add each far end plus its link; return the ones worth scanning next."""
        candidates: List[Tuple[str, str]] = []
        for nb in view.neighbours:
            chassis, mgmt_ip, name, kind = neighbour_identity(nb)
            if not (chassis or mgmt_ip or name):
                continue
            state = STATE_UNSCANNED
            unusable = mgmt_problem(mgmt_ip) if mgmt_ip else ""
            if mgmt_ip and not self.limits.in_scope(mgmt_ip):
                state = STATE_OUT_OF_SCOPE
            far = self.topology.add_node(
                chassis_id=chassis, mgmt_ip=mgmt_ip, name=name, kind=kind,
                vendor=nb.sys_desc, state=state, source=nb.source or nb.proto,
                depth=depth + 1,
                error="the only address it publishes is %s (%s)" % (mgmt_ip, unusable)
                if unusable else "")
            self.topology.add_edge(
                a=device.id, a_iface=nb.local_iface, b=far.id, b_iface=nb.best_port,
                proto=nb.proto, source=nb.source, reported_by=device.id,
                vlan=nb.vlan, speed=nb.local_speed,
                attrs=stamp_sides((getattr(nb, "extra", {}) or {}).get("checks"),
                                  device.id, far.id))
            if state == STATE_OUT_OF_SCOPE or not mgmt_ip:
                continue
            if unusable:
                continue
            if self._already_done(mgmt_ip) or far.id in self.visited:
                continue
            if any(mgmt_ip == ip for ip, _label in candidates):
                continue
            candidates.append((mgmt_ip, far.label))
        return candidates

    def _look_for_rings(self) -> None:
        """Rings are free arithmetic; the bridges are only asked if one exists."""
        from .loops import blocked_on_rings, cycles, examine

        rings = cycles(self.topology)
        if not rings:
            return
        self.say("  %d ring(s) in the topology - asking those bridges about "
                 "spanning tree" % len(rings))
        blocked = blocked_on_rings(
            self.topology, rings,
            community=getattr(self.opts, "community", "public"), log=self.say)
        self.topology.extra["rings"] = examine(self.topology, blocked,
                                               log=self.say)

    def adopt(self, previous: Topology) -> None:
        """Carry an earlier scan forward so a re-run only fills in the gaps."""
        self.topology.merge_from(previous.to_dict()
                                 if isinstance(previous, Topology) else previous)
        for node in self.topology.nodes.values():
            if node.state == STATE_SCANNED:
                self.visited.add(node.id)
                for ip in node.mgmt_ips:
                    self.visited.add("ip:%s" % ip)

    def adopt_results(self, results) -> List[Tuple[str, str]]:
        """Take devices a scan already interrogated; return the far ends to follow.

        A scan and a crawl ask a device the same questions through the same
        probes, so its answer is reused verbatim rather than fetched twice.
        """
        from .neighbors import view_from_result

        candidates: List[Tuple[str, str]] = []
        for result in results:
            target = getattr(result, "target", "")
            if not target or self._already_done(target):
                continue
            view = view_from_result(target, result)
            self.adopted.add(str(target))
            device = self._record_device(view, 0)
            self.views.append((device.id, view))
            self._mark_visited(target, device)
            self.scanned_count += 1
            self._record_peripherals(device, view)
            for item in self._record_neighbours(device, view, 0):
                if item[0] not in [c[0] for c in candidates]:
                    candidates.append(item)
        if results:
            self.say("reusing the scan of %d device(s) - not asking them again"
                     % self.scanned_count)
        self.progress(self.scanned_count, len(candidates))
        return candidates

    def _unfinished(self) -> List[Tuple[str, int]]:
        """Where the previous scan ran out: nodes known but never interrogated."""
        out: List[Tuple[str, int]] = []
        for node in self.topology.nodes.values():
            if node.state not in (STATE_UNSCANNED, STATE_LIMIT):
                continue
            for ip in node.mgmt_ips:
                if self.limits.in_scope(ip) and not self._already_done(ip):
                    out.append((ip, node.depth if node.depth is not None else 1))
                    break
        return out

    def _probe(self, ip: str, depth: int):
        if self.stop_event.is_set():
            return ip, depth, None
        self.say("[depth %d] %s" % (depth, ip))
        return ip, depth, collect_device(ip, self.opts, log=self.say,
                                         cancel=self.stop_event)

    def run(self, seed, previous=None, known=None) -> Topology:
        """Walk outward from one or several seeds.

        *known* is what a scan already collected: those devices are written
        straight into the map and never asked again, so pressing Generate
        after a scan costs the network nothing for the part already known.
        """
        seeds = [seed] if isinstance(seed, str) else [s for s in (seed or []) if s]
        if not seeds:
            raise ValueError("no seed address to start from")
        self.topology = Topology(seed=seeds[0])
        self.views = []
        for one in seeds[1:]:
            if one not in self.topology.seeds:
                self.topology.seeds.append(one)
        self.visited = set()
        self.adopted = set()
        self.scanned_count = 0
        self.stopped_because = ""
        self.stop_event.clear()
        queue: List[Tuple[str, int]] = [(one, 0) for one in seeds]
        if previous is not None:
            self.adopt(previous)
            self.topology.seed = seeds[0]
            for one in seeds:
                if one not in self.topology.seeds:
                    self.topology.seeds.append(one)
            queue.extend(self._unfinished())
        if known:
            found = self.adopt_results(known)
            if found and self.limits.max_depth >= 1 and self.confirm(1, found):
                queue.extend((ip, 1) for ip, _label in found)

        while queue and not self.stop_event.is_set():
            depth = queue[0][1]
            level = [item for item in queue if item[1] == depth]
            queue = [item for item in queue if item[1] != depth]

            admitted: List[Tuple[str, int]] = []
            for ip, _d in level:
                if self._already_done(ip):
                    continue
                if self.scanned_count + len(admitted) >= self.limits.max_devices:
                    self.stopped_because = ("device limit reached (%d)"
                                            % self.limits.max_devices)
                    self.topology.add_node(mgmt_ip=ip, state=STATE_LIMIT, depth=depth)
                    continue
                admitted.append((ip, depth))
            if not admitted:
                continue

            results = []
            workers = min(self.workers, len(admitted))
            if workers > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(self._probe, ip, d) for ip, d in admitted]
                    for future in concurrent.futures.as_completed(futures):
                        results.append(future.result())
            else:
                results = [self._probe(ip, d) for ip, d in admitted]

            next_candidates: List[Tuple[str, str]] = []
            for ip, got_depth, view in results:
                if view is None:
                    continue
                self.scanned_count += 1
                device = self._record_device(view, got_depth)
                self.views.append((device.id, view))
                self._mark_visited(ip, device)
                if not view.reachable:
                    self.say("  %s unreachable: %s" % (ip, view.error or "no answer"))
                self._record_peripherals(device, view)
                found = self._record_neighbours(device, view, got_depth)
                self.say("  %s: %d neighbour(s), %d worth following"
                         % (ip, len(view.neighbours), len(found)))
                for item in found:
                    if item[0] not in [c[0] for c in next_candidates]:
                        next_candidates.append(item)
            self.progress(self.scanned_count, len(next_candidates))

            if self.stop_event.is_set():
                self.stopped_because = (self.stopped_because
                                    or "you pressed Stop")
                break
            if not next_candidates:
                continue
            if depth + 1 > self.limits.max_depth:
                self.say("depth limit %d reached - %d device(s) left unscanned"
                         % (self.limits.max_depth, len(next_candidates)))
                self.stopped_because = self.stopped_because or (
                    "depth limit reached (%d)" % self.limits.max_depth)
                continue
            if not self.confirm(depth + 1, next_candidates):
                self.say("stopping here at your request - %d device(s) left unscanned"
                         % len(next_candidates))
                self.stopped_because = self.stopped_because or "not confirmed"
                break
            queue.extend((ip, depth + 1) for ip, _label in next_candidates)

        if self.stop_event.is_set():
            self.stopped_because = (self.stopped_because
                                    or "you pressed Stop")
        # Before anything reads the graph: a device a switch named by one of
        # its port MACs is the device we already scanned, not a new box.
        folded = self.topology.fold_known_macs()
        if folded:
            self.say("%d device(s) a neighbour named by a port MAC were the "
                     "same machines we had already scanned" % folded)
        # Every builder, in the one order they are declared in. Keeping that
        # order here as well is how the crawl and a plain host list came to
        # draw different maps from the same evidence.
        from .ladder import walk as walk_evidence
        walk_evidence(self.topology, self.views, log=self.say)
        try:
            self._look_for_rings()
        except Exception as exc:
            self.say("  ring check skipped: %s" % exc)
        self.topology.stopped_because = self.stopped_because
        return self.topology


def crawl(seed, opts, limits: Optional[Limits] = None,
          confirm: Optional[Confirm] = None,
          log: Optional[Callable[[str], None]] = None,
          progress: Optional[Callable[[int, int], None]] = None,
          workers: int = 4, previous=None, known=None) -> Topology:
    crawler = Crawler(opts, limits=limits, confirm=confirm, log=log,
                      progress=progress, workers=workers)
    return crawler.run(seed, previous=previous, known=known)

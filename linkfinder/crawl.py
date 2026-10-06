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
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .neighbors import DeviceView, KIND_UNKNOWN, collect_device, neighbour_identity
from .netaddr import mgmt_problem
from .topology import (STATE_LIMIT, STATE_OUT_OF_SCOPE, STATE_SCANNED, STATE_UNREACHABLE,
                       STATE_UNSCANNED, Node, Topology, node_key)

Confirm = Callable[[int, List[Tuple[str, str]]], bool]


@dataclass
class Limits:
    max_depth: int = 2
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
        self.visited: Set[str] = set()
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
            kind=view.kind, vendor=view.vendor, state=state, depth=depth,
            source=",".join(view.sources) or "probe", error=view.error,
            extra=dict(getattr(view, "extra", {}) or {}))

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
                vlan=nb.vlan, speed=nb.local_speed)
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

    def adopt(self, previous: Topology) -> None:
        """Carry an earlier scan forward so a re-run only fills in the gaps."""
        self.topology.merge_from(previous.to_dict()
                                 if isinstance(previous, Topology) else previous)
        for node in self.topology.nodes.values():
            if node.state == STATE_SCANNED:
                self.visited.add(node.id)
                for ip in node.mgmt_ips:
                    self.visited.add("ip:%s" % ip)

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
        return ip, depth, collect_device(ip, self.opts, log=self.say)

    def run(self, seed: str, previous=None) -> Topology:
        self.topology = Topology(seed=seed)
        self.visited = set()
        self.scanned_count = 0
        self.stopped_because = ""
        self.stop_event.clear()
        queue: List[Tuple[str, int]] = [(seed, 0)]
        if previous is not None:
            self.adopt(previous)
            self.topology.seed = seed
            if seed not in self.topology.seeds:
                self.topology.seeds.append(seed)
            queue.extend(self._unfinished())

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
                self._mark_visited(ip, device)
                if not view.reachable:
                    self.say("  %s unreachable: %s" % (ip, view.error or "no answer"))
                found = self._record_neighbours(device, view, got_depth)
                self.say("  %s: %d neighbour(s), %d worth following"
                         % (ip, len(view.neighbours), len(found)))
                for item in found:
                    if item[0] not in [c[0] for c in next_candidates]:
                        next_candidates.append(item)
            self.progress(self.scanned_count, len(next_candidates))

            if self.stop_event.is_set():
                self.stopped_because = self.stopped_because or "stopped on request"
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
            self.stopped_because = self.stopped_because or "stopped on request"
        self.topology.stopped_because = self.stopped_because
        return self.topology


def crawl(seed: str, opts, limits: Optional[Limits] = None,
          confirm: Optional[Confirm] = None,
          log: Optional[Callable[[str], None]] = None,
          progress: Optional[Callable[[int, int], None]] = None,
          workers: int = 4, previous=None) -> Topology:
    crawler = Crawler(opts, limits=limits, confirm=confirm, log=log,
                      progress=progress, workers=workers)
    return crawler.run(seed, previous=previous)

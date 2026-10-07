"""One uniform answer to "who is this device connected to".

Deliberately thin: every access path already exists and is tested, so this only
normalises what they return into a shape the crawler can put straight into the
graph. Both the host probes and the switch probes already use the same
convention - local_iface is a port on the device we interrogated, and the
switch/port/chassis fields describe the far end - so one reading works for all.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import dataclasses
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .model import Neighbor, TargetResult

KIND_SWITCH = "switch"
KIND_ROUTER = "router"
KIND_HOST = "host"
KIND_BMC = "bmc"
KIND_AP = "ap"
KIND_PHONE = "phone"
KIND_UNKNOWN = "unknown"

NOS_HINTS = (
    (r"\b(ios[- ]xe|ios|nx-os|nexus|catalyst)\b", KIND_SWITCH),
    (r"\b(arista|eos)\b", KIND_SWITCH),
    (r"\b(junos|juniper)\b", KIND_SWITCH),
    (r"\b(routeros|mikrotik)\b", KIND_SWITCH),
    (r"\b(arubaos|procurve|aruba)\b", KIND_SWITCH),
    (r"\b(extremexos|exos)\b", KIND_SWITCH),
    (r"\b(comware|h3c|huawei|vrp)\b", KIND_SWITCH),
    (r"\b(cumulus|sonic|onie|dell emc networking|os10)\b", KIND_SWITCH),
    (r"\b(ilo|idrac|bmc|redfish|supermicro)\b", KIND_BMC),
    (r"\b(linux|ubuntu|suse|centos|rhel|debian|vmkernel|esxi|windows)\b", KIND_HOST),
)


def classify_kind(result_kind: str = "", capabilities: str = "", sys_desc: str = "",
                  proto: str = "") -> str:
    """Capabilities are authoritative when present; otherwise fall back to text."""
    kind = (result_kind or "").lower()
    if kind.startswith("bmc"):
        return KIND_BMC
    if kind.startswith("switch"):
        return KIND_SWITCH
    if kind in ("linux", "suse", "esxi", "windows", "darwin"):
        return KIND_HOST

    caps = (capabilities or "").lower()
    if "wlan" in caps:
        return KIND_AP
    if "telephone" in caps:
        return KIND_PHONE
    if "bridge" in caps:
        return KIND_SWITCH
    if "router" in caps:
        return KIND_ROUTER
    if "station" in caps:
        return KIND_HOST

    text = (sys_desc or "").lower()
    for pattern, guessed in NOS_HINTS:
        if re.search(pattern, text):
            return guessed
    return KIND_UNKNOWN


@dataclass
class DeviceView:
    """What one interrogation told us: who the device is, and what it sees."""

    target: str
    reachable: bool = False
    kind: str = KIND_UNKNOWN
    name: str = ""
    chassis_id: str = ""
    mgmt_ips: List[str] = field(default_factory=list)
    vendor: str = ""
    error: str = ""
    sources: List[str] = field(default_factory=list)
    neighbours: List[Neighbor] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def view_from_result(target: str, result: TargetResult) -> DeviceView:
    usable = [n for n in result.neighbors if n.resolved]
    view = DeviceView(
        target=target,
        reachable=bool(usable) or not result.error,
        kind=classify_kind(result_kind=result.kind, sys_desc=result.device_descr),
        name=result.device_name or "",
        chassis_id=result.device_chassis or "",
        mgmt_ips=[target],
        vendor=result.device_descr or "",
        error=result.error or "",
        sources=[a.method for a in result.attempts if a.ok],
        neighbours=usable,
        extra=dict(getattr(result, "extra", {}) or {}),
    )
    if not usable and result.error:
        view.reachable = False
    return view


def collect_via_snmp(target: str, opts,
                     log: Optional[Callable[[str], None]] = None) -> Optional[DeviceView]:
    """A switch that answers SNMP hands over its whole neighbour table in one go,
    with no login at all - so try it before spending a capture window."""
    from .probe_snmp import Switch

    community = getattr(opts, "community", "public") or "public"
    switch = Switch(target, community)
    try:
        if not switch.identify():
            return None
        peers = switch.lldp_neighbours() + switch.cdp_neighbours()
    except Exception:
        return None
    if not peers:
        return None
    if log:
        log("  SNMP on %s: %s, %d neighbour(s)" % (target, switch.name, len(peers)))
    view = DeviceView(
        target=target, reachable=True, kind=KIND_SWITCH, name=switch.name,
        chassis_id=switch.local_chassis_id(), mgmt_ips=[target], vendor=switch.descr,
        sources=["snmp"], neighbours=peers)
    if getattr(opts, "port_health", False):
        from .port_health import collect_safely
        report = collect_safely(switch, interval=getattr(opts, "port_health_interval", 3.0),
                                expect_speed=getattr(opts, "expect_speed", 0),
                                physical_only=not getattr(opts, "all_interfaces", False),
                                log=log)
        view.extra["port_health"] = report.counts()
        if report.error:
            view.extra["port_health_error"] = report.error
    return view


AUTH_FAILURE = ("rejected the credentials", "authentication", "did not get us in")


def credential_sets(opts, target: str = "") -> List:
    """This host's own login first, then the shared one, then any extras.

    Order matters more than it looks: network gear drops the session after a
    couple of failed attempts, so the set most likely to work has to go first
    rather than being reached after two rejections.
    """
    from .engine import creds_for

    first = creds_for(opts, target) if target else opts.ssh
    sequence = [first]
    seen = {(first.user, first.password, first.key_file)}
    if (opts.ssh.user, opts.ssh.password, opts.ssh.key_file) not in seen:
        sequence.append(opts.ssh)
        seen.add((opts.ssh.user, opts.ssh.password, opts.ssh.key_file))
    for extra in (getattr(opts, "cred_list", None) or []):
        mark = (extra.user, extra.password, extra.key_file)
        if extra and mark not in seen:
            seen.add(mark)
            sequence.append(extra)
    return sequence


def collect_device(target: str, opts, log: Optional[Callable[[str], None]] = None) -> DeviceView:
    """Interrogate one device with the existing probes and normalise the answer.

    A second credential set is only tried when the first was actively rejected:
    network gear drops the session after a couple of failures, so attempts are
    not spent on a device that simply is not answering.
    """
    from .engine import discover_one

    try:
        quick = collect_via_snmp(target, opts, log=log)
    except Exception:
        quick = None
    if quick is not None:
        return quick

    attempts = credential_sets(opts, target)
    view = DeviceView(target=target, reachable=False, mgmt_ips=[target],
                      error="no credentials to try")
    for index, creds in enumerate(attempts):
        trial = dataclasses.replace(opts, ssh=creds) if index else opts
        try:
            result = discover_one(target, trial, log=log)
        except Exception as exc:
            return DeviceView(target=target, reachable=False, mgmt_ips=[target],
                              error="%s: %s" % (type(exc).__name__, exc))
        view = view_from_result(target, result)
        if view.reachable:
            if index and log:
                log("  got in with credential set %d (%s)" % (index + 1, creds.user or "-"))
            return view
        if not any(mark in (view.error or "").lower() for mark in AUTH_FAILURE):
            return view
        if index + 1 < len(attempts) and log:
            log("  credentials rejected, trying set %d" % (index + 2))
    return view


def neighbour_identity(nb: Neighbor):
    """(chassis, mgmt ip, name, kind) for the far end of one reported link."""
    chassis = nb.chassis_id or ""
    name = nb.switch or ""
    if chassis and name and chassis.lower() == name.lower():
        name = ""
    kind = classify_kind(capabilities=nb.capabilities, sys_desc=nb.sys_desc, proto=nb.proto)
    return chassis, nb.mgmt_ip or "", name, kind

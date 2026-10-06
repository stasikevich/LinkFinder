"""Comparing the two ends of a link, and saying so only when both ends spoke.

Discovery answers "what is on the other side". This answers the next question an
engineer asks, which is "and is it configured to match". The checks live here
rather than inside the probes so that each one is a small pure function over
facts already gathered: easy to test without a lab, and impossible for a bad
check to break discovery.

The rule throughout: a verdict needs both sides. One side known and the other
not is reported as UNKNOWN, never as a match and never as a fault. Most of the
cost of a tool like this is paid in false alarms.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"
VERDICT_WARN = "warn"

SUBINTERFACE = re.compile(r"^(?P<parent>.+)\.(?P<vid>\d{1,4})$")


def _int(value: Any) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def tagged_vlans(ifaces: Dict[str, Any]) -> Dict[str, List[int]]:
    """Which 802.1Q ids each physical interface carries, from its subinterfaces.

    `eno1.84` means the host expects to send tagged frames on eno1. That is not
    a verdict on its own - the switch port may well be a trunk - but it is the
    fact you need in front of you when a VLAN does not come up.
    """
    out: Dict[str, List[int]] = {}
    for name, iface in (ifaces or {}).items():
        match = SUBINTERFACE.match(name)
        if not match:
            continue
        parent = match.group("parent")
        vid = int(match.group("vid"))
        lowers = list(getattr(iface, "lowers", []) or [])
        if parent not in (ifaces or {}) and parent not in lowers:
            continue
        out.setdefault(parent, [])
        if vid not in out[parent]:
            out[parent].append(vid)
    for vids in out.values():
        vids.sort()
    return out


def check_mtu(local: Any, remote: Any, remote_is_ceiling: bool = True
              ) -> Dict[str, Any]:
    """Compare MTUs, knowing that the two numbers do not mean the same thing.

    A switch port's MTU is a ceiling - the largest frame it will accept. A
    host's MTU is what it actually sends. So 1500 on a server facing a port set
    to 10240 is not a fault, it is the normal state of almost every network,
    and reporting it would put a warning on nearly every link in the result.
    The fault is the other way round: a host sending frames larger than the
    switch will carry.

    Between two endpoints there is no ceiling, both sides send, and any
    difference is a real mismatch - pass remote_is_ceiling=False for that.
    """
    a, b = _int(local), _int(remote)
    if a is None or b is None:
        return {"a": a, "b": b, "verdict": VERDICT_UNKNOWN,
                "detail": "MTU known on one side only" if (a or b) else
                          "MTU not available on either side"}
    if a == b:
        return {"a": a, "b": b, "verdict": VERDICT_MATCH, "confidence": "high"}
    if remote_is_ceiling and a < b:
        return {"a": a, "b": b, "verdict": VERDICT_MATCH, "confidence": "high",
                "detail": "sending %d, the port accepts up to %d" % (a, b)}
    bigger, smaller = (a, b) if a > b else (b, a)
    return {"a": a, "b": b, "verdict": VERDICT_MISMATCH, "confidence": "high",
            "detail": "one side sends up to %d, the other accepts %d - frames "
                      "over %d are dropped" % (bigger, smaller, smaller),
            "evidence": ["local MTU %d" % a, "remote MTU %d" % b]}


def check_vlan(local: Any, remote: Any, host_tagged: Optional[List[int]] = None
               ) -> Dict[str, Any]:
    """Compare the untagged VLAN each side reports, and note what the host tags.

    Only the untagged id is judged. Whether a port also carries a VLAN as a
    trunk member needs the allowed-VLAN list, which not every device publishes,
    so the tagged ids are recorded as context rather than turned into a verdict
    we cannot stand behind.
    """
    a, b = _int(local), _int(remote)
    out: Dict[str, Any] = {"a": a, "b": b}
    if host_tagged:
        out["host_tagged"] = list(host_tagged)
    if a is None or b is None:
        out["verdict"] = VERDICT_UNKNOWN
        out["detail"] = ("VLAN known on one side only" if (a or b)
                         else "no VLAN reported by either side")
        return out
    if a == b:
        out["verdict"] = VERDICT_MATCH
        out["confidence"] = "high"
        return out
    out["verdict"] = VERDICT_MISMATCH
    out["confidence"] = "high"
    out["detail"] = "untagged VLAN %d here, %d on the other side" % (a, b)
    out["evidence"] = ["local untagged VLAN %d" % a, "remote untagged VLAN %d" % b]
    return out


def check_speed(local: Any, remote: Any) -> Dict[str, Any]:
    """Both ends of a cable negotiate the same rate; if they do not, say so."""
    a = _int(str(local).replace("Mb/s", "").strip() if local else None)
    b = _int(remote)
    if a is None or b is None:
        return {"a": a, "b": b, "verdict": VERDICT_UNKNOWN}
    if a == b:
        return {"a": a, "b": b, "verdict": VERDICT_MATCH, "confidence": "high"}
    return {"a": a, "b": b, "verdict": VERDICT_MISMATCH, "confidence": "medium",
            "detail": "%d Mb/s here, %d Mb/s on the other side" % (a, b),
            "evidence": ["local link speed %d Mb/s" % a,
                         "remote link speed %d Mb/s" % b]}


def check_lacp(local_bond: Any, remote_aggregator: Any) -> Dict[str, Any]:
    """Which bundle each end thinks this cable belongs to.

    Neither side having one is the common case and says nothing, so it is not
    reported at all. One side bundled and the other not is worth seeing: a
    bond member landing on a plain access port is a real and quiet fault.
    """
    a = (local_bond or "").strip()
    b = (remote_aggregator or "").strip()
    if not a and not b:
        return {"verdict": VERDICT_UNKNOWN}
    if a and b:
        return {"a": a, "b": b, "verdict": VERDICT_MATCH, "confidence": "high",
                "detail": "%s on this side, %s on the other" % (a, b)}
    if a:
        return {"a": a, "b": None, "verdict": VERDICT_WARN, "confidence": "medium",
                "detail": "this end is in %s, the switch port is not in any "
                          "aggregation we can see" % a,
                "evidence": ["local bond %s" % a,
                             "no ifStackTable aggregator on the remote port"]}
    return {"a": None, "b": b, "verdict": VERDICT_WARN, "confidence": "medium",
            "detail": "the switch port is in %s, this end is not bonded" % b,
            "evidence": ["remote aggregator %s" % b, "no bond on the local side"]}


def _check_stp(port):
    """Only a port the bridge actually told us about gets a spanning-tree layer."""
    if not port:
        return {"verdict": VERDICT_UNKNOWN}
    from .stp import check as check_stp

    return check_stp(port)


CHECKS = ("mtu", "vlan", "speed", "lacp", "stp")


def compare(neighbor, remote: Optional[Dict[str, Any]],
            host_tagged: Optional[List[int]] = None,
            remote_is_ceiling: bool = True) -> Dict[str, Dict[str, Any]]:
    """Every overlay we can fill in for one link, keyed by layer name.

    `remote` is whatever the far end told us - today a switch port read over
    SNMP. Pass None and every layer comes back UNKNOWN, which is the correct
    answer for a link whose other end we never reached.
    """
    remote = remote or {}
    return {
        "mtu": check_mtu(getattr(neighbor, "local_mtu", ""), remote.get("mtu"),
                         remote_is_ceiling=remote_is_ceiling),
        "vlan": check_vlan(getattr(neighbor, "vlan", ""), remote.get("pvid"),
                           host_tagged),
        "speed": check_speed(getattr(neighbor, "local_speed", ""),
                             remote.get("speed_mbps")),
        "lacp": check_lacp(getattr(neighbor, "local_parent", ""),
                           remote.get("aggregator")),
        "stp": _check_stp(remote.get("stp")),
    }


def problems(attrs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Layer names whose verdict an engineer should look at."""
    return sorted(name for name, layer in (attrs or {}).items()
                  if (layer or {}).get("verdict") in (VERDICT_MISMATCH, VERDICT_WARN))


def summarise(attrs: Dict[str, Dict[str, Any]]) -> str:
    """One line for a table cell: the faults, or how far the checking got."""
    bad = problems(attrs)
    if bad:
        return ", ".join("%s %s" % (name, (attrs[name] or {}).get("verdict"))
                         for name in bad)
    checked = [name for name, layer in (attrs or {}).items()
               if (layer or {}).get("verdict") == VERDICT_MATCH]
    if checked:
        return "%s ok" % "/".join(sorted(checked))
    return ""


def describe(attrs: Dict[str, Dict[str, Any]]) -> List[str]:
    """The long form, for the panel that explains one link."""
    lines: List[str] = []
    for name in sorted(attrs or {}):
        layer = attrs[name] or {}
        verdict = layer.get("verdict", VERDICT_UNKNOWN)
        both = "%s / %s" % (layer.get("a", "?"), layer.get("b", "?"))
        line = "%-6s %-9s %s" % (name.upper(), verdict, both)
        if layer.get("detail"):
            line += "  - %s" % layer["detail"]
        lines.append(line)
    return lines


def remote_ports(switch, names: List[str],
                 log: Optional[Callable[[str], None]] = None) -> Dict[str, Dict[str, Any]]:
    """Facts about named switch ports, asked for by index rather than walked.

    Only the ports a link actually landed on are read, so this stays a handful
    of GETs even on a switch with thousands of interfaces, and can run without
    being opt-in.
    """
    from .port_health import (OID_IF_HIGHSPEED, OID_IF_MTU, OID_IF_OPER, OID_PVID,
                              _as_ints, _column, _walk_text)
    from .snmp import SnmpError

    say = log or (lambda _m: None)
    wanted = {n.strip() for n in names if n and n.strip()}
    if not wanted:
        return {}
    try:
        labels = _walk_text(switch.client, "1.3.6.1.2.1.31.1.1.1.1")
    except SnmpError as exc:
        say("  link checks: %s did not answer SNMP (%s)" % (switch.host, exc))
        return {}
    index_of = {text: index for index, text in labels.items() if text in wanted}
    if not index_of:
        return {}
    indexes = sorted(index_of.values())
    try:
        mtus = _as_ints(_column(switch.client, OID_IF_MTU, indexes))
        speeds = _as_ints(_column(switch.client, OID_IF_HIGHSPEED, indexes))
        opers = _as_ints(_column(switch.client, OID_IF_OPER, indexes))
        pvids = _as_ints(_column(switch.client, OID_PVID, indexes))
    except SnmpError as exc:
        say("  link checks: reading %s failed (%s)" % (switch.host, exc))
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, index in index_of.items():
        out[name] = {"ifindex": index, "mtu": mtus.get(index),
                     "speed_mbps": speeds.get(index), "oper": opers.get(index),
                     "pvid": pvids.get(index)}
    return out


def run_link_checks(results, community: str = "public",
                    log: Optional[Callable[[str], None]] = None) -> None:
    """Fill in the overlays for every link whose far end we can ask about.

    Grouped per switch so one SNMP session covers all the ports that landed on
    it. A switch that will not answer costs one failed identify and is then
    left alone; the links stay in the result with their checks UNKNOWN.
    """
    from .netaddr import is_reachable_mgmt
    from .probe_snmp import build_switches

    say = log or (lambda _m: None)
    by_switch: Dict[str, List[Any]] = {}
    for result in results or []:
        for nb in getattr(result, "neighbors", []) or []:
            address = (getattr(nb, "mgmt_ip", "") or "").strip()
            if address and is_reachable_mgmt(address):
                by_switch.setdefault(address, []).append(nb)

    facts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for address, rows in by_switch.items():
        switch = build_switches([address], community)[0]
        if not switch.identify():
            say("  link checks: %s did not answer SNMP" % address)
            continue
        names = [getattr(nb, "best_port", "") for nb in rows]
        facts[address] = remote_ports(switch, names, log=say)
        try:
            from .stp import read_safely as read_stp
            tree = read_stp(switch, log=say)
            if tree.running:
                for port in facts[address].values():
                    port["stp"] = tree.ports.get(port.get("ifindex"))
        except Exception as exc:
            say("  stp: %s" % exc)
        try:
            from .lag import aggregators
            for port, bundle in aggregators(switch, names, log=say).items():
                facts[address].setdefault(port, {})["aggregator"] = bundle
        except Exception as exc:
            say("  lag: %s" % exc)

    for result in results or []:
        tagged = (getattr(result, "extra", {}) or {}).get("tagged_vlans") or {}
        for nb in getattr(result, "neighbors", []) or []:
            remote = facts.get((getattr(nb, "mgmt_ip", "") or "").strip(), {})
            port = remote.get(getattr(nb, "best_port", ""))
            nb.extra["checks"] = compare(nb, port,
                                         host_tagged=tagged.get(nb.local_iface))

"""The clock tree: who follows whom, per domain.

Per-port PTP facts answer "what is this port doing". They do not answer the
question an engineer actually has, which is "is everything on the same clock,
and where does that clock come from". That needs the ports assembled into
domains and the domains into trees.

The distinction that makes this usable rather than noisy: a server running a
master on a point-to-point segment is the grandmaster of that segment, and in
a fronthaul design that is the whole point, not a fault. So a grandmaster that
is one of our own ports is an island and is reported as such. Two *external*
grandmasters sharing one domain number is a different matter - either two
deliberate timing islands or a misconfiguration - and that is worth saying.

The offset threshold is deliberately loose. G.8275.1 allows roughly 1.5 us end
to end and a healthy boundary clock sits in the tens of nanoseconds, so a
microsecond is far enough out to be worth a look and far enough in not to be
an alarm.

DEGRADED_CLASSES holds the IEEE 1588 clock classes that mean the source is no
longer locked to its reference. 6 and 13 are locked; everything listed there
is not.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DEFAULT_MAX_OFFSET_NS = 1000

DEGRADED_CLASSES = {
    7: "holdover", 14: "holdover (application-specific)",
    52: "holdover, out of spec", 58: "holdover, out of spec",
    140: "degraded", 150: "degraded", 160: "degraded",
    165: "free-running", 187: "out of spec", 193: "out of spec",
    248: "never synchronised", 255: "slave only",
}


@dataclass
class ClockPort:
    """One PTP port somewhere in the run."""

    device: str = ""
    iface: str = ""
    domain: int = -1
    state: str = ""
    role: str = ""
    clock_identity: str = ""
    gm_identity: str = ""
    gm_class: Optional[int] = None
    gm_accuracy: Optional[int] = None
    offset_ns: Optional[float] = None
    path_delay_ns: Optional[float] = None
    steps_removed: Optional[float] = None
    time_traceable: Optional[bool] = None
    parent_port: str = ""
    source: str = ""

    @property
    def where(self) -> str:
        return ("%s %s" % (self.device, self.iface)).strip()


@dataclass
class Domain:
    """Every clock we found carrying one domain number."""

    number: int
    ports: List[ClockPort] = field(default_factory=list)
    followers: Dict[str, List[ClockPort]] = field(default_factory=dict)
    own_clocks: Dict[str, ClockPort] = field(default_factory=dict)
    problems: List[Dict[str, Any]] = field(default_factory=list)

    def external_grandmasters(self) -> List[str]:
        """Grandmasters that are not a port of anything we scanned."""
        return sorted(gm for gm in self.followers if gm not in self.own_clocks)

    def islands(self) -> List[str]:
        """Grandmasters that are our own ports, each feeding its own segment."""
        return sorted(gm for gm in self.followers if gm in self.own_clocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.number,
            "ports": [vars(p) for p in self.ports],
            "external_grandmasters": self.external_grandmasters(),
            "islands": self.islands(),
            "problems": list(self.problems),
        }


def _clock_of(port_identity: str) -> str:
    return (port_identity or "").split("-")[0].strip().lower()


def ports_from(results) -> List[ClockPort]:
    """Flatten every PTP port this run saw into one list."""
    out: List[ClockPort] = []
    for result in results or []:
        device = getattr(result, "device_name", "") or getattr(result, "target", "")
        for iface, raw in ((getattr(result, "extra", {}) or {}).get("ptp")
                           or {}).items():
            out.append(ClockPort(
                device=device, iface=iface,
                domain=raw.get("domain", -1),
                state=raw.get("state", ""), role=raw.get("role", ""),
                clock_identity=_clock_of(raw.get("port_identity", "")),
                gm_identity=(raw.get("gm_identity") or "").lower(),
                gm_class=raw.get("gm_class"), gm_accuracy=raw.get("gm_accuracy"),
                offset_ns=raw.get("offset_ns"),
                path_delay_ns=raw.get("mean_path_delay_ns"),
                steps_removed=raw.get("steps_removed"),
                time_traceable=raw.get("time_traceable"),
                parent_port=raw.get("parent_port", ""),
                source=raw.get("source", "")))
    return out


def build(results, max_offset_ns: int = DEFAULT_MAX_OFFSET_NS) -> Dict[int, Domain]:
    """Group every clock by domain and work out what follows what."""
    domains: Dict[int, Domain] = {}
    for port in ports_from(results):
        if port.domain < 0:
            continue
        domain = domains.setdefault(port.domain, Domain(number=port.domain))
        domain.ports.append(port)
        if port.clock_identity:
            domain.own_clocks[port.clock_identity] = port
    for domain in domains.values():
        for port in domain.ports:
            if port.gm_identity:
                domain.followers.setdefault(port.gm_identity, []).append(port)
        inspect(domain, max_offset_ns=max_offset_ns)
    return domains


def inspect(domain: Domain, max_offset_ns: int = DEFAULT_MAX_OFFSET_NS) -> None:
    """Everything worth saying about one domain, with the evidence for it."""
    external = domain.external_grandmasters()
    if len(external) > 1:
        domain.problems.append({
            "kind": "split domain",
            "verdict": "warn",
            "confidence": "high",
            "detail": "domain %d follows %d different grandmasters that are not "
                      "ours: %s" % (domain.number, len(external),
                                    ", ".join(external)),
            "evidence": ["%s is followed by %s" % (
                gm, ", ".join(p.where for p in domain.followers[gm]))
                for gm in external],
        })

    for port in domain.ports:
        if port.gm_class in DEGRADED_CLASSES and port.state == "SLAVE":
            domain.problems.append({
                "kind": "grandmaster not locked", "where": port.where,
                "verdict": "warn", "confidence": "high",
                "detail": "%s follows a grandmaster in %s (class %s)"
                          % (port.where, DEGRADED_CLASSES[port.gm_class],
                             port.gm_class),
                "evidence": ["gm.ClockClass = %s" % port.gm_class,
                             "grandmaster %s" % port.gm_identity],
            })
        if port.offset_ns is not None and abs(port.offset_ns) > max_offset_ns:
            domain.problems.append({
                "kind": "offset", "where": port.where,
                "verdict": "warn", "confidence": "high",
                "detail": "%s is %+g ns from its master, over the %d ns threshold"
                          % (port.where, port.offset_ns, max_offset_ns),
                "evidence": ["offsetFromMaster = %g" % port.offset_ns],
            })
        if port.time_traceable is False and port.state == "SLAVE":
            domain.problems.append({
                "kind": "not traceable", "where": port.where,
                "verdict": "warn", "confidence": "medium",
                "detail": "%s is synchronised but its time is not traceable to a "
                          "reference" % port.where,
                "evidence": ["timeTraceable = 0"],
            })


def describe_port(port: ClockPort) -> str:
    """One line for a clock inside the tree."""
    bits = [port.state or port.role or "?"]
    if port.offset_ns is not None:
        bits.append("offset %+g ns" % port.offset_ns)
    if port.path_delay_ns is not None:
        bits.append("path delay %g ns" % port.path_delay_ns)
    if port.steps_removed is not None:
        bits.append("%g step(s)" % port.steps_removed)
    if port.time_traceable is False:
        bits.append("not traceable")
    return "%-26s %s" % (port.where, ", ".join(bits))


def render(domains: Dict[int, Domain],
           name_of: Optional[Callable[[str], str]] = None) -> str:
    """The clock tree, one block per domain, problems last."""
    if not domains:
        return ""
    label = name_of or (lambda clock: clock)
    lines: List[str] = []
    for number in sorted(domains):
        domain = domains[number]
        lines.append("PTP, domain %d" % number)
        for gm in domain.external_grandmasters():
            lines.append("  grandmaster %s%s" % (gm, _gm_detail(domain, gm, label)))
            for port in sorted(domain.followers[gm], key=lambda p: p.where):
                lines.append("      " + describe_port(port))
        for gm in domain.islands():
            owner = domain.own_clocks[gm]
            lines.append("  island: %s is the grandmaster of its own segment"
                         % owner.where)
            for port in sorted(domain.followers[gm], key=lambda p: p.where):
                if port is owner:
                    continue
                lines.append("      " + describe_port(port))
        quiet = [p for p in domain.ports if not p.gm_identity]
        for port in sorted(quiet, key=lambda p: p.where):
            lines.append("  %s (no grandmaster reported)" % describe_port(port))
        for problem in domain.problems:
            lines.append("  %s: %s" % (problem["kind"].upper(), problem["detail"]))
            for line in problem.get("evidence", []):
                lines.append("      %s" % line)
        lines.append("")
    return "\n".join(lines).rstrip()


def _gm_detail(domain: Domain, gm: str, label) -> str:
    sample = domain.followers[gm][0]
    bits = []
    named = label(gm)
    if named and named != gm:
        bits.append(named)
    if sample.gm_class is not None:
        bits.append("class %s" % sample.gm_class)
    return "  (%s)" % ", ".join(bits) if bits else ""


def problems(domains: Dict[int, Domain]) -> List[Dict[str, Any]]:
    return [p for domain in domains.values() for p in domain.problems]


def apply(results, domains: Dict[int, Domain]) -> None:
    """Hang each finding on the port it was actually measured at.

    A split domain is a fact about the domain, not a fault on every port in
    it; stamping it on all six ports of a fronthaul host would fill the issues
    column with one piece of news repeated. Port-specific findings - offset,
    a degraded grandmaster, time that is not traceable - do belong on the row.
    Ports with nothing wrong get a clean verdict rather than none, so "checked
    and fine" stays distinguishable from "never looked".
    """
    from .linkcheck import VERDICT_MATCH, VERDICT_WARN

    by_where: Dict[str, List[Dict[str, Any]]] = {}
    for domain in domains.values():
        for problem in domain.problems:
            where = problem.get("where")
            if where:
                by_where.setdefault(where, []).append(problem)

    for result in results or []:
        device = getattr(result, "device_name", "") or getattr(result, "target", "")
        for nb in getattr(result, "neighbors", []) or []:
            port = (nb.extra or {}).get("ptp")
            if not port:
                continue
            where = ("%s %s" % (device, nb.local_iface)).strip()
            found = by_where.get(where)
            if found:
                nb.extra.setdefault("checks", {})["ptp"] = {
                    "verdict": VERDICT_WARN,
                    "confidence": found[0].get("confidence", "medium"),
                    "a": found[0]["kind"],
                    "detail": "; ".join(p["detail"] for p in found),
                    "evidence": [line for p in found
                                 for line in p.get("evidence", [])],
                }
            else:
                nb.extra.setdefault("checks", {})["ptp"] = {
                    "verdict": VERDICT_MATCH, "confidence": "high",
                    "a": port.get("state") or port.get("role") or "running",
                }


def domain_problems(domains: Dict[int, Domain]) -> List[Dict[str, Any]]:
    """Findings that belong to a domain rather than to any one port."""
    return [p for domain in domains.values() for p in domain.problems
            if not p.get("where")]

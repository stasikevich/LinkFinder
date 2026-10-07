"""The clock tree: islands, split domains, and what belongs on a port.
Run: python tests/test_ptp_tree.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor, TargetResult
from linkfinder.ptp_tree import (DEGRADED_CLASSES, apply, build, domain_problems,
                                 problems, render)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-64s %s" % (label, "ok" if got == want else "FAIL"))


FALCON = "000580.fffe.08355b"
OTHER_GM = "008015.fffe.4290d0"


def host(name, ports):
    """ports: {iface: (clock, state, gm, extras...)}"""
    result = TargetResult(target="198.51.100." + name[-1], kind="linux")
    result.device_name = name
    result.extra["ptp"] = {}
    for iface, spec in ports.items():
        record = {"domain": 24, "state": spec.get("state", ""),
                  "role": spec.get("role", ""),
                  "port_identity": spec.get("clock", "") + "-1",
                  "gm_identity": spec.get("gm", ""),
                  "gm_class": spec.get("gm_class", 6),
                  "offset_ns": spec.get("offset", 0),
                  "mean_path_delay_ns": spec.get("delay", 9),
                  "steps_removed": spec.get("steps", 1),
                  "time_traceable": spec.get("traceable", True)}
        result.extra["ptp"][iface] = record
        nb = Neighbor(host=result.target, local_iface=iface)
        nb.extra["ptp"] = record
        result.neighbors.append(nb)
    return result


print("-- one grandmaster, everybody following it --")
simple = [host("node-a", {"f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                 "gm": FALCON}}),
          host("node-b", {"f3": {"clock": "020000.fffe.00000b", "state": "SLAVE",
                                 "gm": FALCON}})]
domains = build(simple)
check("one domain found", sorted(domains), [24])
check("one external grandmaster", domains[24].external_grandmasters(), [FALCON])
check("with both followers", len(domains[24].followers[FALCON]), 2)
check("no islands", domains[24].islands(), [])
check("and nothing to complain about", problems(domains), [])

print()
print("-- a host that is master of its own segment is an island, not a fault --")
island = [host("node-a", {"f0": {"clock": "020000.fffe.000008", "state": "MASTER",
                                 "gm": "020000.fffe.000008", "steps": 0}}),
          host("node-b", {"f3": {"clock": "020000.fffe.00000b", "state": "SLAVE",
                                 "gm": "020000.fffe.000008"}})]
domains = build(island)
check("recognised as an island", domains[24].islands(), ["020000.fffe.000008"])
check("not as an external grandmaster",
      domains[24].external_grandmasters(), [])
check("and not reported as a problem", problems(domains), [])
check("the tree says whose segment it is",
      "is the grandmaster of its own segment" in render(domains), True)

print()
print("-- two external grandmasters under one domain number --")
split = [host("node-a", {"f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                "gm": FALCON}}),
         host("node-c", {"f0": {"clock": "020000.fffe.00000c", "state": "SLAVE",
                                "gm": OTHER_GM}})]
domains = build(split)
found = problems(domains)
check("caught", len(found), 1)
check("named", found[0]["kind"], "split domain")
check("at high confidence", found[0]["confidence"], "high")
check("listing both", sorted([FALCON, OTHER_GM]),
      sorted(domains[24].external_grandmasters()))
check("with who follows which", len(found[0]["evidence"]), 2)
check("and it is a domain finding, not a port one", "where" in found[0], False)

mixed = [host("node-a", {"f0": {"clock": "020000.fffe.000008", "state": "MASTER",
                                "gm": "020000.fffe.000008", "steps": 0},
                         "f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                "gm": FALCON}})]
check("an island alongside one real grandmaster is not a split",
      problems(build(mixed)), [])

print()
print("-- what belongs on a port --")
drifting = [host("node-a", {"f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                   "gm": FALCON, "offset": 4200}})]
domains = build(drifting)
found = problems(domains)
check("an offset past the threshold is caught", found[0]["kind"], "offset")
check("and belongs to its port", found[0]["where"], "node-a f2")
check("a tight offset is not", problems(build(simple)), [])

holdover = [host("node-a", {"f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                   "gm": FALCON, "gm_class": 7}})]
found = problems(build(holdover))
check("a grandmaster in holdover is caught", found[0]["kind"],
      "grandmaster not locked")
check("saying what holdover means", "holdover" in found[0]["detail"], True)
check("every degraded class has a word", 165 in DEGRADED_CLASSES, True)
check("class 6 is locked and says nothing",
      [p for p in problems(build(simple)) if p["kind"] == "grandmaster not locked"],
      [])

adrift = [host("node-a", {"f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                                 "gm": FALCON, "traceable": False}})]
found = problems(build(adrift))
check("time that is not traceable is caught", found[0]["kind"], "not traceable")
check("at medium confidence - it may be deliberate", found[0]["confidence"],
      "medium")
check("a master is not judged on traceability",
      [p for p in problems(build(island)) if p["kind"] == "not traceable"], [])

print()
print("-- the split domain does not flood every port --")
wide = [host("node-a", {"f0": {"clock": "020000.fffe.000008", "state": "MASTER",
                               "gm": "020000.fffe.000008", "steps": 0},
                        "f1": {"clock": "020000.fffe.000009", "state": "MASTER",
                               "gm": "020000.fffe.000009", "steps": 0},
                        "f2": {"clock": "020000.fffe.00000a", "state": "SLAVE",
                               "gm": FALCON}}),
        host("node-c", {"f0": {"clock": "020000.fffe.00000c", "state": "SLAVE",
                               "gm": OTHER_GM}})]
domains = build(wide)
apply(wide, domains)
verdicts = [nb.extra["checks"]["ptp"]["verdict"]
            for result in wide for nb in result.neighbors]
check("every PTP port got a verdict", len(verdicts), 4)
check("and all of them are clean", sorted(set(verdicts)), ["match"])
check("the split is reported once, at domain level",
      len(domain_problems(domains)), 1)

apply(drifting, build(drifting))
row = drifting[0].neighbors[0].extra["checks"]["ptp"]
check("a port-specific fault does reach its row", row["verdict"], "warn")
check("naming what it is", row["a"], "offset")

print()
print("-- rendering --")
text = render(build(split))
check("the domain is a heading", text.startswith("PTP, domain 24"), True)
check("the split is in it", "SPLIT DOMAIN" in text, True)
check("a grandmaster can be named when we know it",
      "sw-core-1" in render(build(split),
                            name_of=lambda c: "sw-core-1" if c == FALCON else ""),
      True)
check("nothing in, nothing out", render({}), "")
check("a run with no PTP at all builds no domains", build([]), {})

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all clock-tree checks passed")

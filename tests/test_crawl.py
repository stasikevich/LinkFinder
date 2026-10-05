"""Crawler behaviour against a fake fabric: loops, limits, scope, failures.
Run: python tests/test_crawl.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import crawl as crawl_mod
from linkfinder.crawl import Crawler, Limits
from linkfinder.model import Neighbor
from linkfinder.neighbors import DeviceView
from linkfinder.topology import (STATE_LIMIT, STATE_OUT_OF_SCOPE, STATE_SCANNED,
                                 STATE_UNREACHABLE, STATE_UNSCANNED)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-52s %s" % (label, "ok" if got == want else "FAIL"))


def nb(local, name, chassis, port, ip="", proto="LLDP", caps="Bridge"):
    return Neighbor(local_iface=local, switch=name, chassis_id=chassis, port=port,
                    mgmt_ip=ip, proto=proto, source="fake", capabilities=caps)


def device(name, chassis, neighbours, reachable=True, error="", extra_ips=()):
    def build(target):
        return DeviceView(target=target, reachable=reachable, kind="switch", name=name,
                          chassis_id=chassis, mgmt_ips=[target] + list(extra_ips),
                          vendor="ExampleNOS", error=error, sources=["fake"],
                          neighbours=list(neighbours))
    return build


def install(fabric):
    def fake_collect(target, _opts, log=None):
        builder = fabric.get(target)
        if builder is None:
            return DeviceView(target=target, reachable=False, mgmt_ips=[target],
                              error="no route to host")
        return builder(target)
    crawl_mod.collect_device = fake_collect


CH = {n: "aa:bb:cc:00:00:%02d" % i for i, n in enumerate("ABCDE", start=1)}


def run(fabric, seed="10.0.0.1", limits=None, confirm=None):
    install(fabric)
    crawler = Crawler(opts=None, limits=limits or Limits(), confirm=confirm)
    return crawler, crawler.run(seed)


print("-- a cycle A -> B -> C -> A terminates --")
fabric = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "B", CH["B"], "p1", "10.0.0.2"),
                                      nb("p2", "C", CH["C"], "p2", "10.0.0.3")]),
    "10.0.0.2": device("B", CH["B"], [nb("p1", "A", CH["A"], "p1", "10.0.0.1"),
                                      nb("p2", "C", CH["C"], "p1", "10.0.0.3")]),
    "10.0.0.3": device("C", CH["C"], [nb("p1", "B", CH["B"], "p2", "10.0.0.2"),
                                      nb("p2", "A", CH["A"], "p2", "10.0.0.1")]),
}
crawler, topo = run(fabric, limits=Limits(max_depth=5))
check("three devices, no loop", len(topo.nodes), 3)
check("each scanned exactly once", crawler.scanned_count, 3)
check("three links, not six", len(topo.edges), 3)
check("every link confirmed from both ends",
      sorted({e.confidence for e in topo.edges.values()}), ["confirmed"])
check("all scanned", sorted({n.state for n in topo.nodes.values()}), [STATE_SCANNED])

print()
print("-- max_depth: seed is 0, depth-N devices are probed, their neighbours are not --")
chain = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "B", CH["B"], "p1", "10.0.0.2")]),
    "10.0.0.2": device("B", CH["B"], [nb("p2", "C", CH["C"], "p1", "10.0.0.3")]),
    "10.0.0.3": device("C", CH["C"], [nb("p2", "D", CH["D"], "p1", "10.0.0.4")]),
    "10.0.0.4": device("D", CH["D"], []),
}
crawler, topo = run(chain, limits=Limits(max_depth=1))
check("depth 1: seed and its neighbour probed", crawler.scanned_count, 2)
states = {n.name: n.state for n in topo.nodes.values()}
check("A scanned", states.get("A"), STATE_SCANNED)
check("B scanned", states.get("B"), STATE_SCANNED)
check("C known but not scanned", states.get("C"), STATE_UNSCANNED)
check("D not discovered at all", "D" in states, False)

crawler, topo = run(chain, limits=Limits(max_depth=2))
check("depth 2 probes three devices", crawler.scanned_count, 3)
check("and leaves D unscanned",
      {n.name: n.state for n in topo.nodes.values()}.get("D"), STATE_UNSCANNED)

print()
print("-- max_devices --")
crawler, topo = run(chain, limits=Limits(max_depth=9, max_devices=2))
check("stops after two", crawler.scanned_count, 2)
check("reason recorded", "device limit" in crawler.stopped_because, True)
check("the one it refused is marked",
      any(n.state == STATE_LIMIT for n in topo.nodes.values()), True)

print()
print("-- scope only limits who we connect to --")
mixed = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "B", CH["B"], "p1", "10.0.0.2"),
                                      nb("p9", "FAR", CH["E"], "p1", "192.0.2.50")]),
    "10.0.0.2": device("B", CH["B"], []),
    "192.0.2.50": device("FAR", CH["E"], [nb("p1", "A", CH["A"], "p9", "10.0.0.1")]),
}
crawler, topo = run(mixed, limits=Limits(max_depth=3, scope=["10.0.0.0/24"]))
by_name = {n.name: n for n in topo.nodes.values()}
check("the far device is still a node", "FAR" in by_name, True)
check("marked out-of-scope", by_name["FAR"].state, STATE_OUT_OF_SCOPE)
check("its link is still drawn",
      any("192.0.2.50" in n.mgmt_ips for n in topo.nodes.values()), True)
check("but it was never probed", crawler.scanned_count, 2)
check("bad scope entries are ignored", Limits(scope=["nonsense"]).in_scope("10.0.0.1"), True)

print()
print("-- a neighbour with no management IP --")
nomgmt = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "printer", "", "eth0", "", caps="Station"),
                                      nb("p2", "B", CH["B"], "p1", "10.0.0.2")]),
    "10.0.0.2": device("B", CH["B"], []),
}
crawler, topo = run(nomgmt, limits=Limits(max_depth=3))
names = {n.name: n for n in topo.nodes.values()}
check("the address-less device is in the graph", "printer" in names, True)
check("left unscanned", names["printer"].state, STATE_UNSCANNED)
check("classified from its capabilities", names["printer"].kind, "host")
check("discovery carried on past it", crawler.scanned_count, 2)

print()
print("-- one device failing does not stop the crawl --")
broken = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "B", CH["B"], "p1", "10.0.0.2"),
                                      nb("p2", "C", CH["C"], "p1", "10.0.0.3")]),
    "10.0.0.2": device("B", CH["B"], [], reachable=False, error="ssh refused"),
    "10.0.0.3": device("C", CH["C"], []),
}
crawler, topo = run(broken, limits=Limits(max_depth=3))
states = {n.name or n.id: n.state for n in topo.nodes.values()}
check("the broken one is marked unreachable",
      [n.state for n in topo.nodes.values() if n.chassis_id == CH["B"]], [STATE_UNREACHABLE])
check("its error is kept",
      [n.error for n in topo.nodes.values() if n.chassis_id == CH["B"]], ["ssh refused"])
check("the healthy sibling was still scanned",
      [n.state for n in topo.nodes.values() if n.chassis_id == CH["C"]], [STATE_SCANNED])
check("three devices attempted", crawler.scanned_count, 3)

print()
print("-- a target that answers nothing at all --")
crawler, topo = run({}, seed="10.0.0.99", limits=Limits())
check("seed still becomes a node", len(topo.nodes), 1)
check("marked unreachable", list(topo.nodes.values())[0].state, STATE_UNREACHABLE)

print()
print("-- confirm mode stops the descent --")
answers = []


def refuse(depth, candidates):
    answers.append((depth, [ip for ip, _ in candidates]))
    return False


crawler, topo = run(chain, limits=Limits(max_depth=9), confirm=refuse)
check("only the seed was scanned", crawler.scanned_count, 1)
check("it asked once, about depth 1", answers, [(1, ["10.0.0.2"])])
check("reason recorded", crawler.stopped_because, "not confirmed")

print()
print("-- the same device reached by a second address is scanned once --")
twin = {
    "10.0.0.1": device("A", CH["A"], [nb("p1", "B", CH["B"], "p1", "10.0.0.2")]),
    "10.0.0.2": device("B", CH["B"], [nb("p2", "B-again", CH["B"], "p9", "10.0.9.2"),
                                      nb("p3", "C", CH["C"], "p1", "10.0.0.3")],
                       extra_ips=("10.0.9.2",)),
    "10.0.9.2": device("B", CH["B"], []),
    "10.0.0.3": device("C", CH["C"], []),
}
crawler, topo = run(twin, limits=Limits(max_depth=4))
check("B counted once despite two addresses",
      len([n for n in topo.nodes.values() if n.chassis_id == CH["B"]]), 1)
check("both of its addresses recorded",
      sorted(n.mgmt_ips for n in topo.nodes.values() if n.chassis_id == CH["B"])[0],
      ["10.0.0.2", "10.0.9.2"])
check("three devices scanned, not four", crawler.scanned_count, 3)

print()
print("-- resuming over an earlier scan does not re-probe what is done --")
install(chain)
first = Crawler(opts=None, limits=Limits(max_depth=0))
earlier = first.run("10.0.0.1")
check("first pass scanned only the seed", first.scanned_count, 1)
check("and recorded the depth limit", "depth limit" in earlier.stopped_because, True)

second = Crawler(opts=None, limits=Limits(max_depth=2))
later = second.run("10.0.0.1", previous=earlier)
check("resume skips the seed", second.scanned_count, 2)
check("graph carried forward", len(later.nodes) >= 4, True)
states = {n.name: n.state for n in later.nodes.values()}
check("seed still marked scanned", states.get("A"), STATE_SCANNED)
check("and B was picked up this time", states.get("B"), STATE_SCANNED)

print()
print("-- stop() ends the walk and says so --")
install(chain)
stopper = Crawler(opts=None, limits=Limits(max_depth=9))


def stop_after_first(_depth, _candidates):
    stopper.stop()
    return True


stopper.confirm = stop_after_first
stopped = stopper.run("10.0.0.1")
check("only the seed was probed", stopper.scanned_count, 1)
check("reason recorded", stopped.stopped_because, "stopped on request")

print()
print("-- a level is probed in parallel --")
install(fabric)
parallel = Crawler(opts=None, limits=Limits(max_depth=3), workers=4)
topo = parallel.run("10.0.0.1")
check("same result as the serial walk", (len(topo.nodes), len(topo.edges)), (3, 3))
check("each device once", parallel.scanned_count, 3)

print()
print("-- the stop reason reaches the topology --")
install(chain)
limited = Crawler(opts=None, limits=Limits(max_depth=9, max_devices=2))
out = limited.run("10.0.0.1")
check("written into the graph", "device limit" in out.stopped_because, True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all crawl checks passed")

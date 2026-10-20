"""Finding the far end of a cable nobody is talking over.
Run: python tests/test_neighbor6.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.neighbor6 import discover, is_dark, parse_neigh6

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-58s %s" % (label, "ok" if got == want else "FAIL"))


SAMPLE = """fe80::aabb:ccff:fe00:0071 lladdr a8:bb:cc:00:00:71 REACHABLE
fe80::dead:beef:0:1 lladdr 00:00:00:00:00:00 STALE
fe80::1 router FAILED
fe80::2 lladdr aa:bb:cc:dd:ee:01 INCOMPLETE
fe80::aabb:ccff:fe00:0072 lladdr a8:bb:cc:00:00:72 STALE
nonsense
"""

print("-- reading what the kernel already knows --")
rows = parse_neigh6(SAMPLE)
check("believable rows only", [r["mac"] for r in rows],
      ["a8:bb:cc:00:00:71", "a8:bb:cc:00:00:72"])
check("an all-zero MAC is not a neighbour",
      any(r["mac"].startswith("00:00") for r in rows), False)
check("FAILED is not evidence", any("FAILED" in r["state"] for r in rows), False)
check("nor is INCOMPLETE", any(r["mac"] == "aa:bb:cc:dd:ee:01" for r in rows), False)
check("the state travels with it", rows[0]["state"], "REACHABLE")
check("rubbish in, nothing out", parse_neigh6("nonsense"), [])
check("empty in, empty out", parse_neigh6(""), [])

print()
print("-- which ports are worth a packet --")
check("a cable with nothing to show for it",
      is_dark({"carrier": True, "ipv4": "", "resolved": False}), True)
check("no cable, no question",
      is_dark({"carrier": False, "ipv4": "", "resolved": False}), False)
check("a port with an address of its own is answerable other ways",
      is_dark({"carrier": True, "ipv4": "198.51.100.10", "resolved": False}), False)
check("and a port LLDP already answered is left alone",
      is_dark({"carrier": True, "ipv4": "", "resolved": True}), False)

print()
print("-- the ladder: look, then listen, then speak --")


class Fake:
    """A host whose dark ports answer only when asked."""

    def __init__(self, cached=None, answers=None):
        self.cached = dict(cached or {})
        self.answers = dict(answers or {})
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if command.startswith("ping"):
            for iface, mac in self.answers.items():
                if " -I %s " % iface in command:
                    self.cached[iface] = ("fe80::1 lladdr %s REACHABLE" % mac)
            return ""
        if command.startswith("ip -6 neigh show dev "):
            return self.cached.get(command.rsplit(" ", 1)[1], "")
        return ""


known = Fake(cached={"eth1": "fe80::9 lladdr aa:bb:cc:00:00:09 REACHABLE"})
got = discover(known, ["eth1"])
check("a port the cache already knows costs no packet",
      [c for c in known.commands if c.startswith("ping")], [])
check("and is marked as a link something is using",
      got["eth1"]["evidence"], "cache")
check("with the neighbour named", got["eth1"]["peers"][0]["mac"],
      "aa:bb:cc:00:00:09")

silent = Fake(answers={"eth2": "aa:bb:cc:00:00:22"})
got = discover(silent, ["eth2"])
check("a silent port is asked exactly once",
      len([c for c in silent.commands if c.startswith("ping")]), 1)
check("the answer is found", got["eth2"]["peers"][0]["mac"], "aa:bb:cc:00:00:22")
check("and marked as only answering when asked",
      got["eth2"]["evidence"], "asked")

mixed = Fake(cached={"eth1": "fe80::9 lladdr aa:bb:cc:00:00:09 REACHABLE"},
             answers={"eth2": "aa:bb:cc:00:00:22"})
discover(mixed, ["eth1", "eth2"])
asked = [c for c in mixed.commands if c.startswith("ping")]
check("only the silent port is spoken to", len(asked), 1)
check("and it is the right one", " -I eth2 " in asked[0], True)

quiet = Fake(answers={"eth2": "aa:bb:cc:00:00:22"})
got = discover(quiet, ["eth2"], may_ask=False)
check("with asking refused nothing is sent",
      [c for c in quiet.commands if c.startswith("ping")], [])
check("and nothing is claimed", got, {})

dead = Fake()
got = discover(dead, ["eth3"])
check("a port with a cable and nobody on it reports nothing", got, {})

print()
print("-- naming the far end once every host is in --")
from linkfinder.model import Neighbor, TargetResult
from linkfinder.neighbor6 import connect, name_peers
from linkfinder.neighbors import DeviceView
from linkfinder.topology import Topology


def host(target, name, iface, mac, heard, evidence="asked"):
    result = TargetResult(target=target, kind="linux", error="", device_name=name)
    nb = Neighbor(local_iface=iface, host=target, source="probe")
    nb.local_mac = mac
    result.neighbors.append(nb)
    result.extra["neigh6"] = {iface: {"evidence": evidence,
                                      "peers": [{"mac": m, "state": "REACHABLE"}
                                                for m in heard]}}
    return result


A, B, C = "a8:bb:cc:00:00:01", "a8:bb:cc:00:00:02", "a8:bb:cc:00:00:03"
pair = [host("198.51.100.1", "node-a", "eth1", A, [B]),
        host("198.51.100.2", "node-b", "eth2", B, [A])]
name_peers(pair)
check("the far end is a device, not a MAC",
      pair[0].neighbors[0].extra["neigh6_peer"], "node-b eth2")
check("one responder reads as a cable",
      pair[0].neighbors[0].extra["neigh6_peer_evidence"], "direct")
check("and the sentence says so",
      "most likely a direct cable" in pair[0].neighbors[0].note, True)

shared = [host("198.51.100.1", "node-a", "eth1", A, [B, C]),
          host("198.51.100.2", "node-b", "eth2", B, [A]),
          host("198.51.100.3", "node-c", "eth3", C, [A])]
name_peers(shared)
check("two responders is a segment, not a cable",
      shared[0].neighbors[0].extra["neigh6_peer_evidence"], "segment")
check("and it is worded as one",
      "shared segment, not a cable" in shared[0].neighbors[0].note, True)

stranger = [host("198.51.100.1", "node-a", "eth1", A, ["de:ad:00:00:00:09"]),
            host("198.51.100.2", "node-b", "eth2", B, [])]
name_peers(stranger)
check("a device nobody else in the run knows is still named by its MAC",
      stranger[0].neighbors[0].extra["neigh6_peer"], "de:ad:00:00:00:09")

cached = [host("198.51.100.1", "node-a", "eth1", A, [B], evidence="cache"),
          host("198.51.100.2", "node-b", "eth2", B, [A], evidence="cache")]
name_peers(cached)
check("a link already in the cache is reported as one that is used",
      "already in the neighbour cache" in cached[0].neighbors[0].note, True)

print()
print("-- and drawn on the map --")


def view(target, name, iface, mac, heard):
    return DeviceView(target=target, reachable=True, kind="host", name=name,
                      mgmt_ips=[target], neighbours=[],
                      extra={"macs": {iface: mac},
                             "neigh6": {iface: {"evidence": "asked",
                                                "peers": [{"mac": m} for m in heard]}}})


topo = Topology(seed="198.51.100.1")
one = topo.add_node(mgmt_ip="198.51.100.1", name="node-a", kind="host", state="scanned")
two = topo.add_node(mgmt_ip="198.51.100.2", name="node-b", kind="host", state="scanned")
drawn = connect(topo, [(one.id, view("198.51.100.1", "node-a", "eth1", A, [B])),
                       (two.id, view("198.51.100.2", "node-b", "eth2", B, [A]))])
check("one edge, not two", (drawn, len(topo.edges)), (1, 1))
edge = list(topo.edges.values())[0]
check("naming both ports", sorted([edge.a_iface, edge.b_iface]), ["eth1", "eth2"])
check("the protocol is recorded", edge.protos, ["ND"])
check("with the evidence on it",
      (edge.attrs.get("neighbour") or {}).get("evidence"), "direct")
check("and it is not a problem", edge.problems, [])

# A bare MAC from a neighbour cache that a probe learned nothing about - no
# scan, no lease, and a maker OUI that names no device kind - is a floating
# guess, and the map no longer invents a node for it. The cable is simply not
# drawn to a box that is only a question mark.
topo = Topology(seed="198.51.100.1")
one = topo.add_node(mgmt_ip="198.51.100.1", name="node-a", kind="host", state="scanned")
check("a cable to a bare, unknown far end invents nothing",
      connect(topo, [(one.id, view("198.51.100.1", "node-a", "eth1", A, [B]))]), 0)
check("and no node is invented for it",
      [n for n in topo.nodes.values() if n.id != one.id], [])

# But a far end whose MAC belongs to a radio maker IS worth placing - reading
# its serial is the whole point - so it is drawn, marked a radio.
RADIO = "2c:a5:39:00:00:0a"      # Parallel Wireless OUI
topo = Topology(seed="198.51.100.1")
one = topo.add_node(mgmt_ip="198.51.100.1", name="node-a", kind="host", state="scanned")
check("a cable to a radio-by-maker end draws it",
      connect(topo, [(one.id, view("198.51.100.1", "node-a", "eth1", A, [RADIO]))]), 1)
far = [n for n in topo.nodes.values() if n.id != one.id]
check("exactly one radio is placed", len(far), 1)
check("and it is marked a radio we never scanned",
      (far[0].kind, far[0].state), ("radio", "unscanned"))
check("identified by the MAC that answered", far[0].chassis_id, RADIO)

# A port inside a pod reaches its radios over the internal fronthaul. That
# fabric is a container's private plumbing, not the site, and each far end
# was a floating box nobody wanted on the map. Reported from the bench:
# "внутренние адреса можно игнорировать, нам нужны только внешние".
pod = Topology(seed="198.51.100.1")
host = pod.add_node(mgmt_ip="198.51.100.1", name="bbu-host", kind="host",
                    state="scanned")
check("a pod port does not invent an internal radio",
      connect(pod, [(host.id, view("198.51.100.1", "bbu-host",
                                   "ens2f0np0 in bbu-6976856959-r56ph", A, [B]))]),
      0)
check("and no floating node was created from it", len(pod.nodes), 1)
# But a device we actually scanned, reached over that same pod port, still
# joins: it is on the site in its own right, the fronthaul merely found it.
known = Topology(seed="198.51.100.1")
h2 = known.add_node(mgmt_ip="198.51.100.1", name="bbu-host", kind="host",
                    state="scanned")
r2 = known.add_node(mgmt_ip="198.51.100.9", name="a-real-ru", kind="radio",
                    state="scanned")
r2.chassis_id = B
drew = connect(known, [(h2.id, view("198.51.100.1", "bbu-host",
                                    "ens2f0np0 in bbu-x", A, [B]))])
check("a scanned device over a pod port is still linked", drew, 1)

# A shared segment answers many times. Which one is the far end is unknown,
# so inventing a device per reply would be inventing a topology.
crowd = Topology(seed="198.51.100.1")
host = crowd.add_node(mgmt_ip="198.51.100.1", name="node-a", kind="host",
                      state="scanned")
connect(crowd, [(host.id, view("198.51.100.1", "node-a", "eth1", A,
                               [B, "a8:bb:cc:00:00:99"]))])
check("a crowd on one port invents nobody", len(crowd.nodes), 1)

# A device that answers nothing but was leased an address is named by it.
named = Topology(seed="198.51.100.1")
owner = named.add_node(mgmt_ip="198.51.100.1", name="node-a", kind="host",
                       state="scanned")
with_lease = view("198.51.100.1", "node-a", "eth1", A, [B])
with_lease.extra["dhcp"] = {B: {"address": "169.254.12.11",
                                "hostname": "radio-one"}}
connect(named, [(owner.id, with_lease)])
new_one = [n for n in named.nodes.values() if n.id != owner.id][0]
check("the lease names it", new_one.name, "radio-one")
check("and gives the address to reach it on", new_one.mgmt_ips,
      ["169.254.12.11"])

print()
print("-- a cable from a host back into itself --")
LOOP_A, LOOP_B = "a8:bb:cc:00:00:41", "a8:bb:cc:00:00:42"
one = TargetResult(target="198.51.100.4", kind="linux", error="", device_name="node-d")
for iface, mac in (("eth1", LOOP_A), ("eth2", LOOP_B)):
    nb = Neighbor(local_iface=iface, host="198.51.100.4", source="probe")
    nb.local_mac = mac
    one.neighbors.append(nb)
one.extra["macs"] = {"eth1": LOOP_A, "eth2": LOOP_B}
one.extra["neigh6"] = {
    "eth1": {"evidence": "asked", "peers": [{"mac": LOOP_B}]},
    "eth2": {"evidence": "asked", "peers": [{"mac": LOOP_A}]}}
name_peers([one])
check("it is called what it is, not just named",
      one.neighbors[0].extra["neigh6_peer_evidence"], "loopback")
check("and said plainly",
      "back into itself" in one.neighbors[0].note, True)
check("the far port is still given",
      "node-d eth2" in one.neighbors[0].note, True)

topo = Topology(seed="198.51.100.4")
solo = topo.add_node(mgmt_ip="198.51.100.4", name="node-d", kind="host",
                     state="scanned")
view_one = DeviceView(target="198.51.100.4", reachable=True, kind="host",
                      name="node-d", mgmt_ips=["198.51.100.4"], neighbours=[],
                      extra={"macs": {"eth1": LOOP_A, "eth2": LOOP_B},
                             "neigh6": {"eth1": {"evidence": "asked",
                                                 "peers": [{"mac": LOOP_B}]}}})
other = topo.add_node(mgmt_ip="198.51.100.5", name="node-e", kind="host",
                      state="scanned")
view_two = DeviceView(target="198.51.100.5", reachable=True, kind="host",
                      name="node-e", mgmt_ips=["198.51.100.5"], neighbours=[],
                      extra={"macs": {}, "neigh6": {}})
drawn = connect(topo, [(solo.id, view_one), (other.id, view_two)])
check("the loop is drawn, not silently dropped", drawn, 1)
edge = list(topo.edges.values())[0]
check("both of its ends are the same device", edge.a == edge.b, True)
check("naming the two ports", sorted([edge.a_iface, edge.b_iface]),
      ["eth1", "eth2"])
check("and marked as a loopback",
      (edge.attrs.get("neighbour") or {}).get("evidence"), "loopback")

from linkfinder.loops import cycles
check("a cable into itself is not reported as a ring", cycles(topo), [])

print()
print("-- hearing your own virtual function is not a loop --")
VF_MAC, PF_MAC = "02:00:5e:00:53:00", "a8:bb:cc:00:00:80"
CARDS = {"netns_vfs": {"0000:c1:01.0": "ens3f0np0"},
         "netns": {"40265": {"inode": "40265", "name": "bbu-pod",
                             "ifaces": {"ens3f0np0": {"mac": VF_MAC,
                                                      "pci": "0000:c1:01.0"}},
                             "neighbours": []}}}

host = TargetResult(target="198.51.100.9", kind="linux", error="",
                    device_name="bbu-host")
nb = Neighbor(local_iface="ens3f0np0", host="198.51.100.9", source="probe")
host.add(nb)
host.extra.update(dict(CARDS, macs={"ens3f0np0": PF_MAC,
                                    "ens3f0np0 in bbu-pod": VF_MAC}))
host.extra["neigh6"] = {"ens3f0np0": {"peers": [{"mac": VF_MAC}],
                                      "evidence": "asked"}}
name_peers([host])
check("it is not called a loop", nb.extra["neigh6_peer_evidence"], "internal")
check("the sentence says it is one port",
      "this is one port, not two" in nb.note, True)
check("and names the port it belongs to", "ens3f0np0" in nb.note, True)
check("no cable is mentioned", "cable involved" in nb.note, True)

# A real cable back into the box must still be reported as one.
twin = TargetResult(target="198.51.100.10", kind="linux", error="",
                    device_name="looped")
other = Neighbor(local_iface="eth1", host="198.51.100.10", source="probe")
twin.add(other)
twin.extra["macs"] = {"eth1": "a8:bb:cc:00:00:90",
                      "eth2": "a8:bb:cc:00:00:91"}
twin.extra["neigh6"] = {"eth1": {"peers": [{"mac": "a8:bb:cc:00:00:91"}],
                                 "evidence": "asked"}}
name_peers([twin])
check("a genuine cable into itself is still a loop",
      other.extra["neigh6_peer_evidence"], "loopback")

print()
print("-- and no edge is drawn for it --")
one_card = Topology(seed="198.51.100.9")
owner = one_card.add_node(mgmt_ip="198.51.100.9", name="bbu-host",
                          kind="host", state="scanned")
held = DeviceView(target="198.51.100.9", reachable=True, kind="host",
                  name="bbu-host", mgmt_ips=["198.51.100.9"], neighbours=[],
                  extra=dict(CARDS,
                             macs={"ens3f0np0": PF_MAC,
                                   "ens3f0np0 in bbu-pod": VF_MAC},
                             neigh6={"ens3f0np0": {
                                 "peers": [{"mac": VF_MAC}],
                                 "evidence": "asked"}}))
check("the card's own plumbing draws nothing",
      connect(one_card, [(owner.id, held)]), 0)
check("and invents no device", len(one_card.nodes), 1)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all neighbour-discovery checks passed")

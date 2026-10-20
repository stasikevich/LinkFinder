"""Checks for drawing cables from the IPv4 neighbour cache.

Two hosts held each other's hardware address, on the right port, and the map
drew nothing. The cache was read, kept and shown - and never once used to say
what was plugged into what.

The guard rail this file exists to defend: a management segment must never
become a bundle of cables. Delete the ceiling and the "twenty on a segment"
check below fails loudly, which is the whole point of having it.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import arplinks
from linkfinder.topology import Topology

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


class View:
    def __init__(self, extra):
        self.extra = extra


def arp(rows):
    return {ip: {"mac": mac, "state": state, "dev": dev}
            for ip, mac, dev, state in rows}


def two_hosts(left_arp, right_arp, left_macs, right_macs):
    topo = Topology(seed="10.0.0.1")
    topo.add_node(mgmt_ip="10.0.0.1", state="scanned", kind="host")
    topo.add_node(mgmt_ip="10.0.0.2", state="scanned", kind="host")
    views = [("ip:10.0.0.1", View({"macs": left_macs, "neighbours": left_arp})),
             ("ip:10.0.0.2", View({"macs": right_macs, "neighbours": right_arp}))]
    return topo, views


def cables(topo):
    return sorted((e.a, e.a_iface, e.b, e.b_iface) for e in topo.edges.values())


print("-- a point-to-point cable both ends can prove --")
# Exactly the measured shape: the bridge on one host holds the far host's
# NIC address, plus a guest of its own that nobody scanned.
topo, views = two_hosts(
    left_arp=arp([("172.40.0.1", "aa:bb:cc:00:00:01", "eth9", "REACHABLE")]),
    right_arp=arp([("172.40.0.40", "aa:bb:cc:00:00:09", "br0", "STALE"),
                   ("172.40.0.254", "52:54:00:11:22:33", "br0", "STALE")]),
    left_macs={"eth9": "aa:bb:cc:00:00:09"},
    right_macs={"eth1": "aa:bb:cc:00:00:01"})
added = arplinks.connect(topo, views)
drawn = cables(topo)
check("a cable is drawn", added >= 1, True)
check("and it names the port at both ends",
      ("ip:10.0.0.1", "eth9", "ip:10.0.0.2", "br0") in drawn
      or ("ip:10.0.0.2", "br0", "ip:10.0.0.1", "eth9") in drawn, True)

print()
print("-- twenty on a segment is not twenty cables --")
crowd = arp([("10.9.9.%d" % n, "aa:bb:cc:00:%02d:ff" % n, "eno1", "STALE")
             for n in range(1, 21)])
topo, views = two_hosts(left_arp=crowd, right_arp={},
                        left_macs={"eno1": "aa:bb:cc:00:00:09"},
                        right_macs={})
check("nothing is drawn from a crowded port",
      arplinks.connect(topo, views), 0)
check("and no device is invented from it", len(topo.nodes), 2)

print()
print("-- a port just under the ceiling still answers --")
few = arp([("10.9.9.%d" % n, "aa:bb:cc:00:%02d:ff" % n, "eno1", "STALE")
           for n in range(1, arplinks.MANY + 1)])
check("four is still read", len(arplinks.peers_worth_drawing(few)), 1)
more = arp([("10.9.9.%d" % n, "aa:bb:cc:00:%02d:ff" % n, "eno1", "STALE")
            for n in range(1, arplinks.MANY + 2)])
check("five is not", len(arplinks.peers_worth_drawing(more)), 0)

print()
print("-- what the cache cannot prove is dropped --")
mixed = arp([("10.0.0.5", "aa:bb:cc:00:00:05", "eth0", "FAILED"),
             ("10.0.0.6", "00:00:00:00:00:00", "eth0", "REACHABLE"),
             ("10.0.0.7", "aa:bb:cc:00:00:09", "eth0", "REACHABLE")])
left = arplinks.peers_worth_drawing(mixed, own={"aa:bb:cc:00:00:09"})
check("a failed lookup proves nothing", "eth0" not in left, True)

print()
print("-- a made-up address never names a device --")
# 52:54:.. is locally administered: two unrelated guests can carry the same
# one, so it must not become an identity. The address it answers on can.
topo, views = two_hosts(
    left_arp=arp([("172.40.0.254", "52:54:00:11:22:33", "eth9", "STALE")]),
    right_arp={}, left_macs={"eth9": "aa:bb:cc:00:00:09"}, right_macs={})
arplinks.connect(topo, views)
born = [n for n in topo.nodes.values() if n.state == "unscanned"]
check("the device is still put on the map", len(born), 1)
check("but not under the made-up address", born[0].chassis_id, "")
check("it is keyed by the address it answers on",
      born[0].mgmt_ips, ["172.40.0.254"])

print()
print("-- a real maker's address may name one --")
topo, views = two_hosts(
    left_arp=arp([("172.60.0.254", "00:00:5e:00:53:c4", "eth9", "STALE")]),
    right_arp={}, left_macs={"eth9": "aa:bb:cc:00:00:09"}, right_macs={})
arplinks.connect(topo, views)
born = [n for n in topo.nodes.values() if n.state == "unscanned"]
check("one device appears", len(born), 1)
check("named by its hardware address", born[0].chassis_id, "00:00:5e:00:53:c4")
check("and carrying the address to reach it on",
      born[0].mgmt_ips, ["172.60.0.254"])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all neighbour-cache checks passed")

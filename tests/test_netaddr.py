"""Which addresses we can actually connect to, and how they are printed.
Run: python tests/test_netaddr.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.netaddr import (compress_ipv6, is_ip, is_mac, is_reachable_mgmt,
                                mgmt_problem, normalise_ip, pick_mgmt_ip)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


print("-- telling an address from something that only looks like one --")
check("plain IPv4", is_ip("198.51.100.20"), True)
check("IPv6", is_ip("fe80::7a9a:18ff:fe19:1234"), True)
check("a MAC is not an address", is_ip("38:10:f0:21:54:80"), False)
check("and is recognised as a MAC", is_mac("38-10-F0-21-54-80"), True)
check("an octet over 255 is not an address", is_ip("198.51.100.300"), False)
check("three octets are not an address", is_ip("10.166.74"), False)
check("a hostname is not an address", is_ip("sw-access-1"), False)
check("empty is not an address", is_ip(""), False)
check("two :: groups are not an address", is_ip("fe80::1::2"), False)

print()
print("-- printing IPv6 the way people recognise it --")
check("the form LLDP hands us is compressed",
      compress_ipv6("fe80:0000:0000:0000:7a9a:18ff:fe19:1234"),
      "fe80::7a9a:18ff:fe19:1234")
check("the longest run of zeroes wins",
      compress_ipv6("2001:0db8:0000:0000:0001:0000:0000:0002"),
      "2001:db8::1:0:0:2")
check("a single zero group is not worth a ::",
      compress_ipv6("2001:0db8:0000:0001:0001:0001:0001:0001"),
      "2001:db8:0:1:1:1:1:1")
check("all zeroes", compress_ipv6("0000:0000:0000:0000:0000:0000:0000:0000"), "::")
check("loopback", compress_ipv6("0000:0000:0000:0000:0000:0000:0000:0001"), "::1")
check("a zone index survives", compress_ipv6("fe80:0000:0000:0000:0000:0000:0000:0001%eth0"),
      "fe80::1%eth0")
check("an already short address is left alone", compress_ipv6("fe80::1"), "fe80::1")
check("IPv4 is not touched", normalise_ip("10.0.0.1"), "10.0.0.1")
check("a hostname is not touched", normalise_ip("sw-1"), "sw-1")

print()
print("-- an address can be valid and still useless to us --")
check("a routable v4 is fine", mgmt_problem("198.51.100.1"), "")
check("a routable v6 is fine", mgmt_problem("2001:db8::1"), "")
check("v6 link-local needs a zone and is refused",
      "link-local" in mgmt_problem("fe80::7a9a:18ff:fe19:1234"), True)
check("the uncompressed form is refused just the same",
      "link-local" in mgmt_problem("fe80:0000:0000:0000:7a9a:18ff:fe19:1234"), True)
check("with a zone index it becomes usable",
      mgmt_problem("fe80::1%eth0"), "")
check("v4 link-local is refused too",
      "link-local" in mgmt_problem("169.254.3.4"), True)
check("loopback is refused", "loopback" in mgmt_problem("127.0.0.1"), True)
check("v6 loopback is refused", "loopback" in mgmt_problem("::1"), True)
check("the unspecified address is refused",
      "unspecified" in mgmt_problem("0.0.0.0"), True)
check("v6 unspecified is refused", "unspecified" in mgmt_problem("::"), True)
check("multicast is refused", "multicast" in mgmt_problem("239.1.2.3"), True)
check("v6 multicast is refused", "multicast" in mgmt_problem("ff02::1"), True)
check("a MAC in the management field is refused",
      "not an IP" in mgmt_problem("38:10:f0:21:54:80"), True)
check("the shorthand agrees", is_reachable_mgmt("10.0.0.1"), True)
check("and for the bad case", is_reachable_mgmt("fe80::1"), False)

print()
print("-- picking one address out of what a device published --")
check("a usable address wins over a link-local, whatever the order",
      pick_mgmt_ip(["fe80::1", "10.0.0.5"]), ("10.0.0.5", ""))
check("order does not matter the other way either",
      pick_mgmt_ip(["10.0.0.5", "fe80::1"]), ("10.0.0.5", ""))
address, why = pick_mgmt_ip(["fe80:0000:0000:0000:7a9a:18ff:fe19:1234"])
check("a device with only a link-local keeps it as identity", address,
      "fe80::7a9a:18ff:fe19:1234")
check("and the reason travels with it", "link-local" in why, True)
check("nothing at all is reported as such", pick_mgmt_ip([])[0], "")
check("duplicates collapse", pick_mgmt_ip(["10.0.0.5", "10.0.0.5"]), ("10.0.0.5", ""))
check("blank entries are ignored", pick_mgmt_ip(["", None, "10.0.0.5"]),
      ("10.0.0.5", ""))

print()
print("-- the crawler must not walk into an address it cannot reach --")
from linkfinder.crawl import Crawler, Limits
from linkfinder.model import Neighbor
from linkfinder.neighbors import DeviceView
from linkfinder.topology import STATE_UNSCANNED


def view_with(neighbour):
    view = DeviceView(target="10.0.0.1", reachable=True, name="seed", kind="host",
                      chassis_id="aa:bb:cc:dd:ee:01")
    view.neighbours = [neighbour]
    return view


crawler = Crawler(opts=None, limits=Limits())
seed = crawler.topology.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1",
                                 name="seed")
bad = Neighbor(host="10.0.0.1", local_iface="eth0", switch="CRS326",
               chassis_id="aa:bb:cc:dd:ee:02",
               mgmt_ip="fe80:0000:0000:0000:7a9a:18ff:fe19:1234", proto="LLDP")
queued = crawler._record_neighbours(seed, view_with(bad), depth=0)
check("a link-local neighbour is never queued for probing", queued, [])
far = [n for n in crawler.topology.nodes.values() if n.name == "CRS326"][0]
check("but it is still a node on the map", far.state, STATE_UNSCANNED)
check("and it says why it will not be scanned", "link-local" in far.error, True)
check("the link to it is still drawn", len(crawler.topology.edges) >= 1, True)

good = Neighbor(host="10.0.0.1", local_iface="eth1", switch="sw-2",
                chassis_id="aa:bb:cc:dd:ee:03", mgmt_ip="10.0.0.9", proto="LLDP")
queued = crawler._record_neighbours(seed, view_with(good), depth=0)
check("a reachable neighbour is still queued", queued, [("10.0.0.9", "sw-2")])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all address checks passed")

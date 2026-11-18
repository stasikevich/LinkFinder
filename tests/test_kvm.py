"""A KVM host names its own guests; the map should use that, not a bare MAC.

A guest seen only in a neighbour cache is a hardware address and maybe a
link-local one - which reads as a fault. libvirt on the host holds the real
name and the routable address, so these are parsed from virsh and the guest is
drawn as a named machine with its IPv4 (never a lone fe80).

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import kvm
from linkfinder.topology import Topology

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


IFLIST = """ Interface   Type     Source   Model    MAC
-----------------------------------------------------------
 macvtap1    direct   eno1     virtio   52:54:00:9a:88:8b
 vnet1       bridge   iperf2   virtio   52:54:00:48:6e:ad
"""

IFADDR = """ Name       MAC address          Protocol     Address
-------------------------------------------------------------------------------
 vnet1      52:54:00:48:6e:ad    ipv4         172.50.0.254/0
"""


def test_parse_iflist():
    rows = kvm.parse_iflist(IFLIST)
    check("two interfaces", len(rows), 2)
    check("bridge source read", [r["source"] for r in rows if r["type"] == "bridge"], ["iperf2"])
    check("header and rule skipped", all(r["mac"].count(":") == 5 for r in rows), True)


def test_parse_ifaddr():
    check("mac to v4", kvm.parse_ifaddr(IFADDR), {"52:54:00:48:6e:ad": "172.50.0.254"})


def test_read_guests():
    def run(cmd):
        if "list --name" in cmd:
            return "iperf-4G\n"
        if "domiflist" in cmd:
            return IFLIST
        if "domifaddr" in cmd and "arp" in cmd:
            return IFADDR
        return ""
    guests = kvm.read_guests(run)
    check("one guest", len(guests), 1)
    check("guest name", guests[0]["name"], "iperf-4G")
    vnet = [i for i in guests[0]["interfaces"] if i["type"] == "bridge"][0]
    check("v4 attached to the bridge iface", vnet.get("address"), "172.50.0.254")


class View:
    def __init__(self, extra):
        self.extra = extra


def test_connect_names_the_guest():
    t = Topology(seed="10.0.0.1")
    t.add_node(mgmt_ip="10.0.0.1", state="scanned", kind="host")
    domains = [{"name": "iperf-4G", "interfaces": [
        {"iface": "macvtap1", "type": "direct", "source": "eno1", "mac": "52:54:00:9a:88:8b"},
        {"iface": "vnet1", "type": "bridge", "source": "iperf2",
         "mac": "52:54:00:48:6e:ad", "address": "172.50.0.254"}]}]
    kvm.connect(t, [("ip:10.0.0.1", View({"kvm_domains": domains}))])
    vms = [n for n in t.nodes.values() if n.kind == "vm"]
    check("one vm node drawn", len(vms), 1)
    check("named from libvirt", vms[0].name, "iperf-4G")
    check("keyed on the bridge mac", vms[0].chassis_id, "52:54:00:48:6e:ad")
    check("given its v4", vms[0].mgmt_ips, ["172.50.0.254"])


def test_connect_v4_only_no_fe80():
    t = Topology(seed="10.0.0.1")
    t.add_node(mgmt_ip="10.0.0.1", state="scanned", kind="host")
    # a guest libvirt cannot resolve a v4 for: named, but no link-local shown
    domains = [{"name": "iperf-5G", "interfaces": [
        {"iface": "vnet0", "type": "bridge", "source": "iperf1", "mac": "52:54:00:0b:f8:70"}]}]
    kvm.connect(t, [("ip:10.0.0.1", View({"kvm_domains": domains}))])
    vm = [n for n in t.nodes.values() if n.kind == "vm"][0]
    check("still named", vm.name, "iperf-5G")
    check("no fe80 forced on as an address", vm.mgmt_ips, [])


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("kvm: all checks passed")

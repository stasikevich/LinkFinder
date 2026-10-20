"""Ports hidden inside a container, and the dark port that explains itself.
Run: python tests/test_netns.py

The samples are real `ip` output shapes, with the site's own addresses and
MACs replaced by documentation ones.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.netns import (MAX_SPACES, add_detail, discover, explain_dark,
                              parse, parse_ip_link, parse_pfs, parse_vfs)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


PHYS = (r"32: ens3f0np0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq "
        r"state UP mode DEFAULT group default qlen 1000\    link/ether "
        r"00:00:5e:00:53:00 brd ff:ff:ff:ff:ff:ff promiscuity 0  allmulti 0 "
        r"minmtu 68 maxmtu 9702 addrgenmode eui64 numtxqueues 64 "
        r"parentbus pci parentdev 0000:c1:01.0")
DOWN = (r"53: ens3f3np3: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq "
        r"state DOWN mode DEFAULT group default qlen 1000\    link/ether "
        r"00:00:5e:00:53:02 brd ff:ff:ff:ff:ff:ff parentbus pci "
        r"parentdev 0000:c1:19.0")
VETH = (r"307: eth0@if308: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc "
        r"noqueue state UP mode DEFAULT group default qlen 1000\    link/ether "
        r"00:00:5e:00:53:10 brd ff:ff:ff:ff:ff:ff link-netnsid 0 promiscuity 0 "
        r"veth addrgenmode eui64")
DUMMY = (r"4: dummy0: <BROADCAST,NOARP,UP,LOWER_UP> mtu 1500 qdisc noqueue "
         r"state UNKNOWN mode DEFAULT group default qlen 1000\    link/ether "
         r"00:00:5e:00:53:20 brd ff:ff:ff:ff:ff:ff \    dummy addrgenmode eui64")
LOOP = (r"1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
        r"mode DEFAULT group default qlen 1000\    link/loopback "
        r"00:00:00:00:00:00 brd 00:00:00:00:00:00")

print("-- reading a link the only way that works inside a namespace --")
one = parse_ip_link(PHYS)
check("the port is named", one.name, "ens3f0np0")
check("its MAC", one.mac, "00:00:5e:00:53:00")
check("the PCI device behind it", one.pci, "0000:c1:01.0")
check("which makes it real hardware", one.physical, True)
check("carrier is read from LOWER_UP", one.carrier, "1")
check("and the state", one.operstate, "up")
check("mtu too", one.mtu, "1500")

down = parse_ip_link(DOWN)
check("NO-CARRIER is not carrier", down.carrier, "0")
check("but it is still hardware", down.physical, True)

print()
print("-- and the things that only look like ports --")
check("a veth is not hardware", parse_ip_link(VETH).physical, False)
check("nor is a dummy", parse_ip_link(DUMMY).physical, False)
check("loopback is dropped outright", parse_ip_link(LOOP), None)
check("so is a line that is not a link", parse_ip_link("total 0"), None)
check("and an empty one", parse_ip_link(""), None)

print()
print("-- an old iproute2 that does not report parentdev --")
OLD = (r"2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP "
       r"mode DEFAULT group default qlen 1000\    link/ether "
       r"00:00:5e:00:53:30 brd ff:ff:ff:ff:ff:ff")
check("a port that names no virtual type is taken as hardware",
      parse_ip_link(OLD).physical, True)
OLDVETH = (r"5: veth9@if6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc "
           r"noqueue state UP mode DEFAULT\    link/ether 00:00:5e:00:53:31 "
           r"brd ff:ff:ff:ff:ff:ff")
check("but a peered one is not, even with no type named",
      parse_ip_link(OLDVETH).physical, False)

print()
print("-- the bulk read of a whole host --")
BULK = "\n".join([
    "SELF|4026531840",
    "NS|4026536055|1265316|bbu-65445c6fdc-wwgj8",
    "LINK|4026536055|" + PHYS,
    "LINK|4026536055|" + VETH,
    "LINK|4026536055|" + LOOP,
    "NS|4026536922|1264113|rex-5ddf8f786c-vwn4h",
    "LINK|4026536922|" + VETH,
])
spaces = parse(BULK)
check("both namespaces are seen", sorted(spaces), ["4026536055", "4026536922"])
check("a pod is named by its own hostname",
      spaces["4026536055"].label, "bbu-65445c6fdc-wwgj8")
check("only the real port counts as hardware",
      [i.name for i in spaces["4026536055"].hardware()], ["ens3f0np0"])
check("a sandbox with only a veth holds none",
      spaces["4026536922"].hardware(), [])
check("an unnamed namespace still reads sensibly",
      parse("NS|4026599999|42|\nLINK|4026599999|" + PHYS
            )["4026599999"].label, "namespace 4026599999")

print()
print("-- what the namespace already knows, for free --")
DETAIL = "\n".join([
    "ADDR|4026536055|32: ens3f0np0    inet 169.254.12.1/24 brd 169.254.12.255 "
    "scope global ens3f0np0\\       valid_lft forever preferred_lft forever",
    "NB|4026536055|169.254.12.11 dev ens3f0np0 lladdr 00:00:5e:00:53:41 REACHABLE",
    "NB|4026536055|169.254.12.90 dev ens3f0np0 lladdr 00:00:00:00:00:00 STALE",
    "NB|4026536055|169.254.12.91 dev ens3f0np0  FAILED",
    "NB6|4026536055|fe80::1 dev ens3f0np0 lladdr 00:00:5e:00:53:41 STALE",
    "NB|4026536055|169.254.12.11 dev ens3f0np0 lladdr 00:00:5e:00:53:41 REACHABLE",
])
add_detail(spaces, DETAIL)
pod = spaces["4026536055"]
check("the port's address is attached",
      pod.ifaces["ens3f0np0"].addrs, ["169.254.12.1/24"])
check("the live neighbour is kept", [n["ip"] for n in pod.neighbours],
      ["169.254.12.11", "fe80::1"])
check("an all-zero lladdr is not a device",
      any(n["mac"] == "00:00:00:00:00:00" for n in pod.neighbours), False)
check("nor is a FAILED entry",
      any("169.254.12.91" == n["ip"] for n in pod.neighbours), False)
check("and the same entry twice is still one", len(pod.neighbours), 2)
check("the state is kept, so nobody reads STALE as present",
      [n["state"] for n in pod.neighbours], ["REACHABLE", "STALE"])
check("detail for a namespace we never kept is ignored",
      add_detail(spaces, "NB|4026599999|1.2.3.4 dev x lladdr "
                         "00:00:5e:00:53:99 REACHABLE"), None)

print()
print("-- the virtual functions each physical port owns --")
CARDS = "\n".join([
    "PF|ens3f0np0|0000:c1:00.0",
    "VF|ens3f0np0|0000:c1:01.0",
    "VF|ens3f0np0|0000:c1:01.1",
    "PF|ens3f1np1|0000:c1:00.1",
    "VF|ens3f1np1|0000:c1:09.0",
    "PF|eno1|0000:04:00.0",
])
check("every VF knows its owner", parse_vfs(CARDS)["0000:c1:01.0"],
      "ens3f0np0")
check("a port with no VFs is not in the map",
      "0000:04:00.0" in parse_vfs(CARDS), False)
check("but it is in the PCI map", parse_pfs(CARDS)["eno1"], "0000:04:00.0")

print()
print("-- the headline: a dark port that is not dead --")
found = {"spaces": spaces, "vfs": parse_vfs(CARDS), "pfs": parse_pfs(CARDS)}
said = explain_dark("ens3f0np0", found)
check("the dark port is explained", bool(said), True)
check("by naming the virtual function, not the interface name",
      "0000:c1:01.0" in said, True)
check("and where that function is", "bbu-65445c6fdc-wwgj8" in said, True)
check("and what is alive on the far side",
      "00:00:5e:00:53:41" in said and "169.254.12.11" in said, True)
check("a port whose VFs are all at home explains nothing",
      explain_dark("ens3f1np1", found), "")
check("nor does a port with no VFs at all",
      explain_dark("eno1", found), "")
check("nor one we have never heard of",
      explain_dark("made-up0", found), "")

print()
print("-- a name match must never be enough --")
# Same interface name in the pod, but a PCI address this port does not own.
IMPOSTOR = parse("NS|4026530001|7|other-pod\nLINK|4026530001|"
                 + PHYS.replace("0000:c1:01.0", "0000:ff:99.9"))
check("a pod holding a same-named port of another card is not the answer",
      explain_dark("ens3f0np0",
                   {"spaces": IMPOSTOR, "vfs": parse_vfs(CARDS),
                    "pfs": parse_pfs(CARDS)}), "")

print()
print("-- a host where we are not allowed to look --")
asked = []


def refuse(cmd):
    asked.append(cmd)
    return "DENIED|not root\n"


said_lines = []
out = discover(refuse, said_lines.append)
check("nothing is claimed", out["spaces"], {})
check("the reason is kept", out["refused"], "not root")
check("and we only asked once", len(asked), 1)
check("the operator is told ports may be hidden",
      any("may be hidden" in m for m in said_lines), True)

print()
print("-- a plain host with nothing hidden --")
calls = []


def plain(cmd):
    calls.append(cmd)
    return "SELF|4026531840\n"


out = discover(plain, said_lines.append)
check("no namespaces, no second read", out["spaces"], {})
check("so only the listing ran", len(calls), 1)


def broken(_cmd):
    raise RuntimeError("ssh died")


check("a host that drops the connection does not take the scan down",
      discover(broken)["spaces"], {})
check("and says why", "RuntimeError" in discover(broken)["refused"], True)

print()
print("-- end to end, as the probe calls it --")
seen = []


def host(cmd):
    seen.append(cmd)
    if "ns/net" in cmd:
        return BULK
    if "ip -o addr show" in cmd:
        return DETAIL
    if "virtfn" in cmd:
        return CARDS
    return ""


notes = []
out = discover(host, notes.append)
check("only the namespace holding a port is kept", sorted(out["spaces"]),
      ["4026536055"])
check("three reads, no more", len(seen), 3)
check("the detail read asks only for that one",
      "4026536055:1265316" in seen[1] and "4026536922" not in seen[1], True)
check("the VF map came back", out["vfs"]["0000:c1:01.0"], "ens3f0np0")
check("the operator is told what was found",
      any("bbu-65445c6fdc-wwgj8 (ens3f0np0)" in m for m in notes), True)
check("and how many were passed over", any("1 of 2" in m for m in notes), True)
check("it survives the JSON it will be written to",
      out["spaces"]["4026536055"].to_dict()["ifaces"]["ens3f0np0"]["pci"],
      "0000:c1:01.0")

print()
print("-- a host in a strange state cannot run away with the scan --")
check("there is a ceiling on how many namespaces we enter",
      MAX_SPACES <= 256 and MAX_SPACES >= 8, True)
check("and the script carries it",
      "%(cap)d" not in (host.__doc__ or "") and MAX_SPACES > 0, True)

print()
print("-- a port and its own virtual function are one port --")
from linkfinder.netns import one_port

HELD = {
    "netns_vfs": parse_vfs(CARDS),
    "netns": {"4026536055": {
        "inode": "4026536055", "name": "bbu-65445c6fdc-wwgj8",
        "ifaces": {"ens3f0np0": {"mac": "00:00:5e:00:53:00",
                                 "pci": "0000:c1:01.0"},
                   "ens3f2np2": {"mac": "00:00:5e:00:53:01",
                                 "pci": "0000:c1:11.0"},
                   "eth0": {"mac": "00:00:5e:00:53:10", "pci": ""}},
        "neighbours": []}}}

check("the physical port and its own function are one",
      one_port(HELD, "ens3f0np0", "ens3f0np0 in bbu-65445c6fdc-wwgj8"),
      "ens3f0np0")
check("and it reads the same in either order",
      one_port(HELD, "ens3f0np0 in bbu-65445c6fdc-wwgj8", "ens3f0np0"),
      "ens3f0np0")

# The names collide on purpose, so a name match must never be the reason.
check("a function of a different port is not the same port",
      one_port(HELD, "ens3f2np2", "ens3f0np0 in bbu-65445c6fdc-wwgj8"), "")
check("two root ports are never one port",
      one_port(HELD, "ens3f0np0", "ens3f2np2"), "")
check("a veth in the pod owns no hardware",
      one_port(HELD, "ens3f0np0", "eth0 in bbu-65445c6fdc-wwgj8"), "")
check("a pod we do not hold is not consulted",
      one_port(HELD, "ens3f0np0", "ens3f0np0 in some-other-pod"), "")
check("with no virtual-function map nothing is claimed",
      one_port({"netns": HELD["netns"]}, "ens3f0np0",
               "ens3f0np0 in bbu-65445c6fdc-wwgj8"), "")
check("and an empty record is harmless",
      one_port({}, "ens3f0np0", "ens3f0np0 in bbu-65445c6fdc-wwgj8"), "")

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all namespace checks passed")

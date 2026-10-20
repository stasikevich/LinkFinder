"""Four things the bench showed that no fixture had.

Every case here came off a real estate, not out of my head, and each one
had a test suite passing over the top of it at the time. They are pinned
together because they share a cause: the tool had the evidence and drew
the wrong conclusion from it, which no amount of parser testing finds.

  - an RGB controller called a handset, because its maker also makes phones
  - an Ubuntu virtual machine called a router, because it forwards packets
  - a port with two neighbours keeping whichever came last
  - "the others on this network" reasoning applied to a link-local address,
    where the address belongs to one cable and means nothing shared

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.handsets import classify
from linkfinder.neighbors import classify_kind
from linkfinder.render_topology import port_neighbours, topology_table

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- what is plugged in, from what it offers and what it is called --")
# Every one of these was on one bench at one moment. The interfaces are
# as the kernel reported them.
BENCH = [
    ("AURA LED Controller", "AsusTek Computer Inc.", "ffffff,030000",
     "peripheral"),
    ("IV2201", "OnePlus", "080650,ff4201,020600", "phone"),
    ("OnePlus Nord", "OnePlus", "010100,010300", "phone"),
    ("SMCI HID KM", "Linux 5.4.62 with aspeed", "030001,030102", "peripheral"),
    ("RNDIS/Ethernet Gadget", "Linux 5.4.62", "e00103,0a0000", "peripheral"),
    ("KALAMA-MTP_CID:0437_SN:5443F91F", "", "060101,ff4201", "phone"),
]
for product, maker, interfaces, want in BENCH:
    check("%s is a %s" % (product[:30], want),
          classify({"product": product, "maker": maker,
                    "interfaces": interfaces}), want)
# The maker is still worth something where nothing else is known - a phone
# that is asleep offers nothing at all.
check("and a bare phone name is still a phone",
      classify({"product": "SM-G998B", "maker": "Samsung", "interfaces": ""}),
      "phone")

print()
print("-- what a device is, not what it is doing --")
UBUNTU = "Ubuntu 22.04.4 LTS Linux 5.15.0-177-generic #187-Ubuntu x86_64"
check("a Linux box that forwards is a host, not a router",
      classify_kind("", "bridge,router", UBUNTU), "host")
check("a switch that says it is a switch still is",
      classify_kind("", "bridge", "Cisco IOS Software, C2960X"), "switch")
check("and with nothing said, the capability still answers",
      classify_kind("", "router", ""), "router")
check("a bridge with nothing said is still a switch",
      classify_kind("", "bridge", ""), "switch")
# These name the device rather than a thing it happens to be doing, so
# they stay above the description.
check("wireless beats the description", classify_kind("", "wlan", UBUNTU), "ap")
check("so does telephone", classify_kind("", "telephone", UBUNTU), "phone")

print()
print("-- a port that reported two neighbours keeps the switch --")
# Measured: a management port heard both its switch and a machine behind
# it. Keeping the last one threw the switch away, and then "which switch
# is this port in" was answered with the name of another server.
TWO_ENDS = {
    "nodes": [
        # An address carries its prefix, the way the probe reports it -
        # without one there is no network to speak of.
        {"id": "ip:10.0.0.1", "name": "host-a", "kind": "host",
         "mgmt_ips": ["10.0.0.1"],
         "extra": {"ports": {"eno1": {"addresses": ["10.0.0.1/24"]}}}},
        {"id": "ip:10.0.0.2", "name": "host-b", "kind": "host",
         "mgmt_ips": ["10.0.0.2"],
         "extra": {"ports": {"eno1": {"addresses": ["10.0.0.2/24"]}}}},
        # The one the bench actually showed: on the network, nothing
        # announced on it, and one blank row in the table saying "scanned".
        {"id": "ip:10.0.0.3", "name": "host-quiet", "kind": "host",
         "mgmt_ips": ["10.0.0.3"], "state": "scanned",
         "extra": {"ports": {"eno1": {"addresses": ["10.0.0.3/24"]}}}},
        {"id": "chassis:sw1", "name": "sw-access-1", "kind": "switch"},
    ],
    "edges": [
        {"a": "ip:10.0.0.1", "a_iface": "eno1", "b": "chassis:sw1",
         "b_iface": "Gi0/1", "protos": ["LLDP"]},
        {"a": "ip:10.0.0.1", "a_iface": "eno1", "b": "ip:10.0.0.2",
         "b_iface": "eno1", "protos": ["LLDP"]},
    ],
}
found, nets = port_neighbours(TWO_ENDS)
check("the switch is the one kept for that port",
      found["ip:10.0.0.1"]["eno1"], "sw-access-1")
check("and it is the switch offered to the rest of the network",
      (nets.get("10.0.0.0/24") or [""])[0], "sw-access-1")

print()
print("-- a host that announces nothing is told where its network goes --")
rows = topology_table(TWO_ENDS)
guessed = [r for r in rows if r[9] == "same network"]
check("the quiet host gets a row rather than a blank line",
      [(r[0], r[7]) for r in guessed], [("host-quiet", "sw-access-1")])
check("and it is marked as somebody else's evidence",
      [r[10] for r in guessed], ["others on 10.0.0.0/24"])
check("a host that answered for itself is not guessed at",
      [r for r in guessed if r[0] == "host-b"], [])

print()
print("-- a cable from a machine back into itself explains itself --")
# Asked from the bench: "why do we have a loop in enp23s0f3 enp101s0f3?"
# The map drew the loop and said nothing, so somebody had to ask. It is
# worth explaining because the answer turns on one fact: separate ports
# forward nothing, and the same two in a bridge flood.
from linkfinder.render_topology import _self_cable_lines

LOOPED = {"extra": {
    "macs": {"enp23s0f3": "00:00:5e:00:53:a1",
             "enp101s0f3": "00:00:5e:00:53:b2",
             "eno1": "00:00:5e:00:53:c3"},
    "ports": {"enp23s0f3": {}, "enp101s0f3": {}, "eno1": {}},
    "bridges": ["docker0"],
    "neigh6": {
        "enp23s0f3": {"peers": [{"mac": "00:00:5e:00:53:b2"}]},
        "enp101s0f3": {"peers": [{"mac": "00:00:5e:00:53:a1"}]},
        "eno1": {"peers": [{"mac": "00:00:5e:00:53:ff"}]}}}}
lines = _self_cable_lines(LOOPED)
check("the pair is named once, not once from each end", len(lines), 2)
check("and it names both ports",
      "enp23s0f3" in lines[0] and "enp101s0f3" in lines[0], True)
check("a port facing somebody else is not called a loop",
      "eno1" in " ".join(lines), False)
check("it says nothing is forwarded while they are separate",
      "nothing is forwarded" in lines[-1], True)

# The same cable with the two ports in one bridge is the dangerous case,
# and must read differently rather than reassuringly.
import copy as _copy

DANGEROUS = _copy.deepcopy(LOOPED)
DANGEROUS["extra"]["bridges"] = ["br0", "enp23s0f3", "enp101s0f3"]
check("but in one bridge it says the opposite",
      "will flood" in " ".join(_self_cable_lines(DANGEROUS)), True)
check("a machine with no such cable gets no section",
      _self_cable_lines({"extra": {"macs": {"eno1": "00:00:5e:00:53:c3"},
                                   "ports": {"eno1": {}}}}), [])

print()
print("-- an optic says what fibre it needs, not just its speed --")
# The wavelength and the reach were read off the module and left out of
# the line somebody matching a module to a fibre actually reads. 850 nm
# will not light a single-mode run, and no amount of "10G SR" says so.
from linkfinder.render_topology import _bmc_lines, _optics_lines

optics = _optics_lines({
    "ens1f0": {"speed_gbps": 10, "form": "LR", "media": "single-mode",
               "wavelength_nm": 1310, "reach_smf_km": 10, "vendor": "Intel Corp"},
    "ens1f1": {"speed_gbps": 10, "form": "SR", "media": "multimode",
               "wavelength_nm": 850, "reach_om3_m": 300, "vendor": "Intel Corp"}})
check("the long-reach module names its wavelength and distance",
      optics[0], "ens1f0         10G LR single-mode 1310 nm 10 km, Intel Corp")
check("and the short-reach one its own",
      optics[1], "ens1f1         10G SR multimode 850 nm 300 m OM3, Intel Corp")
check("a module that reported neither is still listed",
      _optics_lines({"ens2f0": {"speed_gbps": 25, "form": "SR"}}),
      ["ens2f0         25G SR"])

print()
print("-- and what the link settled at, not only what it is rated for --")
# Measured on the estate: three 10G modules were carrying 1 Gb/s and nothing
# said so, because the line showed the module's own word for itself. The
# rating and the rate are different numbers and only the second is the
# network. Asked for by name: "real SFP speed".
MODULE = {"ens1f0": {"speed_gbps": 10, "form": "SR", "media": "multimode",
                     "wavelength_nm": 850, "vendor": "Intel Corp"}}
check("a link doing what the module is rated for says so plainly",
      _optics_lines(MODULE, {"ens1f0": {"speed": "10000Mb/s",
                                        "carrier": "up"}}),
      ["ens1f0         10G SR multimode 850 nm, up at 10G, Intel Corp"])
check("one settling for less names both numbers",
      _optics_lines(MODULE, {"ens1f0": {"speed": "1000Mb/s",
                                        "carrier": "up"}}),
      ["ens1f0         10G SR multimode 850 nm, "
       "up at 1G - the module is rated 10G, Intel Corp"])
check("a module in a port carrying nothing says that instead",
      _optics_lines(MODULE, {"ens1f0": {"speed": "", "carrier": "down"}}),
      ["ens1f0         10G SR multimode 850 nm, link down, Intel Corp"])
# A port whose speed was never read must not be called down: that is an
# absence of a reading, and saying "link down" would invent a fault.
check("and one with nothing read about it claims neither",
      _optics_lines(MODULE, {}),
      ["ens1f0         10G SR multimode 850 nm, Intel Corp"])

print()
print("-- a 40G module is not a module with no speed at all --")
# Seven QSFP+ ports on one host showed a wavelength and nothing else: the
# type is "40G Base-LR4" and the pattern demanded a word boundary straight
# after LR, where the lane count sits. Speed, form and media were all lost.
from linkfinder.optics import _classify

check("40G Base-LR4 is read",
      _classify(["40G Ethernet: 40G Base-LR4"]),
      {"form": "LR", "media": "single-mode", "speed_gbps": 40})
check("so is 100G Base-SR4",
      _classify(["100G Ethernet: 100G Base-SR4"]),
      {"form": "SR", "media": "multimode", "speed_gbps": 100})
check("and the single-lane modules still read as they did",
      _classify(["10G Ethernet: 10G Base-LR", "Ethernet: 1000BASE-LX"]),
      {"form": "LR", "media": "single-mode", "speed_gbps": 10})
check("copper over one pair too",
      _classify(["Ethernet: 1000BASE-T"]),
      {"form": "T", "media": "copper", "speed_gbps": 1})

print()
print("-- with no ipmitool, it says what it does know --")
# It used to say only "ipmitool is not installed", which tells somebody
# what did not happen and nothing they can use. NetBox is often not
# connected at all, so the map's own evidence has to count on its own.
RECORDED = {"addresses": [{"port": "IPMI", "address": "198.51.100.9"}]}
check("the address NetBox holds is offered",
      any("198.51.100.9" in l for l in _bmc_lines({"tool": "absent"}, RECORDED)),
      True)
check("with no NetBox, a controller seen elsewhere on the map still counts",
      any("server13" in l for l in
          _bmc_lines({"tool": "absent", "seen_from": "server13"}, None)), True)
# Which tool was missing is not an answer. With no address from anywhere,
# the section says nothing at all rather than naming a program to install.
check("and knowing no address says nothing, not what tool to install",
      _bmc_lines({"tool": "absent"}, None), [])

print()
print("-- the table says when each link was first seen --")
# It was being recorded against every link from the very first version and
# shown nowhere, so a cable that turned up this morning and one that has
# been there for months read exactly alike.
import csv as _csv
import io as _io

from linkfinder.render_topology import TABLE_COLUMNS, topology_csv

DATED = dict(TWO_ENDS)
DATED["edges"] = [dict(e, first_seen="2026-03-04T09:12:00")
                  for e in TWO_ENDS["edges"]]
parsed = list(_csv.reader(_io.StringIO(topology_csv(DATED))))
check("the column is there", parsed[0][-1], "first seen")
check("and every row is as wide as the header",
      {len(r) for r in parsed[1:]}, {len(TABLE_COLUMNS)})
check("a real link carries the day it appeared, not the second",
      sorted({r[-1] for r in parsed[1:]
              if r[7] and r[9] != "same network"}), ["2026-03-04"])
# A row inferred from the network, or a device with nothing recorded
# against it, has no date to give - and says nothing rather than today's.
check("and one with no date recorded says nothing",
      [r[-1] for r in parsed[1:] if r[9] == "same network"], [""])

print()
print("-- one phone is one box, not one box per function it offers --")
# Verbatim from a bench NUC. Windows presents a composite device as several
# PnP devices: the parent carries the serial, and ADB, MTP and the modem are
# each a device with a name. The map drew four boxes around the host, joined
# by four links labelled with sixty characters of instance id.
from linkfinder.handsets import parse_usb_windows
from linkfinder.render_topology import _plugged_in_lines

ONE_PHONE = """
DEV USB\\VID_04E8&PID_6860\\R5CWC0K0S8N
product=SAMSUNG Mobile USB Composite Device
maker=SAMSUNG Electronics Co., Ltd.
DEV USB\\VID_04E8&PID_6860&ADB\\6&1C62D353&0&0003
product=SAMSUNG Android ADB Interface
maker=SAMSUNG Electronics Co., Ltd.
DEV USB\\VID_04E8&PID_6860&MS_COMP_MTP&SAMSUNG_ANDROID\\6&1C62D353&0&0000
product=Galaxy A54 5G
maker=samsung
DEV USB\\VID_04E8&PID_6860&MODEM\\6&1C62D353&0&0001
product=SAMSUNG Mobile USB Modem
maker=SAMSUNG Electronics Co., Ltd.
"""
folded = parse_usb_windows(ONE_PHONE)
check("four PnP devices are one phone", len(folded), 1)
only = list(folded.values())[0]
check("the serial is the one the parent carries", only["serial"], "R5CWC0K0S8N")
check("and the name is the one a person would use", only["product"],
      "Galaxy A54 5G")
check("it is still recognised as a phone", only["kind"], "phone")

# Two of the same model on one machine is normal on a bench and must stay two.
TWO = ONE_PHONE + """
DEV USB\\VID_04E8&PID_6860\\R5CWC0K0S9X
product=SAMSUNG Mobile USB Composite Device
maker=SAMSUNG Electronics Co., Ltd.
"""
check("but two handsets of one model stay two", len(parse_usb_windows(TWO)), 2)
check("each keeping its own serial",
      sorted(e["serial"] for e in parse_usb_windows(TWO).values()),
      ["R5CWC0K0S8N", "R5CWC0K0S9X"])

# A device that shows only as functions - no composite parent - must not
# vanish, and must not come back as three.
PARTS_ONLY = """
DEV USB\\VID_18D1&PID_4EE7&ADB\\7&2A1B3C4D&0&0001
product=Android ADB Interface
maker=Google
DEV USB\\VID_18D1&PID_4EE7&MS_COMP_MTP\\7&2A1B3C4D&0&0000
product=Pixel 8
maker=Google
"""
orphan = parse_usb_windows(PARTS_ONLY)
check("a device with no parent is kept once", len(orphan), 1)
check("under the name worth reading",
      list(orphan.values())[0]["product"], "Pixel 8")

print()
print("-- the same handset reads the same on Linux as on Windows --")
# Reported from the bench: "on Windows it looks nice, on Linux it writes
# KALAMA-MTP_CID:0437_SN:5C97E23F". Windows reads the model out of the
# driver; Linux gets the USB product string, which on an Android with adb
# off is the MTP descriptor - chipset, carrier id and serial, no model.
from linkfinder.handsets import describe

LINUX = {"maker": "OnePlus", "product": "KALAMA-MTP_CID:0437_SN:5C97E23F",
         "serial": "5c97e23f"}
check("a descriptor is not used as a name", describe(LINUX),
      "OnePlus 5c97e23f")
check("and two of them are told apart by their serials",
      describe(dict(LINUX, product="KALAMA-MTP_CID:0437_SN:1EF9E80E",
                    serial="1ef9e80e")), "OnePlus 1ef9e80e")
check("a real model name is left exactly as it is",
      describe({"maker": "SAMSUNG Electronics Co., Ltd.",
                "product": "Galaxy A54 5G", "serial": "R5CWC0K0S8N"}),
      "Galaxy A54 5G")
check("adb's model beats both when adb has run",
      describe(dict(LINUX, adb_model="CPH2573")), "CPH2573")
check("with no maker to fall back on, the descriptor stays",
      describe(dict(LINUX, maker="")),
      "KALAMA-MTP_CID:0437_SN:5C97E23F")
# The rule must not fire on ordinary hardware whose name is a part number.
check("a UART bridge keeps its part number",
      describe({"maker": "FTDI", "product": "FT230X Basic UART",
                "serial": "DK0CGVVM"}), "FT230X Basic UART")
check("and a bench instrument keeps its model",
      describe({"maker": "Agilent Technologies", "product": "N8737A",
                "serial": "US19H2098R"}), "N8737A")

print()
print("-- what is plugged in is said in words, not drawn as a label --")
check("the phone is one line: its name, and its serial if not already in it",
      _plugged_in_lines(folded),
      ["phone          Galaxy A54 5G, serial R5CWC0K0S8N"])
# The panel is "which phones are here", nothing more. The USB bus path and the
# adb state were noise; a name that already carries the serial is not made to
# repeat it.
check("a name that already holds the serial does not repeat it",
      _plugged_in_lines({"1-3.3": {"kind": "phone", "product": "OnePlus abc123",
                                   "serial": "abc123"}}),
      ["phone          OnePlus abc123"])
check("the bus path is gone - it answered no question anyone asked",
      any("on 1-" in l for l in
          _plugged_in_lines({"1-2": {"kind": "phone", "product": "ONEPLUS A5000",
                                     "serial": "abc123"}})),
      False)
check("everything else is a count, not a list",
      _plugged_in_lines({"1-1": {"kind": "peripheral", "product": "mouse"},
                         "1-3": {"kind": "peripheral", "product": "keyboard"}}),
      ["peripheral     2"])
check("and nothing plugged in gets no section", _plugged_in_lines({}), [])
# The handset's OWN record is filed under the same key, flat, with string
# values. Read as a list of devices it threw, and the whole page went with
# it - the tool drew no map at all until the panel was asked for nothing.
check("a handset's own record is not a list of what is plugged into it",
      _plugged_in_lines({"port": "1-2", "kind": "phone", "serial": "aaa1"}), [])

# The same shapes, through the whole renderer, because that is where it broke.
from linkfinder.render_topology import render_html

PHONE_MAP = {
    "nodes": [
        {"id": "ip:198.51.100.10", "name": "nuc1", "kind": "host",
         "state": "scanned", "mgmt_ips": ["198.51.100.10"],
         "extra": {"usb": {"1-2": {"port": "1-2", "kind": "phone",
                                   "product": "ONEPLUS A5000",
                                   "serial": "aaa1"}}}},
        {"id": "chassis:usb:aaa1", "name": "ONEPLUS A5000", "kind": "phone",
         "state": "scanned",
         "extra": {"usb": {"port": "1-2", "kind": "phone", "serial": "aaa1",
                           "product": "ONEPLUS A5000", "version": "2.00"}}},
    ],
    "edges": [{"id": "e1", "a": "ip:198.51.100.10", "a_iface": "usb",
               "b": "chassis:usb:aaa1", "b_iface": "usb",
               "protos": ["usb"], "sources": ["usb"],
               "attrs": {"usb": {"version": "2.00", "speed_mbps": 480,
                                 "port": "1-2"}}}],
}
page = render_html(PHONE_MAP)
check("a map with a phone on it is drawn at all", page.count("<html") , 1)
check("the host's panel says what is plugged into it",
      "ONEPLUS A5000, serial aaa1" in page, True)
import json as _jsonlib

drawn = _jsonlib.loads(page.split("const ELEMENTS = ")[1].split("\n")[0]
                       .rstrip().rstrip(";"))
usb_edge = drawn["edges"][0]["data"]
check("the link carries no label - the colour already says the generation",
      usb_edge.get("label"), "")
check("but the port is still in the link's own record",
      ((usb_edge.get("attrs") or {}).get("usb") or {}).get("port"), "1-2")

print()
print("-- and not on a link-local address, which is per cable --")
LINK_LOCAL = {
    "nodes": [
        {"id": "ip:a", "name": "bbu-a", "kind": "host",
         "extra": {"ports": {"ens1": {"addresses": ["169.254.10.1/16"]}}}},
        {"id": "ip:b", "name": "bbu-b", "kind": "host",
         "extra": {"ports": {"ens1": {"addresses": ["169.254.10.2/16"]}}}},
        {"id": "chassis:sw9", "name": "sw-9", "kind": "switch"},
    ],
    "edges": [{"a": "ip:a", "a_iface": "ens1", "b": "chassis:sw9",
               "b_iface": "Gi0/9", "protos": ["LLDP"]}],
}
_f, ll_nets = port_neighbours(LINK_LOCAL)
check("no network is claimed from a link-local address",
      [n for n in ll_nets if n.startswith("169.254")], [])
check("so nothing is guessed from it",
      [r for r in topology_table(LINK_LOCAL) if r[9] == "same network"], [])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all bench-report checks passed")

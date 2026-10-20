"""Naming the maker, and refusing to when there is no honest answer.
Run: python tests/test_oui.py

Every expected name here comes from the IEEE register the tool ships with,
so these also check that the asset got built and bundled.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import oui

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


print("-- the register is there at all --")
check("it loaded", len(oui.table()) > 40000, True)
check("and only once", oui.table() is oui.table(), True)

print()
print("-- a plain 24-bit assignment --")
check("the usual case", oui.vendor("00:50:56:11:22:33"), "VMware")
check("an unassigned prefix names nobody",
      oui.vendor("fe:ff:ff:00:00:01"), "")

print()
print("-- the prefixes that are not 24 bits --")
# 00:55:da and 00:1b:c5 are administration blocks: the whole point is that
# the first three octets belong to nobody and the answer is further in.
check("a 28-bit assignment is found",
      oui.vendor("00:55:da:10:00:01"), "KoolPOS")
check("a different slice of the same block is a different maker",
      oui.vendor("00:55:da:20:00:01"),
      "Beijing Connected Information Technology")
check("a 36-bit assignment is found",
      oui.vendor("00:1b:c5:00:12:34"), "OpenRB.com, Direct SIA")
check("the block itself is not in the register", "001BC5" in oui.table(),
      False)
check("so an unassigned slice of it names nobody",
      oui.vendor("00:1b:c5:ff:ff:ff"), "")

print()
print("-- however the MAC was written down --")
for written in ("00:50:56:11:22:33", "00-50-56-11-22-33",
                "0050.5611.2233", "005056112233",
                "00:50:56:11:22:33  ", "00:50:56:AB:CD:EF"):
    check("%-22s reads the same" % written, oui.vendor(written), "VMware")
check("even just the prefix", oui.vendor("00:50:56"), "VMware")

print()
print("-- addresses nobody was ever assigned --")
check("a locally administered MAC names nobody",
      oui.vendor("ea:d6:a0:3f:bb:dc"), "")
check("and is called what it is",
      oui.mac_kind("ea:d6:a0:3f:bb:dc"), "locally administered")
check("every locally administered bit pattern is caught",
      sorted({oui.mac_kind("%02x:00:00:00:00:01" % n)
              for n in (0x02, 0x06, 0x0a, 0x0e, 0xfe)}),
      ["locally administered"])
check("a multicast group is not a device",
      oui.mac_kind("01:80:c2:00:00:0e"), "multicast")
check("broadcast least of all", oui.mac_kind("ff:ff:ff:ff:ff:ff"), "multicast")
check("a real card is universal", oui.mac_kind("00:50:56:11:22:33"),
      "universal")

print()
print("-- rubbish in, nothing out --")
for junk in ("", "not-a-mac", "zz:zz:zz:zz:zz:zz", "00:50", None, 12345):
    check("%-22r is not looked up" % junk, oui.vendor(junk), "")
check("a half-written MAC has no kind", oui.mac_kind("00:50:56"), "")
check("nor does nothing at all", oui.mac_kind(""), "")

print()
print("-- the ladder: who gets to say who made it --")
BOARD = {"chassis_id": "00:50:56:11:22:33",
         "extra": {"virtual": {"sys_vendor": "Supermicro"}}}
said = oui.vendor_of(BOARD)
check("the board's own record beats the register", said["vendor"],
      "Supermicro")
check("and says where that came from",
      "DMI" in said["vendor_source"], True)
check("worth more than a lookup", said["vendor_confidence"], "high")

bare = oui.vendor_of({"chassis_id": "00:50:56:11:22:33"})
check("with nothing better, the register answers", bare["vendor"], "VMware")
check("named as such", bare["vendor_source"], "MAC prefix (IEEE)")
check("and worth less", bare["vendor_confidence"], "medium")

print()
print("-- a description is not a maker --")
# One switch here answers this over LLDP. It is a firmware build; letting
# it win would bury the register's correct answer under a version number.
FIRMWARE = {"chassis_id": "00:50:56:11:22:33",
            "vendor": "8.24.2.2-B3 2024-11-04T13:44:36+02:00"}
check("a version string never becomes the maker",
      oui.vendor_of(FIRMWARE)["vendor"], "VMware")
check("a model string does not either",
      oui.vendor_of({"chassis_id": "00:50:56:11:22:33",
                     "vendor": "Aruba JL676A PL.10.13.1161"})["vendor"],
      "VMware")
for placeholder in ("To be filled by O.E.M.", "Default string", "unknown",
                    "System manufacturer", "  ", "1.2.3"):
    check("DMI saying %-24r is refused" % placeholder,
          oui.vendor_of({"chassis_id": "00:50:56:11:22:33",
                         "extra": {"virtual": {"sys_vendor": placeholder}}}
                        )["vendor"], "VMware")

print()
print("-- when there is nothing to say, say nothing --")
none = oui.vendor_of({"chassis_id": "ea:d6:a0:3f:bb:dc"})
check("a made-up address gets no maker", none["vendor"], "")
check("with no source invented", none["vendor_source"], "")
check("and the confidence says so", none["vendor_confidence"], "none")
check("but the kind of address is still reported", none["mac_type"],
      "locally administered")
check("a device with no MAC at all is handled",
      oui.vendor_of({})["vendor_confidence"], "none")
check("and a multicast one is never looked up",
      oui.vendor_of({"chassis_id": "01:80:c2:00:00:0e"})["vendor"], "")

print()
print()
print("-- a maker tells us what the hardware is --")
from linkfinder.oui import kind_from_maker, vendor
check("a clock maker gives a timing source", kind_from_maker("Trimble"), "timing")
check("so does the other one", kind_from_maker("SEIKO SYSTEMS, INC"), "timing")
check("a maker not in the table is left alone, whatever it builds",
      kind_from_maker("A Clock Maker We Did Not List"), "")
check("a radio maker gives a radio",
      kind_from_maker("Comba Telecom Systems (China)"), "radio")
check("a server maker gives a host",
      kind_from_maker("Super Micro Computer"), "host")
check("a switch maker gives a switch", kind_from_maker("Routerboard.com"), "switch")
check("a maker we have no opinion on stays unnamed",
      kind_from_maker("Some Unlisted Gmbh"), "")
check("and so does an empty name", kind_from_maker(""), "")
check("the register itself still answers for a real address",
      vendor("00:17:47:70:4c:4a"), "Trimble")

if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all vendor checks passed")

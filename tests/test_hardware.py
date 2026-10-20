"""Checks for the number an inventory is keyed by, and for refusing a fake one.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import hardware

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- a serial names one unit, or it is not a serial --")
for good in ("S900001X1234567", "ABC1234", "900000000001", "C900BAN00A00002",
             "ZYX9876"):
    check("%r is a serial" % good, hardware.is_real(good), True)
for bad in ("To Be Filled By O.E.M.", "Default string", "Not Specified",
            "System Serial Number", "0123456789", "None", "", "  ", "0",
            "-", "00000000", "n/a"):
    check("%r is not" % bad, hardware.is_real(bad), False)

print()
print("-- what a Linux box publishes --")
DMI = """product_serial=S900001X1234567
chassis_serial=C900MKJ00A00001
board_serial=OM900S000001
product_name=SYS-1019D-14C-RD
product_uuid=00000000-0000-0000-0000-aabbcc000000
sys_vendor=Supermicro
board_vendor=Supermicro
chassis_asset_tag=
"""
fields = hardware.parse_fields(DMI)
check("the unit's own serial wins", hardware.serial_of(fields), "S900001X1234567")
check("and the box can be named", hardware.describe(fields),
      "Supermicro SYS-1019D-14C-RD")
check("an empty field is kept out of the way", fields["chassis_asset_tag"], "")

WHITEBOX = hardware.parse_fields("""product_serial=To Be Filled By O.E.M.
chassis_serial=Default string
board_serial=BX7700123456
product_name=To Be Filled By O.E.M.
sys_vendor=ASUSTeK COMPUTER INC.
""")
check("a board serial is used when the placeholders are all there is",
      hardware.serial_of(WHITEBOX), "BX7700123456")

NOTHING = hardware.parse_fields("""product_serial=To Be Filled By O.E.M.
chassis_serial=Default string
board_serial=Default string
sys_vendor=ASUSTeK COMPUTER INC.
""")
check("and a box with nothing real says nothing, rather than inventing one",
      hardware.serial_of(NOTHING), "")

print()
print("-- what Windows publishes --")
WIN = hardware.parse_fields("""product_serial=ZYX9876
product_name=OptiPlex 3080
sys_vendor=Dell Inc.
""")
check("the BIOS serial is read", hardware.serial_of(WIN), "ZYX9876")
check("and the model", hardware.describe(WIN), "Dell Inc. OptiPlex 3080")

print()
print("-- what ESXi publishes --")
ESXI = """Platform Information
   UUID: 0x33 0x36 0x31
   Product Name: PowerEdge R640
   Vendor Name: Dell Inc.
   Serial Number: QWE4567
   Enclosure Serial Number: QWE4567
   BIOS Asset Tag: unknown
"""
fields = hardware.parse_esxi(ESXI)
check("the serial comes out of the indented block",
      hardware.serial_of(fields), "QWE4567")
check("and so does the model", hardware.describe(fields), "Dell Inc. PowerEdge R640")
check("an asset tag of 'unknown' is not mistaken for one",
      hardware.is_real(fields.get("chassis_asset_tag", "")), False)

print()
print("-- what a BMC publishes --")


class FakeRedfish(object):
    """Only the two calls the reader makes, so the test cannot pass by accident."""

    def __init__(self, bodies, members=None):
        self.bodies = bodies
        self.lists = members or {}
        self.asked = []

    def members(self, path):
        return self.lists.get(path, [])

    def get(self, path):
        self.asked.append(path)
        return self.bodies.get(path)


bmc = FakeRedfish(
    {"/redfish/v1/Chassis/Self": {"SerialNumber": "C900BAN00A00002",
                                  "Model": "X12SPW-TF",
                                  "Manufacturer": "Supermicro",
                                  "PartNumber": "MBD-X12SPW"},
     "/redfish/v1/Systems/Self": {"SerialNumber": "C900BAN00A00002",
                                  "Model": "SYS-110P", "Manufacturer": "Supermicro"}},
    {"/redfish/v1/Chassis": ["/redfish/v1/Chassis/Self"],
     "/redfish/v1/Systems": ["/redfish/v1/Systems/Self"]})
fields = hardware.read_redfish(bmc)
check("a BMC that does not call its chassis '1' is still read",
      hardware.serial_of(fields), "C900BAN00A00002")
check("the collection was listed, not guessed",
      "/redfish/v1/Chassis/Self" in bmc.asked, True)
check("the part number is kept too", fields.get("part_number"), "MBD-X12SPW")

empty = FakeRedfish({"/redfish/v1/Chassis/1": {"SerialNumber": "Not Specified",
                                               "Model": "CrateBox"}})
fields = hardware.read_redfish(empty)
check("a placeholder serial is refused but the model is kept",
      (hardware.serial_of(fields), fields.get("product_name")), ("", "CrateBox"))

print()
print("-- what a switch publishes over SNMP --")
# ENTITY-MIB answers for every fan, optic and power supply as well. Only the
# chassis entry is the box an inventory would hold.
TABLE = {
    hardware.OID_ENT_SERIAL: {"1": "", "2": "900000000001", "7": "QFX2233DD1",
                              "9": "FAN-00881"},
    hardware.OID_ENT_CLASS: {"1": "1", "2": "3", "7": "10", "9": "7"},
    hardware.OID_ENT_MODEL: {"1": "", "2": "NX-3200-24S", "7": "SFP-10G-LR"},
}
fields = hardware.read_snmp(lambda oid: TABLE.get(oid, {}))
check("the chassis serial is taken, not an optic's",
      hardware.serial_of(fields), "900000000001")
check("and its model with it", fields.get("product_name"), "NX-3200-24S")

NO_CLASS = {hardware.OID_ENT_SERIAL: {"1": "", "4": "900000000001"}}
fields = hardware.read_snmp(lambda oid: NO_CLASS.get(oid, {}))
check("a device that publishes no entity classes still gives up its serial",
      hardware.serial_of(fields), "900000000001")

check("a device with no ENTITY-MIB at all is not a failure",
      hardware.read_snmp(lambda oid: {}), {})


def angry(_oid):
    raise RuntimeError("timed out")


check("and neither is one that times out", hardware.read_snmp(angry), {})

print()
print("-- what it does with what it found --")


class Result(object):
    def __init__(self):
        self.extra = {}
        self.device_serial = ""


said = []
r = Result()
got = hardware.attach(r, hardware.parse_fields(DMI), said.append)
check("the serial is put on the result", r.device_serial, "S900001X1234567")
check("and returned", got, "S900001X1234567")
check("the whole record is kept for the panel",
      r.extra["hardware"]["product_name"], "SYS-1019D-14C-RD")
check("an empty field is not carried into it",
      "chassis_asset_tag" in r.extra["hardware"], False)
check("and a person is told in one line",
      said, ["  hardware: Supermicro SYS-1019D-14C-RD, serial S900001X1234567"])

said = []
r = Result()
hardware.attach(r, NOTHING, said.append)
check("a box with no serial is named, and said to have none",
      said, ["  hardware: ASUSTeK COMPUTER INC., no serial published"])
check("and nothing is written into the serial field", r.device_serial, "")

r = Result()
check("nothing found means nothing recorded",
      (hardware.attach(r, {}), r.extra), ("", {}))

print()
print("-- the serial reaches the map, and tells two boxes apart --")
from linkfinder.topology import Topology

t = Topology()
a = t.add_node(mgmt_ip="198.51.100.10", name="twin", serial="S900001X1234567")
b = t.add_node(mgmt_ip="198.51.100.11", name="twin", serial="S900002X7654321")
check("two machines of one model stay two devices", len(t.nodes), 2)
check("each keeping its own serial",
      sorted(n.serial for n in t.nodes.values()),
      ["S900001X1234567", "S900002X7654321"])
again = t.add_node(chassis_id="aa:bb:cc:dd:ee:01", name="twin",
                   serial="S900001X1234567")
check("and a box that answers under a second identity is folded into one",
      again.id, a.id)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all hardware checks passed")

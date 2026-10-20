"""What a MikroTik switch says about its own hardware.

Every line of input here was captured from a real CRS326 on a live bench,
including the two things that make this worth parsing at all: a module that
reports no optics because it is a copper SFP, and a power supply that has
failed while the switch carries on without a word.

Run: python tests/test_routeros.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.render_topology import _health_lines, _optics_lines, port_order
from linkfinder.routeros import (HEALTH_SCRIPT, PORTS_SCRIPT, parse_board,
                                 parse_health, parse_ports, rate_to_mbps)

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# Captured verbatim. The values are integers in smaller units - RouterOS
# returns them that way through `as-value`, which is the whole reason for
# asking it that way rather than reading the printed table.
PORTS = """
LFPORT~|~name=ether1~|~status=link-ok~|~rate=100Mbps~|~present=~|~vendor=~|~part=~|~serial=~|~revision=~|~made=~|~wavelength=~|~temperature=~|~voltage=~|~tx=~|~rx=~|~bias=~|~rxloss=~|~txfault=~|~connector=~|~type=~|~om1=~|~om2=~|~om3=~|~sm=
LFPORT~|~name=qsfpplus1-1~|~status=no-link~|~rate=~|~present=false~|~vendor=~|~part=~|~serial=~|~revision=~|~made=~|~wavelength=~|~temperature=~|~voltage=~|~tx=~|~rx=~|~bias=~|~rxloss=~|~txfault=~|~connector=~|~type=~|~om1=~|~om2=~|~om3=~|~sm=
LFPORT~|~name=sfp-sfpplus3~|~status=link-ok~|~rate=10Gbps~|~present=true~|~vendor=Intel Corp~|~part=FTLX8571D3BCV-I3~|~serial=ARK39GY~|~revision=A~|~made=14-05-12~|~wavelength=85000~|~temperature=52~|~voltage=3295~|~tx=-1901~|~rx=-1807~|~bias=8~|~rxloss=false~|~txfault=false~|~connector=LC~|~type=SFP/SFP+/SFP28/SFP56~|~om1=30~|~om2=80~|~om3=300~|~sm=0
LFPORT~|~name=sfp-sfpplus22~|~status=link-ok~|~rate=10Gbps~|~present=true~|~vendor=PROLABS~|~part=E10GSFPLR-I-NP~|~serial=PR2245431077~|~revision=A2~|~made=22-11-09~|~wavelength=131000~|~temperature=44~|~voltage=3283~|~tx=-2301~|~rx=-2500~|~bias=31~|~rxloss=false~|~txfault=false~|~connector=LC~|~type=SFP/SFP+/SFP28/SFP56~|~om1=0~|~om2=0~|~om3=0~|~sm=10000
LFPORT~|~name=sfp-sfpplus7~|~status=link-ok~|~rate=10Gbps~|~present=true~|~vendor=OEM~|~part=SFP-10G-T~|~serial=20241014020~|~revision=A~|~made=24-10-14~|~wavelength=85000~|~temperature=39~|~voltage=3327~|~tx=-3010~|~rx=-3979~|~bias=6~|~rxloss=true~|~txfault=false~|~connector=LC~|~type=SFP/SFP+/SFP28/SFP56~|~om1=20~|~om2=80~|~om3=300~|~sm=0
"""

optics, ports = parse_ports(PORTS)

print("-- the request asks RouterOS to walk its own ports --")
# `/interface/ethernet/monitor [find] once` lays ports out side by side in
# columns thousands of characters wide, with no way to tell which value
# belongs to which port. One line per port is the whole trick.
check("it prints one record per port", ":foreach i in=" in PORTS_SCRIPT, True)
check("and asks for values, not a printed table",
      "as-value" in PORTS_SCRIPT, True)
check("the health request is built the same way",
      ":foreach i in=[/system/health/find]" in HEALTH_SCRIPT, True)
check("neither quotes badly enough to leave an unbalanced string",
      PORTS_SCRIPT.count('"') % 2 + HEALTH_SCRIPT.count('"') % 2, 0)

print()
print("-- every port is listed, with or without a module in it --")
check("all five ports came back", sorted(ports, key=port_order),
      ["ether1", "qsfpplus1-1", "sfp-sfpplus3", "sfp-sfpplus7",
       "sfp-sfpplus22"])
check("a lit port carries its negotiated speed",
      ports["sfp-sfpplus3"]["speed"], "10000Mb/s")
check("and says it is up", ports["sfp-sfpplus3"]["carrier"], "up")
check("an empty cage is down with no speed",
      (ports["qsfpplus1-1"]["state"], ports["qsfpplus1-1"]["speed"]),
      ("down", ""))
check("a copper port is still a port", ports["ether1"]["speed"], "100Mb/s")
check("only the cages with a module in them have optics",
      sorted(optics, key=port_order),
      ["sfp-sfpplus3", "sfp-sfpplus7", "sfp-sfpplus22"])

print()
print("-- the numbers are scaled, and the words are left alone --")
# This is where it went wrong first: everything was run through a number
# reader, so FTLX8571D3BCV-I3 became 8571 and the serial ARK39GY became 39.
# A wrong serial is worse than none - somebody will search an inventory for it.
one = optics["sfp-sfpplus3"]
check("the part number survives intact", one["part"], "FTLX8571D3BCV-I3")
check("so does the serial", one["serial"], "ARK39GY")
check("and the date it was made", one["date_code"], "14-05-12")
check("wavelength is nanometres, whole", one["wavelength_nm"], 850)
check("voltage is volts", one["voltage_v"], 3.295)
check("transmit power is dBm", one["tx_dbm"], -1.901)
check("receive power is dBm", one["rx_dbm"], -1.807)
check("temperature is plain degrees", one["temperature_c"], 52)
check("a multimode module names its OM3 reach", one["reach_om3_m"], 300)

far = optics["sfp-sfpplus22"]
check("a long-reach module names kilometres, not metres",
      far["reach_smf_km"], 10)
check("at its own wavelength", far["wavelength_nm"], 1310)
check("and a module with no single-mode reach claims none",
      "reach_smf_km" in one, False)

print()
print("-- a module complaining is not dropped because the link is up --")
# Loss of receive with the link still reported up is the thing only the
# module can say, and the one most worth saying.
check("loss of receive is kept", optics["sfp-sfpplus7"].get("rx_loss"), True)
check("a healthy module claims no fault",
      ("rx_loss" in one, "tx_fault" in one), (False, False))

print()
print("-- the panel line reads as a line, not as a record --")
lines = _optics_lines(optics, ports)
check("rack order, not alphabetical",
      [l.split()[0] for l in lines],
      ["sfp-sfpplus3", "sfp-sfpplus7", "sfp-sfpplus22"])
check("a multimode module names its fibre and its link",
      lines[0], "sfp-sfpplus3   850 nm 300 m OM3, up at 10G, Intel Corp")
check("and a long-reach one its distance",
      lines[2], "sfp-sfpplus22  1310 nm 10 km, up at 10G, PROLABS")

print()
print("-- ports sort the way a person reads a rack --")
# A 24-port switch sorted as text runs 1, 10, 11, ... 2, 20 - which is not
# an order anybody looks for a port in.
check("numbers compare as numbers",
      sorted(["sfp-sfpplus10", "sfp-sfpplus2", "sfp-sfpplus1"], key=port_order),
      ["sfp-sfpplus1", "sfp-sfpplus2", "sfp-sfpplus10"])
check("and a name with no number is still sorted",
      sorted(["ether1", "bridge", "sfp-sfpplus1"], key=port_order),
      ["bridge", "ether1", "sfp-sfpplus1"])

print()
print("-- a failed power supply is said out loud --")
# Captured from the bench: this switch has been running on one of its two
# supplies, saying nothing about it anywhere.
HEALTH = """
LFHEALTH~|~sfp-temperature~|~57~|~C
LFHEALTH~|~switch-temperature~|~53~|~C
LFHEALTH~|~fan-state~|~ok~|~
LFHEALTH~|~fan1-speed~|~4875~|~RPM
LFHEALTH~|~psu1-state~|~ok~|~
LFHEALTH~|~psu2-state~|~fail~|~
"""
health = parse_health(HEALTH)
check("every reading came back", len(health), 6)
check("a number keeps its unit",
      (health["switch-temperature"]["value"],
       health["switch-temperature"]["unit"]), (53, "C"))
check("a healthy state is marked well", health["psu1-state"]["ok"], True)
check("and a failed one is not", health["psu2-state"]["ok"], False)
check("a reading with a number is never called unwell",
      health["fan1-speed"]["ok"], True)

said = _health_lines(health)
check("temperatures share one line",
      said[0], "temperature    sfp 57 C, switch 53 C")
# A box with two boards names them board-temperature1 and -2, which an exact
# list of names left in a heap at the bottom under no heading at all.
check("however many of them the box has",
      _health_lines({"board-temperature1": {"value": 42, "unit": "C", "ok": True},
                     "board-temperature2": {"value": 35, "unit": "C", "ok": True}}),
      ["temperature    board1 42 C, board2 35 C"])
check("fans share another", said[1].startswith("fans"), True)
check("and the supplies a third", said[2], "power          psu1 ok, psu2 fail")
check("the failure is then repeated in words nobody can miss",
      any("running on what is left" in l for l in said), True)
check("a healthy box gets no such line",
      [l for l in _health_lines({"psu1-state": {"value": "ok", "unit": "",
                                                "ok": True}})
       if "running on what is left" in l], [])
check("and nothing at all when there is nothing to say", _health_lines({}), [])

print()
print("-- RouterOS 6 keeps health somewhere else entirely --")
# v6 has one record of flat properties and no list to walk, so the script
# that works on v7 returns nothing at all rather than failing. Measured
# against the documentation: v7 is NAME/VALUE/TYPE rows, v6 is properties.
from linkfinder.routeros import parse_health_printed

OLD = """
      routerboard: yes
          voltage: 24.0V
      temperature: 45C
   cpu-temperature: 52C
"""
older = parse_health_printed(OLD)
check("the flat form is read too", sorted(older),
      ["cpu-temperature", "temperature", "voltage"])
check("the unit is split off the number",
      (older["voltage"]["value"], older["voltage"]["unit"]), (24, "V"))
check("a decimal survives",
      parse_health_printed("  voltage: 23.9V")["voltage"]["value"], 23.9)
check("and the board's own yes/no is not a reading",
      "routerboard" in older, False)

print()
print("-- a port named with a vertical bar does not split in two --")
# Real port names on a live switch: "sfp28-1 (UL-IT)", "sfp28-2(Router)".
# Whoever racks it types what they like, so the separator has to be
# something nobody types.
from linkfinder.routeros import MARK

AWKWARD = ("LFPORT" + MARK + "name=sfp28-1 (UL|IT)" + MARK + "status=link-ok"
           + MARK + "rate=25Gbps" + MARK + "present=false")
_o, awkward = parse_ports(AWKWARD)
check("the name arrives whole", sorted(awkward), ["sfp28-1 (UL|IT)"])
check("and its speed with it",
      awkward["sfp28-1 (UL|IT)"]["speed"], "25000Mb/s")
check("the separator is not a character anybody types", MARK, "~|~")

print()
print("-- the board names itself, in the shape the serial reader takes --")
BOARD = """
routerboard: yes
      model: CRS326-24S+2Q+
   revision: r3
serial-number: HER097B3FQS
  firmware-type: qca9531L
current-firmware: 7.23.7
"""
board = parse_board(BOARD)
check("the serial is where the reader looks for it",
      board["product_serial"], "HER097B3FQS")
check("the model is the product name", board["product_name"], "CRS326-24S+2Q+")
check("the maker is filled in, because RouterOS does not say it",
      board["sys_vendor"], "MikroTik")
check("and nothing is invented from an empty answer", parse_board(""), {})

print()
print("-- a rate is translated once, where it can be tested --")
for text, want in (("10Gbps", "10000Mb/s"), ("100Mbps", "100Mb/s"),
                   ("1Gbps", "1000Mb/s"), ("", ""), ("no-link", "")):
    check("%r reads as %r" % (text, want), rate_to_mbps(text), want)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all RouterOS checks passed")

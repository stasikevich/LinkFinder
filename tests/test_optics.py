"""Transceivers: reading the module, and comparing the two ends of a fibre.
Run: python tests/test_optics.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor, TargetResult
from linkfinder.optics import (MULTI_MODE, SINGLE_MODE, check, collect,
                               diagnostics, label, match_optics, parse_ethtool_m)

FAILURES = []


def check_that(label_text, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label_text, got, want))
    print("%-64s %s" % (label_text, "ok" if got == want else "FAIL"))


LR = """	Identifier                                : 0x03 (SFP)
	Connector                                 : 0x07 (LC)
	Transceiver type                          : 10G Ethernet: 10G Base-LR
	Transceiver type                          : Ethernet: 1000BASE-LX
	Encoding                                  : 0x06 (64B/66B)
	BR, Nominal                               : 10300MBd
	Length (SMF,km)                           : 10km
	Length (OM3)                              : 0m
	Laser wavelength                          : 1310nm
	Vendor name                               : Intel Corp
	Vendor PN                                 : FTLX1471D3BCVI31
	Vendor SN                                 : A13A3XF
	Date code                                 : 190122
"""

SR = """	Identifier                                : 0x03 (SFP)
	Transceiver type                          : 10G Ethernet: 10G Base-SR
	Length (SMF,km)                           : 0km
	Length (OM3)                              : 300m
	Laser wavelength                          : 850nm
	Vendor PN                                 : AFBR-709DMZ-IN3
"""

WITH_DOM = SR + """	Module temperature                        : 34.52 degrees C
	Module voltage                            : 3.2860 V
	Laser output power                        : -2.3010 dBm
	Rcvr signal avg optical power             : -3.8120 dBm
"""

GIG_COPPER = """	Identifier                                : 0x03 (SFP)
	Transceiver type                          : Ethernet: 1000BASE-T
	Length (Copper)                           : 100m
	Vendor PN                                 : GLC-T
"""

print("-- reading what ethtool decoded --")
lr = parse_ethtool_m(LR)
check_that("the fastest type decides what the port is", lr["form"], "LR")
check_that("not the 1G one it also speaks", lr["speed_gbps"], 10)
check_that("single-mode follows from the form", lr["media"], SINGLE_MODE)
check_that("wavelength is a number", lr["wavelength_nm"], 1310)
check_that("so is the reach", lr["reach_smf_km"], 10)
check_that("vendor and part survive", (lr["vendor"], lr["part"]),
           ("Intel Corp", "FTLX1471D3BCVI31"))
check_that("the identifier loses its hex", lr["identifier"], "SFP")
check_that("both advertised types are kept", len(lr["types"]), 2)
check_that("one line a person reads", label(lr), "10G-LR, 1310 nm, 10 km")

sr = parse_ethtool_m(SR)
check_that("short reach is multimode", sr["media"], MULTI_MODE)
check_that("and reads in metres of OM3", label(sr), "10G-SR, 850 nm, 300 m OM3")

copper = parse_ethtool_m(GIG_COPPER)
check_that("1000BASE-T is copper", copper["media"], "copper")
check_that("and one gigabit", copper["speed_gbps"], 1)

check_that("an empty read gives an empty record", parse_ethtool_m(""), {})
check_that("so does a line with no colon", parse_ethtool_m("nothing here"), {})

print()
print("-- diagnostics, when the module has any --")
check_that("a module without DOM says so plainly", diagnostics(lr),
           "this module reports no diagnostics")
dom = parse_ethtool_m(WITH_DOM)
check_that("receive power is read", dom["rx_dbm"], -3.812)
check_that("transmit power too", dom["tx_dbm"], -2.301)
check_that("and the temperature", dom["temperature_c"], 34.52)
check_that("reported together", diagnostics(dom),
           "RX -3.812 dBm, TX -2.301 dBm, 34.52 C")

print()
print("-- comparing the two ends --")
check_that("the same optic at both ends", check(lr, lr)["verdict"], "match")
check_that("so does SR to SR", check(sr, sr)["verdict"], "match")

crossed = check(lr, sr)
check_that("single-mode facing multimode is the hard fault",
           crossed["verdict"], "mismatch")
check_that("named as what it is",
           crossed["detail"], "single-mode against multimode - the fibre types "
                              "do not meet")
check_that("at high confidence", crossed["confidence"], "high")
check_that("with both sides as evidence", len(crossed["evidence"]), 2)

speed_gap = check(parse_ethtool_m(LR), parse_ethtool_m(
    LR.replace("10G Ethernet: 10G Base-LR", "25G Ethernet: 25G Base-LR")))
check_that("a speed difference is a mismatch too", speed_gap["verdict"], "mismatch")

far = parse_ethtool_m(LR.replace("1310nm", "1550nm"))
check_that("1310 against 1550 is a mismatch", check(lr, far)["verdict"], "mismatch")
near = parse_ethtool_m(LR.replace("1310nm", "1330nm"))
check_that("but 20 nm of vendor rounding is not",
           check(lr, near)["verdict"], "match")

lrm = parse_ethtool_m(SR.replace("10G Base-SR", "10G Base-LRM"))
same_fibre = check(sr, lrm)
check_that("same fibre, different optic is a warning not a fault",
           same_fibre["verdict"], "warn")
check_that("at medium confidence", same_fibre["confidence"], "medium")

print()
print("-- what we do not know, we do not claim --")
check_that("remote missing", check(lr, None)["verdict"], "unknown")
check_that("and it says which side", check(lr, None)["detail"],
           "remote optics unknown")
check_that("local missing", check(None, sr)["detail"], "local optics unknown")
check_that("neither side", check(None, None)["detail"],
           "no transceiver data on either side")
check_that("a copper port with no reach data is not a mismatch",
           check(copper, copper)["verdict"], "match")

print()
print("-- collecting from a host --")
SHELL = ("OPTIC eno1\n" + LR + "ENDOPTIC\n"
         "OPTIC eno2\n" + SR + "ENDOPTIC\n")
found = collect(lambda _cmd: SHELL)
check_that("one record per port", sorted(found), ["eno1", "eno2"])
check_that("and they are parsed", found["eno1"]["form"], "LR")
check_that("a host with no modules yields nothing", collect(lambda _cmd: ""), {})


def explodes(_cmd):
    raise OSError("ssh died")


check_that("a dead shell is not an error", collect(explodes), {})

print()
print("-- matching the ends across a run --")


def host(target, name, iface, module, peer=None):
    result = TargetResult(target=target, kind="linux")
    result.device_name = name
    if module:
        result.extra["optics"] = {iface: parse_ethtool_m(module)}
    nb = Neighbor(host=target, local_iface=iface)
    if peer:
        nb.extra["ptp_peer_ports"] = [list(peer)]
    result.neighbors.append(nb)
    return result


good = [host("198.51.100.10", "node-a", "f0", LR, peer=("node-b", "f3")),
        host("198.51.100.11", "node-b", "f3", LR, peer=("node-a", "f0"))]
match_optics(good)
check_that("a matched pair is reported ok",
           good[0].neighbors[0].extra["checks"]["optics"]["verdict"], "match")
check_that("from both ends",
           good[1].neighbors[0].extra["checks"]["optics"]["verdict"], "match")
check_that("and the module is kept on the row",
           good[0].neighbors[0].extra["optics"]["form"], "LR")

bad = [host("198.51.100.10", "node-a", "f0", LR, peer=("node-b", "f3")),
       host("198.51.100.11", "node-b", "f3", SR, peer=("node-a", "f0"))]
said = []
match_optics(bad, log=said.append)
check_that("a crossed pair is caught",
           bad[0].neighbors[0].extra["checks"]["optics"]["verdict"], "mismatch")
check_that("and announced once per end", len(said), 2)
check_that("naming the port", "node-a f0" in said[0], True)

lonely = [host("198.51.100.10", "node-a", "f0", LR, peer=("sw-core-1", "Eth1/1"))]
match_optics(lonely)
check_that("a far end we never scanned stays unknown",
           lonely[0].neighbors[0].extra["checks"]["optics"]["verdict"], "unknown")
check_that("with the reason", lonely[0].neighbors[0].extra["checks"]["optics"]["detail"],
           "remote optics unknown")

bare = [host("198.51.100.10", "node-a", "f0", "", peer=("node-b", "f3"))]
match_optics(bare)
check_that("a host with no modules at all is left alone",
           "optics" in (bare[0].neighbors[0].extra.get("checks") or {}), False)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all optics checks passed")

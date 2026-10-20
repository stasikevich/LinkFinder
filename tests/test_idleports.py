"""Checks that a lit cable carrying nothing is reported.

The case from the bench: a bond lost legs, stayed up on the last one, and
three 10G ports of the same card sat up with good light in no bond, no
bridge and no address. Nothing anywhere said so - the ports were dropped
from the port table on the way out of the probe, because the rule assumed a
port with no address was already drawn as a link. It is drawn as a link only
if something was heard on it, and nothing was.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.render_topology import _bond_lines, device_facts, idle_lit

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


PORTS = {
    "eno1": {"role": "mgmt", "carrier": "up", "addresses": ["10.0.0.5/24"]},
    "enp1s0f0np0": {"role": "bond-slave", "carrier": "up", "addresses": []},
    "enp1s0f1np1": {"role": "physical", "carrier": "up", "addresses": [],
                    "speed": "10000Mb/s"},
    "enp1s0f2np2": {"role": "physical", "carrier": "up", "addresses": [],
                    "speed": "10000Mb/s"},
    "ens2f0": {"role": "bridge-member", "carrier": "up", "addresses": []},
    "ens3f0": {"role": "physical", "carrier": "", "addresses": []},
    "ens4f0": {"role": "physical", "carrier": "up", "addresses": ["172.16.0.1/24"]},
}

print("-- only a lit port that belongs to nothing is named --")
check("the two idle ones, and no others", idle_lit(PORTS),
      ["enp1s0f1np1", "enp1s0f2np2"])
check("a bond leg is working, not idle", "enp1s0f0np0" in idle_lit(PORTS), False)
check("so is a bridge member", "ens2f0" in idle_lit(PORTS), False)
check("a port with no cable is not lit", "ens3f0" in idle_lit(PORTS), False)
check("a port with an address carries something",
      "ens4f0" in idle_lit(PORTS), False)
check("and a host with nothing idle says nothing", idle_lit(
    {"eno1": PORTS["eno1"]}), [])

print()
print("-- the bond names the ports sitting beside it --")
lines = _bond_lines({"bond0": ["enp1s0f0np0"]},
                    {"bond0": {"mode": "IEEE 802.3ad Dynamic link aggregation",
                               "legs": [{"name": "enp1s0f0np0", "mii": "up"}],
                               "ad_partner_mac": "0c:97:5f:af:fa:40"}},
                    PORTS)
said = " ".join(lines)
check("it says how many legs are left", "the bond has 1 leg" in said, True)
check("and names the lit ports of the same card",
      "enp1s0f1np1, enp1s0f2np2" in said, True)
check("without the awkward plural", "leg(s)" in said, False)

print()
print("-- a healthy bond beside nothing idle stays quiet --")
quiet = _bond_lines({"bond0": ["enp1s0f0np0"]},
                    {"bond0": {"mode": "IEEE 802.3ad Dynamic link aggregation",
                               "legs": [{"name": "enp1s0f0np0", "mii": "up"}],
                               "ad_partner_mac": "0c:97:5f:af:fa:40"}},
                    {"eno1": PORTS["eno1"]})
check("no sentence about lit ports",
      "lit and in nothing" in " ".join(quiet), False)

print()
print("-- the panel grows a section for them, with the light --")
node = {"id": "ip:10.0.0.5", "name": "bench", "kind": "host",
        "state": "scanned", "mgmt_ips": ["10.0.0.5"], "chassis_id": "",
        "serial": "", "setups": [], "depth": 0, "sources": [], "error": "",
        "extra": {"ports": PORTS,
                  # Without these every port reads as software plumbing and
                  # the address section is empty - which is the real rule,
                  # not a quirk of this fixture.
                  "macs": {name: "aa:bb:cc:00:00:%02d" % n
                           for n, name in enumerate(sorted(PORTS))},
                  "optics": {"enp1s0f1np1": {"rx_dbm": -2.46,
                                             "part": "CLSFPP8510GSR"}}}}
titles = [s["title"] for s in device_facts(node)]
check("the section is there", "Cabled and carrying nothing" in titles, True)
body = " ".join(line for s in device_facts(node)
                if s["title"] == "Cabled and carrying nothing"
                for line in s["lines"])
check("the light is on the line", "-2.46 dBm" in body, True)

print()
print("-- and a port with no address is not called an address --")
addresses = " ".join(line for s in device_facts(node)
                     if s["title"] == "Addresses it answers on"
                     for line in s["lines"])
check("'no address' is gone from that heading",
      "no address" in addresses, False)
check("the addressed ports are still there", "10.0.0.5/24" in addresses, True)

print()
print("-- a leg that keeps dropping is said out loud --")
# From a live bench: two legs at 14 and 17 link failures, both currently up,
# so every report called the bond healthy. Healthy legs sit at 0 or 1.
sick = _bond_lines({"bond0": ["ens1f0", "ens1f1"]},
                   {"bond0": {"mode": "load balancing (xor)",
                              "legs": [{"name": "ens1f0", "mii": "up",
                                        "failures": "14"},
                                       {"name": "ens1f1", "mii": "up",
                                        "failures": "17"}]}}, {})
check("it is named as a finding, not only counted",
      "dropped and came back repeatedly" in " ".join(sick), True)
check("with the legs and how often",
      "ens1f0 14 times" in " ".join(sick), True)

well = _bond_lines({"bond0": ["ens1f0"]},
                   {"bond0": {"mode": "load balancing (xor)",
                              "legs": [{"name": "ens1f0", "mii": "up",
                                        "failures": "1"}]}}, {})
check("one blip in a lifetime is not a fault",
      "repeatedly" in " ".join(well), False)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all idle-port checks passed")

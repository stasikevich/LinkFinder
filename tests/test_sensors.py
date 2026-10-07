"""Card temperature, and not pretending it belongs to one port.
Run: python tests/test_sensors.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor, TargetResult
from linkfinder.sensors import Card, apply, card_for, collect, parse

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-64s %s" % (label, "ok" if got == want else "FAIL"))


SYSFS = """NIC|eno1|0000:05:00.0||||
NIC|enp23s0f0|0000:17:00.0|ice|43000|95000|105000
NIC|enp23s0f1|0000:17:00.1||||
NIC|enp23s0f2|0000:17:00.2||||
NIC|enp23s0f3|0000:17:00.3||||
NIC|eno2|0000:06:00.0|i350bb|35000|120000|110000
NIC|eno3|0000:06:00.1||||
"""

print("-- one sensor, four ports --")
cards = parse(SYSFS)
check("cards are keyed by slot, not by port", sorted(cards),
      ["0000:06:00", "0000:17:00"])
ice = cards["0000:17:00"]
check("the chip is named", ice.chip, "ice")
check("millidegrees become degrees", ice.celsius, 43)
check("so does the limit", ice.max_celsius, 95)
check("and the critical point", ice.critical_celsius, 105)
check("every function of the card is listed", sorted(ice.interfaces),
      ["enp23s0f0", "enp23s0f1", "enp23s0f2", "enp23s0f3"])
check("a port with no sensor of its own is not a card",
      "0000:05:00" in cards, False)
check("the description says whose temperature it is",
      ice.describe(),
      "ice (0000:17:00): 43 C of 95 max - shared by enp23s0f0, enp23s0f1, "
      "enp23s0f2, enp23s0f3")
check("any port of the card finds it", card_for(cards, "enp23s0f3").chip, "ice")
check("a port on no card finds nothing", card_for(cards, "eno1"), None)
check("nothing in, nothing out", parse(""), {})
check("junk in, nothing out", parse("not a line at all"), {})

print()
print("-- judged against the card's own limit, never a number we chose --")
check("well under is fine", ice.verdict()["verdict"], "match")
warm = Card(chip="ice", slot="s", celsius=96, max_celsius=95,
            critical_celsius=105, interfaces=["f0"])
check("at the limit is a warning", warm.verdict()["verdict"], "warn")
nearly = Card(chip="ice", slot="s", celsius=96, max_celsius=100,
              critical_celsius=100, interfaces=["f0"])
check("within ten percent of it is also a warning",
      nearly.verdict()["verdict"], "warn")
check("but only at medium confidence", nearly.verdict()["confidence"], "medium")
check("and the detail quotes both numbers",
      "limit of 100" in nearly.verdict()["detail"], True)
check("the evidence names the sysfs file",
      "hwmon temp1_input" in nearly.verdict()["evidence"][0], True)

no_limit = Card(chip="x", slot="s", celsius=80, interfaces=["f0"])
check("a card with no published limit is not judged",
      no_limit.verdict()["verdict"], "unknown")
check("and says why",
      "publishes no limit" in no_limit.verdict()["detail"], True)
check("a card with no reading at all", Card(chip="x", slot="s").verdict()["verdict"],
      "unknown")

print()
print("-- collecting --")
check("one read of sysfs", sorted(collect(lambda _c: SYSFS)),
      ["0000:06:00", "0000:17:00"])
said = []
collect(lambda _c: SYSFS, log=said.append)
check("each card with a reading is announced once", len(said), 2)


def explodes(_cmd):
    raise OSError("ssh died")


check("a host that cannot be read is not an error", collect(explodes), {})

print()
print("-- the reading reaches every port that shares the card --")
result = TargetResult(target="198.51.100.10", kind="linux")
result.device_name = "node-a"
result.extra["sensors"] = {slot: card.to_dict() for slot, card in cards.items()}
for iface in ("eno1", "enp23s0f0", "enp23s0f2"):
    result.neighbors.append(Neighbor(host=result.target, local_iface=iface))
apply([result])
by_iface = {nb.local_iface: nb for nb in result.neighbors}
check("a port on the sensing card gets it",
      by_iface["enp23s0f0"].extra["nic_card"]["celsius"], 43)
check("so does its sibling with no sensor of its own",
      by_iface["enp23s0f2"].extra["nic_card"]["celsius"], 43)
check("and both are told it is shared",
      len(by_iface["enp23s0f2"].extra["nic_card"]["interfaces"]), 4)
check("a port on a card with no sensor gets nothing",
      "nic_card" in by_iface["eno1"].extra, False)
check("the verdict lands as a check",
      by_iface["enp23s0f0"].extra["checks"]["temperature"]["verdict"], "match")

bare = TargetResult(target="198.51.100.11", kind="linux")
bare.neighbors.append(Neighbor(host=bare.target, local_iface="eth0"))
apply([bare])
check("a host with no sensors at all is left alone",
      "checks" in bare.neighbors[0].extra, False)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all sensor checks passed")

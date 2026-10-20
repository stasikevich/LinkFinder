"""Checks for lining up a switch port with the record of it.

The dangerous failure is not "no answer": it is a confident wrong answer,
because somebody will act on it and unplug the wrong cable. So most of what
is checked here is the refusal to guess.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import switchports as sp

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- the number at the end is what identifies a port --")
check("a stacked name", sp.port_number("1/1/49"), 49)
check("a vendor abbreviation", sp.port_number("Te0/9"), 9)
check("an inventory name", sp.port_number("Port52"), 52)
check("a module name", sp.port_number("sfp-sfpplus5"), 5)
check("and a name with no number at all", sp.port_number("uplink"), None)

print()
print("-- the real shapes this estate uses --")
RECORDED = ["Port%d" % n for n in range(1, 53)]
plan = sp.propose("sw1", ["1/1/49", "1/1/7"], RECORDED)
check("both ports land", sorted(plan["matched"].values()), ["Port49", "Port7"])
check("the stem is read off the records", plan["stem"], "Port")
check("one yes covers the whole switch", plan["covers"], 52)
check("and it says so plainly", plan["standing"], sp.CERTAIN)

lower = sp.propose("sw2", ["1/1/12"], ["port%d" % n for n in range(1, 52)])
check("a switch recorded in lower case is no different",
      lower["matched"]["1/1/12"], "port12")

print()
print("-- it refuses rather than guesses --")
check("nothing recorded, nothing proposed",
      sp.propose("sw3", ["1/1/1"], []), None)
check("nothing seen, nothing proposed",
      sp.propose("sw3", [], RECORDED), None)
check("a port whose number matches two records is not matched",
      sp.propose("sw4", ["1/1/5"], ["Port5", "Eth5"]), None)
mixed = sp.propose("sw5", ["1/1/5", "uplink"], RECORDED)
check("a port with no number is reported, not invented",
      mixed["unmatched"], ["uplink"])
check("and that lowers the standing", mixed["standing"] != sp.CERTAIN, True)
check("a stem is only claimed when every record agrees",
      sp.shape_of(["Port1", "Eth2"]), "")

print()
print("-- nothing resolves until a person has said yes --")
rule = sp.propose("sw1", ["1/1/49"], RECORDED)
check("an unconfirmed rule answers nothing",
      sp.resolve(rule, "1/1/49", RECORDED), "")
saved = sp.remember({}, "sw1", rule)["switch_ports"]["sw1"]
check("confirming is what the defaults file records", saved["confirmed"], True)
check("and then it answers", sp.resolve(saved, "1/1/49", RECORDED), "Port49")
check("including a port it never saw, because the rule covers the switch",
      sp.resolve(saved, "1/1/23", RECORDED), "Port23")
check("but still not a port with no number",
      sp.resolve(saved, "uplink", RECORDED), "")

print()
print("-- only confirmed switches are judged --")
INVENTORY = {"sw1": {"ports": RECORDED, "connected": {"serverA": "Port12"}}}
LINKS = [{"switch": "sw1", "port": "1/1/13", "device": "serverA"}]
check("with no confirmed rule there is no disagreement to report",
      sp.disagreements(LINKS, {}, INVENTORY), [])
rules = sp.confirmed_rules({"switch_ports": {"sw1": saved}})
found = sp.disagreements(LINKS, rules, INVENTORY)
check("with one, the moved cable is named", len(found), 1)
check("and both sides are given",
      (found[0]["scan_says"], found[0]["netbox_says"]), ("Port13", "Port12"))
agree = sp.disagreements([{"switch": "sw1", "port": "1/1/12",
                           "device": "serverA"}], rules, INVENTORY)
check("a cable that agrees is not reported", agree, [])

print()
print("-- an unconfirmed rule in the file is not treated as confirmed --")
check("confirmed_rules ignores a proposal",
      sp.confirmed_rules({"switch_ports": {"sw9": {"confirmed": False}}}), {})
check("and survives a file written by hand with rubbish in it",
      sp.confirmed_rules({"switch_ports": "not a table"}), {})

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all switch-port checks passed")

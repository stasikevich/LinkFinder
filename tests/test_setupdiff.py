"""Checks for comparing one bench with what is recorded against it.

The thing being prevented is a confident wrong pairing: two devices joined
because their names rhyme. Most of what follows is the refusal to do that,
and the insistence that a weak match is labelled as one.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import setupdiff as sd

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def record(name, serial="", status="Active", addresses=None):
    return {"name": name, "serial": serial, "status": {"label": status},
            "addresses": addresses or []}


def node(name, serial="", ips=None):
    return {"id": "ip:" + (ips or ["0.0.0.0"])[0], "name": name,
            "serial": serial, "mgmt_ips": ips or []}


print("-- a serial settles it --")
rows = sd.compare("bench", [record("5G SERVER", "S907083X6310614")],
                  [node("bench-a", "S907083X6310614", ["10.0.0.1"])])
check("one row, matched", (len(rows), rows[0]["verdict"]), (1, sd.IN_BOTH))
check("by the serial", rows[0]["matched by"], sd.BY_SERIAL)
check("with nothing to confirm", rows[0]["needs a look"], "")
check("and high confidence", rows[0]["confidence"], sd.HIGH)

print()
print("-- a serial hidden in the record's name counts too --")
rows = sd.compare("bench", [record("I400-0019 FHGW - 050321107250092")],
                  [node("fhgw", "050321107250092")])
check("it is found", rows[0]["verdict"], sd.IN_BOTH)
check("and said plainly", rows[0]["matched by"], sd.BY_SERIAL_IN_NAME)

print()
print("-- the product family in front of a name is a hint, not a fact --")
rows = sd.compare("bench", [record("MM-0-n78LC-00")],
                  [node("Integrity-MM-0-n78LC-00")])
check("the pair is offered", rows[0]["verdict"], sd.IN_BOTH)
check("the reason is given", rows[0]["matched by"], sd.BY_NAME_TAIL)
check("it is marked as needing a look", rows[0]["needs a look"], "yes")
check("and is not sold as certain", rows[0]["confidence"], sd.LOW)

print()
print("-- it will not pair two things that merely rhyme --")
check("a short tail is not evidence",
      sd._tail_match("sw1", "core-sw1"), False)
check("nor is an identical name treated as a tail",
      sd._tail_match("server-a", "server-a"), False)
check("a real family prefix is", sd._tail_match("MM-0-n78LC-00",
                                                "Integrity-MM-0-n78LC-00"), True)
rows = sd.compare("bench", [record("Vaunix 35754")], [node("bench-a")])
check("two unrelated devices stay apart",
      sorted(r["verdict"] for r in rows), [sd.ONLY_HERE, sd.ONLY_NETBOX])

print()
print("-- a short serial is not a fingerprint --")
rows = sd.compare("bench", [record("rack 12", "12")], [node("thing", "12")])
check("it still matches on an exact serial", rows[0]["verdict"], sd.IN_BOTH)
rows = sd.compare("bench", [record("rack 12")], [node("thing", "12")])
check("but is never looked for inside a name",
      rows[0]["verdict"] != sd.IN_BOTH, True)

print()
print("-- strong evidence is never outbid by weak --")
# The tail match would grab the wrong record if order decided it.
rows = sd.compare("bench",
                  [record("MM-0-n78LC-00"), record("other", "SER123")],
                  [node("Integrity-MM-0-n78LC-00", "SER123")])
paired = [r for r in rows if r["verdict"] == sd.IN_BOTH]
check("the serial wins the device", len(paired), 1)
check("and it is the record that proves it",
      paired[0]["netbox name"], "other")

print()
print("-- both absences are reported, and which side they are on --")
rows = sd.compare("bench",
                  [record("recorded only", "A1"), record("shared", "B2")],
                  [node("shared", "B2"), node("here only", "C3")])
kinds = {r["verdict"]: r for r in rows}
check("the one only NetBox has", kinds[sd.ONLY_NETBOX]["netbox name"],
      "recorded only")
check("says what was tried, rather than accusing the lab",
      "no serial, name or address" in kinds[sd.ONLY_NETBOX]["needs a look"],
      True)
check("the one only we found", kinds[sd.ONLY_HERE]["found as"], "here only")
check("says it is not recorded",
      "not recorded" in kinds[sd.ONLY_HERE]["needs a look"], True)
check("and the summary counts all three",
      sd.summary(rows), "1 in both, 1 only in netbox, 1 only here")

print()
print("-- nothing marked here is a different thing from nothing there --")
only_them = sd.compare("bench", [record("a", "A1"), record("b", "B2")], [])
check("it does not read as two missing devices",
      "mark this bench first" in sd.summary(only_them), True)
check("but the rows are still there to look at", len(only_them), 2)
mixed = sd.compare("bench", [record("a", "A1"), record("b", "B2")],
                   [node("a", "A1")])
check("and a real absence still counts as one",
      sd.summary(mixed), "1 in both, 1 only in netbox")

print()
print("-- the spreadsheet carries every column --")
text = sd.to_csv(rows)
check("a header and a row per finding",
      len(text.strip().split(chr(10))), len(rows) + 1)
check("the columns are the ones promised",
      text.split(chr(10))[0], ",".join(sd.COLUMNS))

print()
print("-- a bench is only the devices put in it --")
topology = {"nodes": [node("a", ips=["10.0.0.1"]), node("b", ips=["10.0.0.2"])]}
topology["nodes"][0]["setups"] = ["bench-a"]
topology["nodes"][1]["setups"] = ["other bench"]
check("only its own devices are taken",
      [n["name"] for n in sd.in_setup(topology, "bench-a")], ["a"])
check("and the name is not case sensitive",
      len(sd.in_setup(topology, "bench-a")), 1)
check("an empty bench is empty, not everything",
      sd.in_setup(topology, "nothing here"), [])


print()
print("-- a class this tool cannot enter is not a missing device --")
GEAR = [record("the chamber", "CH-1"), record("a server", "SRV-1")]
GEAR[0]["role"] = {"name": "RF Chamber"}
GEAR[1]["role"] = {"name": "Server"}
blind = sd.compare("bench", GEAR, [node("a server", "SRV-1")])
by_name = {r["netbox name"]: r for r in blind}
check("without being told, it is reported like any other absence",
      by_name["the chamber"]["verdict"], sd.ONLY_NETBOX)
check("and the role is shown, which explains most rows on its own",
      by_name["the chamber"]["netbox role"], "RF Chamber")
offered = sd.roles_never_matched(blind)
check("the class is offered, with how many there are",
      offered, [("RF Chamber", 1)])
check("a class that did match is never offered",
      [r for r, _n in offered if r == "Server"], [])

told = sd.compare("bench", GEAR, [node("a server", "SRV-1")],
                  out_of_reach=["rf chamber"])
by_name = {r["netbox name"]: r for r in told}
check("once told, it stops being reported as missing",
      by_name["the chamber"]["verdict"], sd.CANNOT_CHECK)
check("and says why in a way nobody acts on wrongly",
      "no way into a RF Chamber" in by_name["the chamber"]["needs a look"],
      True)
check("the real device is untouched",
      by_name["a server"]["verdict"], sd.IN_BOTH)

print()
print("-- the name here and the tag there are allowed to differ --")
check("a pair is split", sd.split_name("stand by the window=bench-a"),
      ("stand by the window", "bench-a"))
check("a plain name has no tag of its own", sd.split_name("bench-a"),
      ("bench-a", ""))
check("and then the tag is the name, tidied",
      sd.tag_for("Bench 7"), "bench-7")
check("but a named pairing always wins",
      sd.tag_for("Bench 7", {"Bench 7": "the-old-tag"}), "the-old-tag")

print()
print("-- the classes somebody confirmed are remembered --")
kept = sd.remember_out_of_reach({}, ["RF Chamber", "CPE"])
check("written into the defaults file",
      kept["out_of_reach_roles"], ["CPE", "RF Chamber"])
check("and read back", sd.out_of_reach(kept), ["CPE", "RF Chamber"])
check("saying the same one twice does not double it",
      sd.remember_out_of_reach(kept, ["cpe"])["out_of_reach_roles"],
      ["CPE", "RF Chamber"])
# A confirmation nobody can undo is a trap: one wrong yes and a real
# absence is hidden for ever.
check("a class can be taken back with a minus",
      sd.remember_out_of_reach(kept, ["-cpe"])["out_of_reach_roles"],
      ["RF Chamber"])
check("taking back one that was never there changes nothing",
      sd.remember_out_of_reach(kept, ["-nothing"])["out_of_reach_roles"],
      ["CPE", "RF Chamber"])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all setup-comparison checks passed")

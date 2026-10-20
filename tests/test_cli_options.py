"""Checks that the command line still offers what the product promises.

Every flag here is one somebody is told to type. A refactor that quietly
removed four of them left the whole suite green and was only found by running
the real binary against the real lab - so the flags are named here, one by one,
and a missing one fails loudly.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.cli import build_parser

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


parser = build_parser()
OFFERED = set()
for action in parser._actions:
    OFFERED.update(action.option_strings)

PROMISED = [
    # getting in
    "-u", "--user", "-p", "--password", "-i", "--key", "--ssh-port",
    "--sudo-password", "--cred", "--targets-file",
    # what to ask
    "-m", "--mode", "--iface", "--mgmt-only", "-t", "--seconds",
    "--no-ptp", "--no-identify", "--no-neighbour-probe", "--adb",
    # the switch side
    "--switch", "--community", "--switch-user", "--switch-password", "--mac",
    "--port-health", "--port-health-interval", "--expect-speed",
    "--all-interfaces",
    # the map
    "--topology", "--max-depth", "--max-devices", "--scope", "--auto-discover",
    "--topology-json", "--topology-html", "--render-topology",
    "--merge-topology", "--topology-resume", "--topology-workers",
    "--compare-scan", "--diff", "--watch", "--watch-state", "--loop-check",
    # the inventory
    "--netbox", "--netbox-url", "--netbox-token", "--netbox-insecure",
    "--netbox-tags", "--netbox-proposals", "--setup-tag",
    # output and housekeeping
    "--json", "--csv", "-w", "-q", "--gui", "-V", "--version",
    "--learn", "--list-switches", "--forget-switches", "--remember-credentials",
]

print("-- every flag the documentation tells somebody to type --")
missing = [flag for flag in PROMISED if flag not in OFFERED]
check("none of them has gone missing", missing, [])

print()
print("-- and a full command line still parses --")
args = parser.parse_args([
    "--topology", "198.51.100.1", "-u", "root", "-p", "secret",
    "--ssh-port", "2222", "--adb", "--max-depth", "2",
    "--netbox", "--netbox-url", "https://nb.example",
    "--netbox-token", "nbt_key.secret", "--netbox-insecure",
    "--netbox-tags", "--netbox-proposals", "out.json",
    "--setup-tag", "dio", "--setup-tag", "bench-c",
    "--topology-json", "a.json", "--topology-html", "a.html", "-t", "30"])
check("the seed is taken", args.topology, "198.51.100.1")
check("the login is taken", (args.user, args.password), ("root", "secret"))
check("a non-standard SSH port is taken", args.ssh_port, 2222)
check("asking adb is taken", args.adb, True)
check("NetBox is asked for", args.netbox, True)
check("with its address and token", (args.netbox_url, bool(args.netbox_token)),
      ("https://nb.example", True))
check("an internal CA can be allowed", args.netbox_insecure, True)
check("the tag list is asked for", args.netbox_tags, True)
check("proposals go somewhere nameable", args.netbox_proposals, "out.json")
check("setup tags are repeatable", args.setup_tag, ["dio", "bench-c"])
check("and the capture window is taken", args.seconds, 30)

print()
print("-- the defaults a person relies on --")
plain = parser.parse_args(["198.51.100.10"])
check("SSH port 22 unless told otherwise", plain.ssh_port, 22)
check("NetBox is off", plain.netbox, False)
check("adb is off - it can start a server on the host", plain.adb, False)
check("nothing is a setup until it is named", plain.setup_tag, [])
check("PTP is read by default", plain.ptp, True)
check("the neighbour probe is on, as he chose", plain.neighbours, True)
check("switches are not remembered unless asked", plain.learn, False)
check("the capture window is 90s", plain.seconds, 90)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all command-line checks passed")

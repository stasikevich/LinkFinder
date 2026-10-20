"""Start the binary that is about to ship and make it do its work.

    python tools/binary_runs.py

Everything else about the build inspects the binary: that the whole
program is inside it, that nobody's settings came with it. Nothing has
ever *run* it. The difference is not theoretical - three link builders
once shipped missing, and the binary started perfectly, drew a map and
simply left those links out.

So the built file is asked to do the things it can do with no network and
no lab: say its version, list its options, merge two maps, compare them,
and write the table. A map is invented here rather than kept in the tree,
which keeps a real estate out of the repository and makes the fixture say
plainly what it is testing.

This is the opposite end from tools/smoke.py, which needs a real host and
is deliberately not in the build. This one needs nothing, so it is.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

# Documentation addresses, so nothing here can ever name a real machine.
BEFORE = {
    "schema_version": 1, "generated_at": "2026-03-04T09:00:00",
    "seed": "192.0.2.10", "seeds": ["192.0.2.10"], "extra": {},
    "stopped_because": "",
    "nodes": [
        {"id": "ip:192.0.2.10", "kind": "host", "name": "alpha",
         "chassis_id": "", "chassis_id_raw": "", "mgmt_ips": ["192.0.2.10"],
         "vendor": "", "state": "scanned", "depth": 0, "sources": [],
         "serial": "SN0000000A", "setups": [], "first_seen": "2026-03-04",
         "last_seen": "2026-03-04", "error": "", "extra": {}},
        {"id": "chassis:00:00:5e:00:53:01", "kind": "switch", "name": "sw-one",
         "chassis_id": "00:00:5e:00:53:01", "chassis_id_raw": "",
         "mgmt_ips": ["192.0.2.2"], "vendor": "", "state": "unscanned",
         "depth": 1, "sources": [], "serial": "", "setups": [],
         "first_seen": "2026-03-04", "last_seen": "2026-03-04",
         "error": "", "extra": {}}],
    "edges": [
        {"id": "e1", "a": "ip:192.0.2.10", "a_iface": "eno1",
         "b": "chassis:00:00:5e:00:53:01", "b_iface": "Gi0/1",
         "protos": ["LLDP"], "sources": ["test"], "reported_by": [],
         "confidence": "reported", "vlan": "", "speed": "1000Mb/s",
         "attrs": {}, "problems": [], "first_seen": "2026-03-04",
         "last_seen": "2026-03-04"}],
}


def _after():
    """The same place a week later, with the cable moved one port along."""
    import copy

    held = copy.deepcopy(BEFORE)
    held["generated_at"] = "2026-03-11T09:00:00"
    held["edges"][0]["b_iface"] = "Gi0/9"
    return held


def binaries():
    """The command-line binary of each brand, wherever the build left it."""
    for name in ("linkfinder.exe", "linkfinder-pw.exe",
                 "linkfinder", "linkfinder-pw"):
        path = os.path.join(DIST, name)
        if os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
            yield path


def run(path, args, timeout=120):
    done = subprocess.run([path] + args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def main(argv=None) -> int:
    found = list(binaries())
    if not found:
        print("no command-line binary built yet - nothing to run")
        return 0
    where = tempfile.mkdtemp(prefix="lf-binary-runs-")
    one = os.path.join(where, "before.json")
    two = os.path.join(where, "after.json")
    with open(one, "w", encoding="utf-8") as handle:
        json.dump(BEFORE, handle)
    with open(two, "w", encoding="utf-8") as handle:
        json.dump(_after(), handle)
    merged = os.path.join(where, "merged.json")
    table = os.path.join(where, "merged.csv")

    # (what it proves, the arguments, something the answer must contain,
    #  the exit codes that count as working)
    #
    # --diff answers 1 when it found changes, the way diff(1) does, so a
    # scheduled job can act on it. Demanding 0 everywhere would have made
    # this check fail on the tool working correctly.
    WORK = [
        ("it starts and names itself", ["--version"], "licence", (0,)),
        ("it lists what it can be asked", ["--help"], "--topology", (0,)),
        ("it merges two maps", ["--merge-topology", one, two,
                                "--topology-json", merged], "", (0,)),
        ("it says what moved between them", ["--diff", one, two],
         "Gi0/9", (0, 1)),
        ("it writes the table", ["--merge-topology", one,
                                 "--topology-csv", table,
                                 "--topology-json", merged], "", (0,)),
        ("it says where it keeps things", ["--where"], "", (0,)),
    ]
    bad = []
    for path in found:
        short = os.path.relpath(path, ROOT)
        for what, args, must, fine in WORK:
            try:
                rc, said = run(path, args)
            except Exception as problem:
                bad.append("%s: %s raised %s" % (short, what, problem))
                continue
            if rc not in fine:
                bad.append("%s: %s exited %d - %s"
                           % (short, what, rc, said.strip()[-200:]))
            elif must and must not in said:
                bad.append("%s: %s said nothing about %r"
                           % (short, what, must))
        if os.path.isfile(table):
            with open(table, encoding="utf-8") as handle:
                rows = [r for r in handle.read().splitlines() if r.strip()]
            if len(rows) < 2:
                bad.append("%s: the table it wrote has no rows" % short)
            os.remove(table)
        else:
            bad.append("%s: it wrote no table at all" % short)
        print("ran %d thing(s) through %s" % (len(WORK), short))

    if bad:
        print("%d thing(s) the built binary cannot do:" % len(bad))
        for line in bad:
            print("  " + line)
        return 1
    print("every built binary starts and does its work")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The gate that keeps anybody's settings out of the product.

It was passing on everything. A string written in our own source ends up
zlib-compressed inside the binary, so reading the raw bytes found nothing
and every build looked clean - a real NetBox token baked into a shipped
asset would have gone straight out of the door.

It now opens the archive and reads what is actually in there. Which means
this file has one job above all others: prove it can still fail. A gate
whose failure nobody has ever seen is a gate nobody should trust.

It also checks the opposite mistake. The first version matched the bare
word `nbt_`, which fired on the help page explaining what a token looks
like, and a gate that goes off at its own documentation is a gate someone
quietly switches off.

Nothing here needs a build to have happened - without one there is simply
nothing to look inside, and that is reported rather than called a pass.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))

import no_settings as gate

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- it matches a secret, not a word about one --")
# Straight out of the shipped help page, which documents the shape of a
# token and the defaults file. None of this is anybody's data.
DOCUMENTED = [
    b"On NetBox 4.5 that is nbt_&lt;key&gt;.&lt;secret&gt; - the secret half",
    b'{\n  "user": "root",\n  "password": "...",\n  "bmc_user": "ADMIN"}',
    b'{"user": "root", "password": "secret"}',
    b"<code>netbox.setup_tags</code> in the defaults file",
]
for text in DOCUMENTED:
    fired = [what for shape, what in gate.NEVER_INSIDE if shape.search(text)]
    check("left alone: %s" % text[:38].decode("ascii", "replace"), fired, [])

print()
print("-- and a real one does not get through --")
# Shaped like the real thing, invented here. The point of each line is
# that the rule above it has something it genuinely catches.
REAL = [
    (b'{"netbox": {"token": "nbt_AbCdEfGhIjKl.MnOpQrStUvWx"}}',
     "a NetBox API token"),
    (b'{"setup_tags": ["dio", "ozzy"]}', "somebody's setup tags"),
    (b'{"netbox": {"url": "https://netbox.example.net/", "verify": true}}',
     "a saved NetBox address"),
]
for text, expected in REAL:
    fired = [what for shape, what in gate.NEVER_INSIDE if shape.search(text)]
    check("caught: %s" % expected, expected in fired, True)

print()
print("-- it can see inside a packaged binary, not just its outside --")
dist = os.path.join(HERE, "dist")
binaries = list(gate._binaries(dist)) if os.path.isdir(dist) else []
if not binaries:
    print("no build to look inside - nothing to check here")
else:
    # PROOF is a word that exists only in our own Python, which is stored
    # compressed inside a second archive carried by the first. Finding it
    # means both were opened. The control used to be "ipmitool", which is
    # also a word in the shipped help page - so it was found, the gate
    # looked satisfied, and every Python module was still unread.
    kept = gate.NEVER_INSIDE
    try:
        gate.NEVER_INSIDE = ((re.compile(gate.PROOF), "the positive control"),)
        reached = gate.inside_binaries(dist)
    finally:
        gate.NEVER_INSIDE = kept
    check("compressed content is reachable", bool(reached), True)

    raw = open(binaries[0], "rb").read()
    check("and the raw bytes alone would have missed it",
          gate.PROOF in raw, False)

    print()
    print("-- a gate that cannot look says so, rather than passing --")
    unpack = gate._unpacked
    try:
        gate._unpacked = lambda path: iter(())
        blind = gate.inside_binaries(dist)
    finally:
        gate._unpacked = unpack
    check("silence is reported as a failure",
          any("could not read inside" in line for line in blind), True)

    print()
    print("-- and it reaches our own code, not just the files beside it --")
    # Across every binary, not the first one alphabetically: dist can hold
    # an older build that predates whatever the control is, and pinning the
    # check to it turns a real result into a coin toss.
    reached = {}
    for path in binaries:
        for name, blob in gate._unpacked(path):
            if gate.PROOF in blob:
                reached["proof"] = name
            # A rule written as a tuple of tuples is one constant, not a
            # string. Walking only strings walked straight past the lot.
            if b"neighbour cache search" in blob:
                reached["nested"] = name
    check("a string from a module is found", "proof" in reached, True)
    check("and it came from the bundled Python, not an asset",
          ".pyz" in str(reached.get("proof", "")), True)
    check("including one nested inside a tuple of rules",
          "nested" in reached, True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all settings-gate checks passed")

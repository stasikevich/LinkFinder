"""Checks for turning a range of addresses into addresses.

The fault being fixed was silence: a network typed as a target produced no
targets at all and said nothing, so a person watched an empty scan and
blamed the network.

What must hold: every form a person writes is opened out, a range too large
to sweep is refused with a number, and nothing is ever dropped quietly.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.engine import targets_with_logins
from linkfinder.ranges import LIMIT, expand, expand_all, looks_like_a_range

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- the three ways a person writes a range --")
check("a network", expand("198.51.100.0/30")[0],
      ["198.51.100.1", "198.51.100.2"])
check("a run, written out",
      expand("198.51.100.5-198.51.100.7")[0],
      ["198.51.100.5", "198.51.100.6", "198.51.100.7"])
check("the same run, written short", expand("198.51.100.5-7")[0],
      ["198.51.100.5", "198.51.100.6", "198.51.100.7"])
check("a single address is not a range",
      looks_like_a_range("198.51.100.5"), False)
check("and a hostname is not either", looks_like_a_range("a-switch"), False)

print()
print("-- a sweep too big to start is refused, with the number --")
hosts, why = expand("10.0.0.0/8")
check("nothing is handed back", hosts, [])
check("and the reason carries the count", "16777216" in why, True)
check("as well as the limit", str(LIMIT) in why, True)
hosts, why = expand("198.51.100.9-198.51.100.2")
check("a backwards range is refused too", hosts, [])
check("in words a person can act on", "runs backwards" in why, True)

print()
print("-- what the caller is told --")
out, opened, refused = expand_all(["198.51.100.0/30", "a-switch",
                                   "10.0.0.0/8"])
check("the addresses come out", out,
      ["198.51.100.1", "198.51.100.2", "a-switch"])
check("the ranges that opened are named", len(opened), 1)
check("and so is the one that did not", len(refused), 1)

print()
print("-- the scan sees them, because they open before anything else --")
said = []
got, _per = targets_with_logins(["198.51.100.0/30"], "", log=said.append)
check("a network becomes targets", got,
      ["198.51.100.1", "198.51.100.2"])
check("and the log says what it did",
      any("range:" in line for line in said), True)

said = []
got, _per = targets_with_logins(["10.0.0.0/8"], "", log=said.append)
check("a refused sweep yields nothing", got, [])
check("but never silently",
      any("more than the" in line for line in said), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all range checks passed")

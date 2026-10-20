"""The order evidence is believed in, and the fact that it is kept once.

Two callers used to hold their own copy of this list, and the copies had
already drifted: the crawl knew about PTP and a plain host list did not,
so the same facts drew two different maps. A list that must not drift
needs a test that notices when it does.

It is also where a worse mistake was made. The list first held module
names as strings and reached them with __import__, which works perfectly
from source and shipped a binary with three of the builders missing -
PyInstaller finds what a program imports by reading the source, and a
name assembled at run time is invisible to it. Every ARP link and every
PTP link was gone from the product while every suite stayed green. So
this checks the rungs are real functions, imported the ordinary way.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import ladder
from linkfinder.topology import Topology

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def named(step):
    """Which module and function a rung is, from the function itself."""
    return (getattr(step, "__module__", "").rsplit(".", 1)[-1],
            getattr(step, "__name__", ""))


print("-- every rung is a function this file imported, not a name --")
# The whole point: a bundler can see `from . import arplinks` and cannot
# see "arplinks" sitting in a tuple.
check("nothing in the list is a string",
      [r for _l, r, _v in ladder.RUNGS if isinstance(r, str)], [])
check("and every one of them can be called",
      [l for l, step, _v in ladder.RUNGS if not callable(step)], [])
with open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "linkfinder", "ladder.py"),
        encoding="utf-8") as handle:
    src = handle.read()
check("the modules are reached by a plain import statement",
      "from . import arplinks" in src, True)
check("and not by a name put together at run time",
      "__import__(" in src, False)

print()
print("-- the strong evidence comes first --")
order = [named(step) for _label, step, _views in ladder.RUNGS]
place = {pair: n for n, pair in enumerate(order)}

check("a tap carrying the guest's own address is believed first",
      place[("vmlinks", "connect")], 0)
check("ptp is believed before an IPv6 neighbour",
      place[("ptp_links", "connect")] < place[("neighbor6", "connect")], True)
check("an IPv6 neighbour before an ARP entry",
      place[("neighbor6", "connect")] < place[("arplinks", "connect")], True)
check("and the lacp partner after the cables",
      place[("arplinks", "connect")] < place[("lag", "connect_partners")],
      True)

print()
print("-- what reads the finished map comes after what adds to it --")
last_builder = max(n for n, (_l, _s, views) in enumerate(ladder.RUNGS) if views)
readers = [n for n, (_l, step, views) in enumerate(ladder.RUNGS)
           if not views and named(step)[1] in ("confirm_from_map", "find", "fold")]
check("every map reader runs after the last builder",
      [n for n in readers if n < last_builder], [])
check("every rung carries a name to say when it is skipped",
      [label for label, _s, _v in ladder.RUNGS if not str(label).strip()], [])

print()
print("-- neither caller keeps a copy of the list --")
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for name in ("crawl.py", "cli.py"):
    with open(os.path.join(here, "linkfinder", name), encoding="utf-8") as f:
        text = f.read()
    check("%s walks the ladder" % name, "ladder import walk" in text, True)
    strayed = [bad for bad in ("from .neighbor6 import connect",
                               "from .arplinks import connect",
                               "from .ptp_links import connect",
                               "from .lag import connect_partners",
                               "from .lag import fold",
                               "from .ipmi import confirm_from_map",
                               "from .consistency import find")
               if bad in text]
    check("%s reaches for no builder of its own" % name, strayed, [])

print()
print("-- a rung that throws is reported, and the rest still run --")
# A guard rail nobody has seen fail is a guard rail nobody should trust.
said = []
kept = ladder.RUNGS


def explode(topology, views=None, log=None):
    raise RuntimeError("on purpose")


try:
    ladder.RUNGS = (("the first made-up rung", explode, True),
                    ("the second one", explode, False))
    ladder.walk(Topology(), [], log=said.append)
finally:
    ladder.RUNGS = kept

check("the broken rung is named out loud",
      any("the first made-up rung skipped" in line for line in said), True)
check("and the walk carried on to the next one",
      any("the second one skipped" in line for line in said), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all ladder checks passed")

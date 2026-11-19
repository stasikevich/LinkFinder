"""The Windows capture drops a neighbour that is this host's own LLDP echo.

pktmon listens to every adapter at once, so it also hears a frame this host
puts on the wire and takes it for a neighbour - drawing the host cabled to
itself. A neighbour whose chassis id is a MAC the host owns is that echo, not
a link, and is dropped - the same guard the Linux probe has for a bridge echo.

Run: python tests/test_probe_windows.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.model import Neighbor, TargetResult
from linkfinder.probe_windows import _drop_self_echo

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-60s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def chassis_set(result):
    return sorted(n.chassis_id for n in result.neighbors)


def test_self_echo_is_dropped_real_switch_kept():
    r = TargetResult(target="198.51.100.9", kind="windows")
    r.extra["macs"] = {"Ethernet 4": "00:e0:4c:68:00:05"}      # this host's NIC
    r.add(Neighbor(chassis_id="00:e0:4c:68:00:05", local_iface="x"))  # the echo
    r.add(Neighbor(chassis_id="ec:50:aa:1f:a7:40", switch="sw", port="1/1/34"))
    _drop_self_echo(r, lambda _m: None)
    check("the host's own MAC as a neighbour is dropped",
          "00:e0:4c:68:00:05" in chassis_set(r), False)
    check("the real switch neighbour is kept",
          "ec:50:aa:1f:a7:40" in chassis_set(r), True)


def test_mac_format_does_not_matter():
    # own MAC written with dashes, the echo's chassis with colons - normalise
    # folds them, so the echo is still caught.
    r = TargetResult(target="198.51.100.9", kind="windows")
    r.extra["macs"] = {"eth": "00-E0-4C-68-00-05"}
    r.add(Neighbor(chassis_id="00:e0:4c:68:00:05"))
    _drop_self_echo(r, lambda _m: None)
    check("different spellings of the same MAC still match", len(r.neighbors), 0)


def test_nothing_owned_drops_nothing():
    r = TargetResult(target="198.51.100.9", kind="windows")
    r.add(Neighbor(chassis_id="ec:50:aa:1f:a7:40", switch="sw"))
    _drop_self_echo(r, lambda _m: None)
    check("with no own MACs known, nothing is dropped", len(r.neighbors), 1)


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("\nFAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("\nall windows self-echo checks passed")

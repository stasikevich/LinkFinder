"""A Mini-Circuits RF switch is a device on the map, read over its HTTP API.

The box has no shell; a plain SSH probe sees nothing. Its HTTP API answers a
few read-only queries, and MN?/SN? name it well enough to draw it and hold it
against NetBox by serial. The parser is fed captured replies - no network - and
the one trap is checked: the box echoes its model inside an "unknown command"
error too, which must NOT read as a clean identification.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import minicircuits as mc

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def test_value_strips_echo():
    check("MN value", mc._value("MN=RC-1SP6T-A12"), "RC-1SP6T-A12")
    check("SN value", mc._value("SN=02210310149"), "02210310149")
    check("an error has no value", mc._value("-99 unknown command MN:RC-1SP6T-A12"), "")


def test_looks_like_switch():
    check("a clean model is a switch", mc.looks_like_switch("MN=RC-2SP6T-A12"), True)
    check("an ordinary web box is not", mc.looks_like_switch("Server"), False)
    # the box leaks its model in the unknown-command error; that must not count
    check("the error echo is not an identification",
          mc.looks_like_switch("-99 unknown command MN:RC-1SP6T-A12 SN:02210310149"), False)


def _fake(replies):
    return lambda cmd: replies.get(cmd, "")


def test_read_a_switch():
    get = _fake({"MN?": "MN=RC-1SP6T-A12", "SN?": "SN=02210310149",
                 "FIRMWARE?": "F5-2", "SWPORT?": "1"})
    out = mc.read(get)
    check("maker", out["maker"], "Mini-Circuits")
    check("model", out["model"], "RC-1SP6T-A12")
    check("serial - the key NetBox is held against", out["serial"], "02210310149")
    check("firmware", out["firmware"], "F5-2")
    check("the live switched port", out["switch_port"], "1")


def test_read_a_zero_port_is_kept():
    # port 0 is a real state, not "missing" - it must survive
    out = mc.read(_fake({"MN?": "MN=RC-2SP6T-A12", "SWPORT?": "0"}))
    check("switch port 0 is kept", out.get("switch_port"), "0")


def test_not_a_switch_is_none():
    check("a web box that is not one returns None",
          mc.read(_fake({"MN?": "<html>Login</html>"})), None)
    check("an unknown-command error returns None",
          mc.read(_fake({"MN?": "-99 unknown command"})), None)


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("minicircuits: all checks passed")

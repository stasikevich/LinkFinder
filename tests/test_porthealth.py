"""A port can be up and sick: CRC, a duplex mismatch, a flap.

None of these show as "down", so they are read from the kernel's own counters
and flagged. A clean port must raise nothing (no false alarm on a healthy box),
and each real fault must be named.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import porthealth

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


SAMPLE = (
    "PH|eno1|1|full|3|0|0|0\n"      # healthy
    "PH|enp1|1|half|50|12|4|0\n"    # CRC + half duplex + flapping
    "PH|enp2|1|full|2|0|7|1\n"      # plain rx/tx errors, no CRC
    "PH|enp3|0|unknown|1|0|0|0\n"   # down, nothing to say
)


def test_parse_numbers():
    ports = porthealth.parse(SAMPLE)
    check("count", len(ports), 4)
    check("crc as int", ports["enp1"]["rx_crc_errors"], 12)
    check("changes as int", ports["enp1"]["carrier_changes"], 50)


def test_problems():
    pr = porthealth.problems(porthealth.parse(SAMPLE))
    by = {}
    for i in pr:
        by.setdefault(i["iface"], []).append(i["kind"])
    check("healthy port silent", by.get("eno1"), None)
    check("down port silent", by.get("enp3"), None)
    check("enp1 flags", sorted(by.get("enp1", [])), ["crc", "duplex", "flap"])
    check("enp2 plain errors only", by.get("enp2"), ["errors"])
    check("crc severity", [i["severity"] for i in pr if i["kind"] == "crc"], ["mismatch"])
    check("flap severity", [i["severity"] for i in pr if i["kind"] == "flap"], ["warn"])


def test_half_duplex_only_when_up():
    # half duplex on a port with no carrier is not a live mismatch
    pr = porthealth.problems(porthealth.parse("PH|x|0|half|1|0|0|0\n"))
    check("half duplex while down is silent", pr, [])


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("porthealth: all checks passed")

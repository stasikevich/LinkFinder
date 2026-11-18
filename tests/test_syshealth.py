"""The box can be sick while every cable is green, and must stay silent when fine.

A full root filesystem, memory gone to swap, a disk pinned busy, a drifted clock
or a unit that died at boot are all real and all invisible on the wire. Each must
be found and named; a healthy, even a busy, host must raise nothing. The noise
units systemd always lists (a run scope, the wait-online shim) must never count.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import syshealth

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# A healthy, loaded-but-fine box: 8 cores, some load, RAM free, disks under 80%,
# clock synced, one noise unit systemd always reports.
HEALTHY = (
    "SH|load|2.10 1.80 1.50 8\n"
    "SH|mem|16000000 9000000\n"
    "SH|swap|4000000 4000000\n"
    "SH|uptime|864000\n"
    "SH|fs|41|120000000000|240000000000|/|ext4\n"
    "SH|fs|12|50000000000|60000000000|/boot|ext4\n"
    "SH|clock|yes\n"
    "SH|unit|run-docker-abc.scope\n"
)

# A sick box: root 94% full, memory almost gone, swap nearly full, clock off, a
# real failed unit alongside a noise one.
SICK = (
    "SH|load|40.0 38.0 35.0 8\n"
    "SH|mem|16000000 300000\n"
    "SH|swap|4000000 100000\n"
    "SH|uptime|500\n"
    "SH|fs|94|5000000000|240000000000|/|ext4\n"
    "SH|fs|83|9000000000|60000000000|/var|ext4\n"
    "SH|clock|no\n"
    "SH|reboot|yes\n"
    "SH|unit|NetworkManager-wait-online.service\n"
    "SH|unit|chronyd.service\n"
)

IOSTAT = """Linux 5.15.0 (host)  11/18/26  _x86_64_  (8 CPU)

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           5.00    0.00    2.00    0.50    0.00   92.50

Device    r/s     w/s   rkB/s   wkB/s  %util
sda      1.00    2.00   10.00   20.00   3.00
nvme0n1  0.50    0.50    4.00    4.00   1.00

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           3.00    0.00    1.00   45.00    0.00   51.00

Device    r/s     w/s   rkB/s   wkB/s  %util
sda     50.00  120.00  900.00 2000.00   98.00
nvme0n1  0.50    0.50    4.00    4.00    2.00
"""


def test_parse_numbers():
    r = syshealth.parse(HEALTHY)
    check("load 5-min", r["load"]["5"], 1.8)
    check("cpu count", r["load"]["cpus"], 8)
    check("mem avail", r["mem"]["avail_kb"], 9000000)
    check("two filesystems", len(r["filesystems"]), 2)
    check("root percent", r["filesystems"][0]["percent"], 41)
    check("clock synced", r["clock_synced"], True)


def test_healthy_is_silent():
    check("no problems on a healthy box", syshealth.problems(syshealth.parse(HEALTHY)), [])


def test_sick_box_flags():
    pr = syshealth.problems(syshealth.parse(SICK))
    by = {}
    for p in pr:
        by.setdefault(p["kind"], []).append(p["severity"])
    check("root full is a mismatch", by.get("disk-space", [None])[0], "mismatch")
    check("var high is also flagged", "warn" in by.get("disk-space", []), True)
    check("memory critical is a mismatch", by.get("memory"), ["mismatch"])
    check("swap flagged", by.get("swap"), ["warn"])
    check("load pile-up flagged", by.get("load"), ["warn"])
    check("clock drift flagged", by.get("clock"), ["warn"])
    # the real failed unit is flagged, the wait-online noise one is not
    services = [p["what"] for p in pr if p["kind"] == "service"]
    check("real failed unit named", services, ["chronyd.service"])


def test_iostat_last_report():
    io = syshealth.parse_iostat(IOSTAT)
    check("iowait from last report", io["iowait"], 45.0)
    check("busiest device", io["busiest"]["device"], "sda")
    check("busiest util", io["busiest"]["util"], 98.0)


def test_iowait_problem():
    r = syshealth.parse(HEALTHY)
    r["io"] = syshealth.parse_iostat(IOSTAT)
    kinds = [p["kind"] for p in syshealth.problems(r)]
    check("high iowait flagged", "iowait" in kinds, True)


def test_unit_noise_filter():
    check("only real units survive",
          syshealth._real_units(["run-x.scope", "user@0.service",
                                 "systemd-networkd-wait-online.service", "docker.service"]),
          ["docker.service"])


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if FAILURES:
    print("FAIL (%d):" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("syshealth: all checks passed")

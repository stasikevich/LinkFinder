"""Is the machine itself healthy - not its links, the box underneath them.

A node can have every cable green and still be in trouble: a root filesystem
about to fill, memory exhausted into swap, a disk pinned at 100% busy, a clock
that has drifted off NTP, or a systemd unit that died at boot. None of that
shows on the wire, so it is read from the host's own /proc, df and systemctl -
all world-readable, nothing written - and from iostat when it happens to be
installed (its absence just leaves iowait unread). The rule is the overlay's
rule: flag only what a person would go and fix, and stay silent on a healthy
box - a server busy with real work is not a fault.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

Run = Callable[[str], str]

# One cheap read of everything /proc, df and systemctl expose. df -T names the
# filesystem type so pseudo-mounts can be dropped; the -x list keeps the rows to
# real, fillable filesystems. Each line is tagged so one parser sorts them.
SCRIPT = r"""
echo "SH|load|$(cat /proc/loadavg 2>/dev/null) $(nproc 2>/dev/null || echo 1)"
awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}/^SwapTotal:/{st=$2}/^SwapFree:/{sf=$2}
     END{printf "SH|mem|%d %d\nSH|swap|%d %d\n",t,a,st,sf}' /proc/meminfo 2>/dev/null
echo "SH|uptime|$(cut -d. -f1 /proc/uptime 2>/dev/null)"
df -P -T -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs \
   -x ramfs -x iso9660 -x fuse.snapfuse 2>/dev/null \
   | awk 'NR>1{gsub(/%/,"",$6); printf "SH|fs|%s|%s|%s|%s|%s\n",$6,$4,$3,$7,$2}'
echo "SH|clock|$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
[ -f /var/run/reboot-required ] || [ -f /run/reboot-required ] && echo "SH|reboot|yes"
systemctl --failed --no-legend --plain --all 2>/dev/null | awk '{print "SH|unit|"$1}'
"""

IOSTAT_CMD = "iostat -x -c 1 2 2>/dev/null"

# A unit that is "failed" on a healthy box and means nothing to chase: a
# transient run scope, the wait-online shim that fails when a link is down by
# design, a user slice. Named units outside this set are real.
_UNIT_NOISE = ("run-", "user@", "user-", "NetworkManager-wait-online",
               "systemd-networkd-wait-online")

# Thresholds, all deliberately high so a busy-but-fine box stays silent.
FS_FULL = 90          # a filesystem this full will cause an outage - mismatch
FS_HIGH = 80          # getting there - warn
MEM_CRIT = 3          # MemAvailable this low means OOM is imminent - mismatch
MEM_LOW = 8           # tight - warn
SWAP_LOW = 10         # swap almost gone while in use - thrashing risk - warn
LOAD_MULT = 4         # load5 this many times the core count - sustained pile-up
IOWAIT_HIGH = 30      # CPU spending this long waiting on disk - a bottleneck
UTIL_HIGH = 95        # a disk pinned this busy - the bottleneck's device


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse(text: str) -> Dict[str, Any]:
    """Turn the tagged block into one readings dict, numbers kept as numbers."""
    out: Dict[str, Any] = {"filesystems": [], "failed_units": []}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or parts[0] != "SH":
            continue
        tag = parts[1]
        if tag == "load" and len(parts) >= 3:
            nums = parts[2].split()
            if len(nums) >= 4:
                out["load"] = {"1": _float(nums[0]), "5": _float(nums[1]),
                               "15": _float(nums[2]), "cpus": _int(nums[-1])}
        elif tag in ("mem", "swap") and len(parts) >= 3:
            nums = parts[2].split()
            if len(nums) >= 2:
                out[tag] = {"total_kb": _int(nums[0]),
                            ("avail_kb" if tag == "mem" else "free_kb"): _int(nums[1])}
        elif tag == "uptime" and len(parts) >= 3:
            out["uptime_s"] = _int(parts[2])
        elif tag == "fs" and len(parts) >= 7:
            out["filesystems"].append({
                "percent": _int(parts[2]), "avail_b": _int(parts[3]),
                "size_b": _int(parts[4]), "mount": parts[5], "fstype": parts[6]})
        elif tag == "clock" and len(parts) >= 3:
            out["clock_synced"] = {"yes": True, "no": False}.get(parts[2].strip().lower())
        elif tag == "reboot":
            out["reboot_required"] = True
        elif tag == "unit" and len(parts) >= 3 and parts[2].strip():
            out["failed_units"].append(parts[2].strip())
    return out


def parse_iostat(text: str) -> Dict[str, Any]:
    """%iowait and the busiest device from the LAST report of `iostat -xc 1 2`.

    The first report is the since-boot average; the second is the live second,
    so only the last avg-cpu block and the device rows under it are read.
    """
    blocks = (text or "").split("avg-cpu:")
    if len(blocks) < 2:
        return {}
    last = "avg-cpu:" + blocks[-1]
    lines = last.splitlines()
    out: Dict[str, Any] = {}
    for i, line in enumerate(lines):
        if line.strip().startswith("avg-cpu:") and i + 1 < len(lines):
            cols = lines[i + 1].split()
            if len(cols) >= 4:
                out["iowait"] = _float(cols[3])
        cols = line.split()
        if len(cols) >= 2 and cols[0] not in ("Device", "avg-cpu:") and not line.startswith(" "):
            util = _float(cols[-1])
            dev = cols[0]
            if util is not None and (not out.get("busiest") or util > out["busiest"]["util"]):
                out["busiest"] = {"device": dev, "util": util}
    return out


def _real_units(units: List[str]) -> List[str]:
    return [u for u in units if not any(u.startswith(n) for n in _UNIT_NOISE)]


def problems(readings: Dict[str, Any]) -> List[Dict[str, str]]:
    """The findings worth a person's time; worst (mismatch) first."""
    found: List[Dict[str, str]] = []

    for fs in sorted(readings.get("filesystems", []),
                     key=lambda f: -(f.get("percent") or 0)):
        pct, mount = fs.get("percent"), fs.get("mount", "?")
        if not isinstance(pct, int):
            continue
        if pct >= FS_FULL:
            found.append({"what": mount, "kind": "disk-space", "severity": "mismatch",
                          "detail": "%s is %d%% full - it will stop writes soon" % (mount, pct)})
        elif pct >= FS_HIGH:
            found.append({"what": mount, "kind": "disk-space", "severity": "warn",
                          "detail": "%s is %d%% full" % (mount, pct)})

    mem = readings.get("mem") or {}
    mt, ma = mem.get("total_kb"), mem.get("avail_kb")
    if isinstance(mt, int) and mt > 0 and isinstance(ma, int):
        pct_avail = 100.0 * ma / mt
        if pct_avail < MEM_CRIT:
            found.append({"what": "memory", "kind": "memory", "severity": "mismatch",
                          "detail": "only %.0f%% memory available - OOM is imminent" % pct_avail})
        elif pct_avail < MEM_LOW:
            found.append({"what": "memory", "kind": "memory", "severity": "warn",
                          "detail": "memory tight - %.0f%% available" % pct_avail})

    sw = readings.get("swap") or {}
    swt, swf = sw.get("total_kb"), sw.get("free_kb")
    if isinstance(swt, int) and swt > 0 and isinstance(swf, int):
        if 100.0 * swf / swt < SWAP_LOW:
            found.append({"what": "swap", "kind": "swap", "severity": "warn",
                          "detail": "swap is nearly full - the host may be thrashing"})

    load = readings.get("load") or {}
    l5, cpus = load.get("5"), load.get("cpus")
    if isinstance(l5, float) and isinstance(cpus, int) and cpus > 0 and l5 >= cpus * LOAD_MULT:
        found.append({"what": "load", "kind": "load", "severity": "warn",
                      "detail": "load %.1f over %d core(s) - the run queue is piling up" % (l5, cpus)})

    io = readings.get("io") or {}
    iowait = io.get("iowait")
    if isinstance(iowait, float) and iowait >= IOWAIT_HIGH:
        busiest = io.get("busiest") or {}
        where = " (%s at %.0f%% busy)" % (busiest["device"], busiest["util"]) if busiest else ""
        found.append({"what": "io", "kind": "iowait", "severity": "warn",
                      "detail": "CPU waiting %.0f%% on disk I/O%s" % (iowait, where)})

    if readings.get("clock_synced") is False:
        found.append({"what": "clock", "kind": "clock", "severity": "warn",
                      "detail": "system clock is not synchronised to NTP"})

    for unit in _real_units(readings.get("failed_units", [])):
        found.append({"what": unit, "kind": "service", "severity": "warn",
                      "detail": "systemd unit %s has failed" % unit})

    return found


def collect(run: Run, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """System-health readings plus findings, or {} when nothing came back."""
    say = log or (lambda _m: None)
    readings = parse(run("sh -c " + _quote(SCRIPT)))
    if not readings.get("load") and not readings.get("filesystems"):
        return {}
    # iostat is optional: run it only when installed, and never let a missing or
    # slow iostat lose the rest of the readings.
    if (run("command -v iostat 2>/dev/null") or "").strip():
        io = parse_iostat(run(IOSTAT_CMD))
        if io:
            readings["io"] = io
    issues = problems(readings)
    if issues:
        readings["problems"] = issues
        say("  system health: %s" % "; ".join(i["detail"] for i in issues[:3]))
    return readings


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"

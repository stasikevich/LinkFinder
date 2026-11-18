"""Local disks and the RAID built from them, read from what the host exposes.

A map node is a physical machine, and its storage - the NVMe or SAS drives, the
controller in front of them and any RAID it or the kernel has built - belongs on
it as much as its optics or its serial. Everything here is a read:

* lsblk and /proc/mdstat ship with every Linux, so plain disks and software
  (md) RAID always come back;
* the RAID controller itself is found without any vendor tool - from lspci when
  it is there, and otherwise from the driver each SCSI host loads in sysfs - so
  even a host with no management CLI shows "there is a Smart Array here";
* the array's own layout (which disks, what level, healthy or rebuilding) needs
  the controller's CLI, and that is asked only when it is installed.

The output is one shape whatever found it, so one renderer serves every source
and a vendor added later needs no new field.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

Run = Callable[[str], str]

LSBLK_JSON = ("lsblk -J -b -d -o "
              "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,VENDOR 2>/dev/null")
LSBLK_PAIRS = ("lsblk -P -b -d -o "
               "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,VENDOR 2>/dev/null")
MDSTAT = "cat /proc/mdstat 2>/dev/null"
LSPCI = "lspci 2>/dev/null"
# The driver each SCSI host loaded - the surest controller hint on a box with
# no lspci. Read with the host name so a RAID driver can be named.
SCSI_DRIVERS = ("for h in /sys/class/scsi_host/*/proc_name; do "
                "printf '%s ' \"$(cat \"$h\" 2>/dev/null)\"; done 2>/dev/null")

# Drivers that mean a RAID/array controller sits in front of the disks. mpt3sas
# and ahci and nvme are plain HBAs, not RAID, and are deliberately absent.
RAID_DRIVERS = {
    "smartpqi": "HPE/Microchip Smart Array (PQI)",
    "hpsa": "HP Smart Array",
    "megaraid_sas": "Broadcom/LSI MegaRAID",
    "aacraid": "Adaptec/PMC RAID",
    "3w-9xxx": "3ware RAID", "3w-sas": "3ware RAID",
    "arcmsr": "Areca RAID", "mpt2sas": "LSI (IR RAID capable)",
}
_LSPCI_RAID = re.compile(
    r"(RAID bus controller|Smart Array|Smart Storage|MegaRAID|PERC|"
    r"Serial Attached SCSI controller.*(Smart|MegaRAID|PQI))", re.I)

# Management CLIs, per controller family. A box can be any vendor and any
# distro, so each family lists every name it ships under AND the absolute paths
# a package drops it in when it is not on PATH. Presence is what decides whether
# the array layout is read; absence is normal and leaves it unread.
HW_FAMILIES = (
    ("storcli", ("storcli64", "storcli", "perccli64", "perccli"),
     ("/opt/MegaRAID/storcli/storcli64", "/opt/MegaRAID/perccli/perccli64",
      "/opt/dell/perccli/perccli64", "/usr/sbin/storcli64",
      "/usr/sbin/perccli64", "/usr/sbin/storcli")),
    ("ssacli", ("ssacli", "hpssacli", "hpacucli"),
     ("/usr/sbin/ssacli", "/opt/smartstorageadmin/ssacli/bin/ssacli",
      "/usr/sbin/hpssacli", "/opt/hp/hpssacli/bld/hpssacli",
      "/usr/sbin/hpacucli", "/opt/compaq/hpacucli/bld/hpacucli")),
    ("megacli", ("MegaCli64", "MegaCli", "megacli"),
     ("/opt/MegaRAID/MegaCli/MegaCli64", "/usr/sbin/MegaCli64",
      "/usr/sbin/megacli")),
    ("arcconf", ("arcconf",),
     ("/usr/sbin/arcconf", "/usr/Arcconf/arcconf",
      "/opt/pmc/arcconf/arcconf")),
)

# Model strings a controller gives a logical drive instead of a real product.
_LOGICAL_MODELS = ("logical volume", "logicaldrive", "ld ", "virtual disk")

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(n: Optional[int]) -> str:
    try:
        size = float(n)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            return "%d B" % int(size) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024
    return ""


def _media(bus: str, rota: Any, model: str) -> str:
    if any(tag in (model or "").lower() for tag in _LOGICAL_MODELS):
        return "logical"
    if (bus or "").lower() == "nvme":
        return "nvme"
    rotational = str(rota).strip()
    if rotational in ("0", "False", "false"):
        return "ssd"
    if rotational in ("1", "True", "true"):
        return "hdd"
    return ""


def _disk_from_row(name, dtype, size, rota, tran, model, serial, vendor):
    name = (name or "").strip()
    if not name or (dtype or "").strip().lower() != "disk":
        return None            # rom, loop, part, lvm, md are not bare disks
    if name.startswith(("zram", "ram")):
        return None            # a RAM disk / compressed swap is not a drive
    try:
        size_bytes = int(str(size).strip() or 0)
    except ValueError:
        size_bytes = 0
    if size_bytes <= 0:
        return None            # an empty card reader is not a drive
    model = (model or "").strip()
    bus = (tran or "").strip().lower()
    cls = ("network" if bus in ("iscsi", "fc", "fcoe")
           else "removable" if bus == "usb" else "local")
    return {
        "name": name, "bus": bus, "media": _media(bus, rota, model),
        "size_bytes": size_bytes, "size": human_size(size_bytes),
        "model": model, "serial": (serial or "").strip(),
        "vendor": (vendor or "").strip(), "rotational": str(rota).strip(),
        "behind_raid": _media(bus, rota, model) == "logical", "class": cls,
    }


def parse_lsblk_json(text: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    out = []
    for row in data.get("blockdevices", []) or []:
        disk = _disk_from_row(row.get("name", ""), row.get("type", ""),
                              row.get("size", 0), row.get("rota", ""),
                              row.get("tran", ""), row.get("model", ""),
                              row.get("serial", ""), row.get("vendor", ""))
        if disk:
            out.append(disk)
    return out


_PAIR = re.compile(r'(\w+)="([^"]*)"')


def parse_lsblk_pairs(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in (text or "").splitlines():
        row = {k.lower(): v for k, v in _PAIR.findall(line)}
        if not row:
            continue
        disk = _disk_from_row(row.get("name", ""), row.get("type", ""),
                              row.get("size", 0), row.get("rota", ""),
                              row.get("tran", ""), row.get("model", ""),
                              row.get("serial", ""), row.get("vendor", ""))
        if disk:
            out.append(disk)
    return out


def detect_controllers(lspci_text: str, drivers_text: str) -> List[Dict[str, Any]]:
    """RAID/array controllers from lspci and the sysfs SCSI-host drivers.

    lspci names the card where it is installed; the driver list is the fallback
    for a host with no lspci and also confirms which family claimed it. A
    controller is reported once, with whatever names both sources gave.
    """
    found: List[Dict[str, Any]] = []
    for line in (lspci_text or "").splitlines():
        if _LSPCI_RAID.search(line):
            slot, _, rest = line.partition(" ")
            found.append({"slot": slot.strip(),
                          "model": rest.split(":", 1)[-1].strip() or rest.strip(),
                          "driver": "", "source": "lspci"})
    drivers = [d for d in (drivers_text or "").split() if d]
    for drv in sorted(set(drivers)):
        if drv in RAID_DRIVERS:
            if any(drv == c.get("driver") for c in found):
                continue
            hit = next((c for c in found if not c["driver"]), None)
            if hit:
                hit["driver"] = drv
            else:
                found.append({"slot": "", "model": RAID_DRIVERS[drv],
                              "driver": drv, "source": "driver"})
    return found


_MD_HEAD = re.compile(r"^(md\d+)\s*:\s*(\S+)\s+(\S+)\s+(.*)$")
_MD_MEMBER = re.compile(r"([A-Za-z0-9/]+)\[(\d+)\](\([FS]\))?")
_MD_STATE = re.compile(r"\[([U_]+)\]")
_MD_PROGRESS = re.compile(r"(recovery|resync|reshape|check)\s*=\s*([\d.]+%)")


def parse_mdstat(text: str) -> List[Dict[str, Any]]:
    out = []
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        head = _MD_HEAD.match(lines[i].strip())
        if not head:
            i += 1
            continue
        name, active, level, rest = head.groups()
        members = [{"name": m, "index": int(idx),
                    "state": {"(F)": "faulty", "(S)": "spare"}.get(flag or "", "")}
                   for m, idx, flag in _MD_MEMBER.findall(rest)]
        detail = lines[i + 1].strip() if i + 1 < len(lines) else ""
        health = _MD_STATE.search(detail)
        degraded = bool(health and "_" in health.group(1))
        window = " ".join(lines[i + 1:i + 3])
        prog = _MD_PROGRESS.search(window)
        out.append({
            "name": name, "level": level, "members": members,
            "state": active + (" degraded" if degraded else ""),
            "health": health.group(1) if health else "",
            "progress": "%s %s" % prog.groups() if prog else "",
        })
        i += 2
    return out


def parse_storcli(text: str) -> List[Dict[str, Any]]:
    """Virtual drives from storcli/perccli `... show all J` JSON output."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    out = []
    for ctrl in data.get("Controllers", []) or []:
        resp = ctrl.get("Response Data", ctrl) or {}
        cid = resp.get("Controller")
        for vd in resp.get("VD LIST") or resp.get("Virtual Drives") or []:
            level = str(vd.get("TYPE") or vd.get("RAID Level") or "").strip()
            out.append({
                "controller": "controller %s" % cid if cid is not None else "",
                "name": str(vd.get("DG/VD") or vd.get("Name") or "").strip(),
                "level": "raid" + level if level.isdigit() else level.lower(),
                "state": str(vd.get("State") or vd.get("Status") or "").strip(),
                "size": str(vd.get("Size") or "").strip(),
                "source": "storcli",
            })
    return out


# Smart Array P440ar in Slot 0 (Embedded)
_SSA_CTRL = re.compile(r"^(.*?)\s+in Slot\s+(\d+)", re.I)
# logicaldrive 1 (279.4 GB, RAID 1, OK)
_SSA_LD = re.compile(r"logicaldrive\s+(\S+)\s+\(([^,]+),\s*([^,]+),\s*([^)]+)\)", re.I)
# physicaldrive 1I:1:1 (..., SAS HDD, 300 GB, OK)
_SSA_PD = re.compile(r"physicaldrive\s+(\S+)\s+\(([^)]*)\)", re.I)


def parse_ssacli(text: str) -> List[Dict[str, Any]]:
    """HP/HPE Smart Array arrays from `ssacli ctrl all show config`.

    The output is an indented tree: a controller line, then arrays, then a
    logicaldrive line per volume (size, RAID level, status) with the
    physicaldrive members beneath it. Each logical drive becomes one array row.
    """
    out: List[Dict[str, Any]] = []
    ctrl = ""
    pending: Optional[Dict[str, Any]] = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        cm = _SSA_CTRL.match(line)
        if cm:
            ctrl = "%s slot %s" % (cm.group(1).strip(), cm.group(2))
            continue
        lm = _SSA_LD.search(line)
        if lm:
            if pending:
                out.append(pending)
            size, level, status = lm.group(2).strip(), lm.group(3).strip(), lm.group(4).strip()
            pending = {"controller": ctrl, "name": "logicaldrive %s" % lm.group(1),
                       "level": level.replace("RAID ", "raid").replace(" ", "").lower(),
                       "state": status, "size": size, "members": [], "source": "ssacli"}
            continue
        pm = _SSA_PD.search(line)
        if pm and pending is not None:
            bits = [b.strip() for b in pm.group(2).split(",")]
            pending["members"].append({"name": pm.group(1), "detail": ", ".join(bits)})
    if pending:
        out.append(pending)
    return out


# Virtual Drive: 0 (Target Id: 0) ... RAID Level : Primary-5, Secondary-0 ...
_MC_VD = re.compile(r"Virtual Drive\s*:\s*(\d+)", re.I)
_MC_LEVEL = re.compile(r"RAID Level\s*:\s*Primary-(\d+)", re.I)
_MC_SIZE = re.compile(r"^Size\s*:\s*(.+)$", re.I | re.M)
_MC_STATE = re.compile(r"State\s*:\s*(.+)", re.I)


def parse_megacli(text: str) -> List[Dict[str, Any]]:
    """Virtual drives from `MegaCli -LDInfo -Lall -aALL` (LSI/Dell, text)."""
    out: List[Dict[str, Any]] = []
    blocks = re.split(r"(?=Virtual Drive\s*:\s*\d+)", text or "")
    for block in blocks:
        vd = _MC_VD.search(block)
        if not vd:
            continue
        lvl = _MC_LEVEL.search(block)
        size = _MC_SIZE.search(block)
        state = _MC_STATE.search(block)
        out.append({
            "controller": "", "name": "VD %s" % vd.group(1),
            "level": "raid%s" % lvl.group(1) if lvl else "",
            "state": state.group(1).strip() if state else "",
            "size": size.group(1).strip() if size else "", "source": "megacli"})
    return out


# Logical device number 0 / RAID level : 5 / Status of logical device : Optimal
_AC_LD = re.compile(r"Logical device number\s+(\d+)", re.I)
_AC_LEVEL = re.compile(r"RAID level\s*:\s*(\S+)", re.I)
_AC_STATE = re.compile(r"Status of logical device\s*:\s*(.+)", re.I)
_AC_SIZE = re.compile(r"^\s*Size\s*:\s*(.+)$", re.I | re.M)


def parse_arcconf(text: str) -> List[Dict[str, Any]]:
    """Logical devices from `arcconf GETCONFIG 1 LD` (Adaptec/Microsemi)."""
    out: List[Dict[str, Any]] = []
    blocks = re.split(r"(?=Logical device number\s+\d+)", text or "")
    for block in blocks:
        ld = _AC_LD.search(block)
        if not ld:
            continue
        lvl = _AC_LEVEL.search(block)
        state = _AC_STATE.search(block)
        size = _AC_SIZE.search(block)
        level = (lvl.group(1).strip() if lvl else "")
        out.append({
            "controller": "", "name": "logicaldevice %s" % ld.group(1),
            "level": "raid" + level if level.isdigit() else level.lower(),
            "state": state.group(1).strip() if state else "",
            "size": size.group(1).strip() if size else "", "source": "arcconf"})
    return out


def _find_cli(run: Run, names, paths) -> str:
    """The runnable path for a family: a name on PATH, or a known location."""
    for name in names:
        hit = (run("command -v %s 2>/dev/null" % name) or "").strip()
        if hit:
            return hit.splitlines()[0].strip()
    for path in paths:
        if (run("test -x %s && echo yes 2>/dev/null" % path) or "").strip() == "yes":
            return path
    return ""


def _run_hw_cli(run: Run, family: str, cli: str) -> List[Dict[str, Any]]:
    q = cli if " " not in cli else '"%s"' % cli
    if family == "storcli":
        return parse_storcli(run("%s /call show all J 2>/dev/null" % q))
    if family == "ssacli":
        return parse_ssacli(run("%s ctrl all show config 2>/dev/null" % q))
    if family == "megacli":
        return parse_megacli(run("%s -LDInfo -Lall -aALL 2>/dev/null" % q))
    if family == "arcconf":
        return parse_arcconf(run("%s GETCONFIG 1 LD 2>/dev/null" % q))
    return []


def collect(run: Run, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Disks, controller and RAID for one host, nothing invented."""
    say = log or (lambda _m: None)
    result: Dict[str, Any] = {"disks": [], "raid": {}}

    found = parse_lsblk_json(run(LSBLK_JSON)) or parse_lsblk_pairs(run(LSBLK_PAIRS))
    disks = [d for d in found if d.get("class") == "local"]
    result["disks"] = disks
    network = [{"name": d["name"], "size": d["size"], "model": d["model"]}
               for d in found if d.get("class") == "network"]
    if network:
        result["iscsi"] = network

    raid: Dict[str, Any] = {}
    controllers = detect_controllers(run(LSPCI), run(SCSI_DRIVERS))
    if controllers:
        raid["controllers"] = controllers
    software = parse_mdstat(run(MDSTAT))
    if software:
        raid["software"] = software

    # The array layout needs the controller's own CLI; find whichever family is
    # installed, wherever this distro put it, and ask only that one.
    hardware: List[Dict[str, Any]] = []
    present: List[str] = []
    for family, names, paths in HW_FAMILIES:
        cli = _find_cli(run, names, paths)
        if not cli:
            continue
        present.append(cli)
        rows = _run_hw_cli(run, family, cli)
        if rows:
            hardware.extend(rows)
            break
    if hardware:
        raid["hardware"] = hardware
    elif controllers and not software:
        # A controller is here but its CLI is not, so the array is visible as a
        # "logical" disk but its layout cannot be read. Say so rather than imply
        # there is no RAID.
        raid["note"] = ("a RAID controller is present; install its CLI "
                        "(ssacli / storcli / perccli / megacli / arcconf) "
                        "to read the array layout")
    if present:
        raid["cli_present"] = present

    # SMART per disk - smartctl opens the device, so it runs only with the
    # privileged run; absent smartctl just leaves health unread.
    if disks and (run("command -v smartctl 2>/dev/null") or "").strip():
        for d in disks:
            h = parse_smart(run(SMART_CMD % ("/dev/" + d["name"])))
            if h:
                d["health"] = h
        hp = health_problems(disks)
        if hp:
            result["health_problems"] = hp

    if raid:
        result["raid"] = raid
    if disks:
        kinds = ", ".join(sorted({d["media"] or d["bus"] or "disk" for d in disks}))
        extra = ""
        if software:
            extra = "; %d md array(s)" % len(software)
        elif hardware:
            extra = "; %d hw array(s)" % len(hardware)
        elif controllers:
            extra = "; RAID controller, no CLI"
        say("  storage: %d disk(s) (%s)%s" % (len(disks), kinds, extra))
    return result


# SMART health. smartctl reads it straight from the drive; it needs root to
# open the device, so the collector asks for it only when a sudo-capable run is
# given. Two output shapes: NVMe prints key: value, a SATA/SAS disk prints the
# attribute table whose last column is the raw value.
SMART_CMD = "smartctl -H -A %s 2>/dev/null"
_SMART_KV = {
    "critical_warning": r"Critical Warning:\s*(\S+)",
    "percentage_used": r"Percentage Used:\s*(\d+)",
    "available_spare": r"Available Spare:\s*(\d+)",
    "available_spare_threshold": r"Available Spare Threshold:\s*(\d+)",
    "media_errors": r"Media and Data Integrity Errors:\s*([\d,]+)",
    "temperature_c": r"Temperature:\s*(\d+)\s*Celsius",
    "power_on_hours": r"Power On Hours:\s*([\d,]+)",
}
# SATA attribute name -> our key; the raw value is the last integer on the line.
_SMART_ATTR = {
    "Reallocated_Sector_Ct": "reallocated",
    "Current_Pending_Sector": "pending",
    "Offline_Uncorrectable": "offline_uncorrectable",
    "Temperature_Celsius": "temperature_c",
    "Power_On_Hours": "power_on_hours",
}
# SSD wear shows as a normalised value counting DOWN from 100; the used percent
# is 100 minus it. Percent_Lifetime_Remain and Percentage Used count up.
_SMART_WEAR_DOWN = ("Wear_Leveling_Count", "Media_Wearout_Indicator",
                    "SSD_Life_Left")


def _num(value: Any) -> Optional[int]:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_smart(text: str) -> Dict[str, Any]:
    """One health record from `smartctl -H -A`, NVMe or SATA/SAS."""
    out: Dict[str, Any] = {}
    s = text or ""
    m = re.search(r"SMART overall-health[^:]*:\s*(\w+)", s)
    if m:
        out["passed"] = m.group(1).upper() == "PASSED"
    for key, pat in _SMART_KV.items():
        m = re.search(pat, s)
        if m:
            v = m.group(1)
            if key == "critical_warning":
                out[key] = v
            else:
                n = _num(v)
                if n is not None:
                    out[key] = n
    for name, key in _SMART_ATTR.items():
        # RAW_VALUE is the first integer after the WHEN_FAILED "-" column; anchor
        # there so a trailing "(Min/Max 18/32)" on a temperature line is ignored.
        m = re.search(r"^\s*\d+\s+%s\b.*?\s-\s+(\d+)" % name, s, re.M)
        if m and key not in out:
            out[key] = _num(m.group(1))
    for name in _SMART_WEAR_DOWN:
        m = re.search(r"^\s*\d+\s+%s\b\s+\S+\s+(\d+)\s" % name, s, re.M)
        if m and "percentage_used" not in out:
            left = _num(m.group(1))
            if left is not None and 0 <= left <= 100:
                out["percentage_used"] = 100 - left
            break
    return out


def health_problems(disks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Drives that are failing or wearing out, worst first."""
    found: List[Dict[str, str]] = []
    for d in disks:
        h = d.get("health") or {}
        name = d.get("name", "")
        if h.get("passed") is False or str(h.get("critical_warning") or "0x00") not in ("0", "0x00", "0x0"):
            found.append({"disk": name, "severity": "mismatch",
                          "detail": "%s SMART reports the drive is FAILING" % name})
        spare, thr = h.get("available_spare"), h.get("available_spare_threshold")
        if isinstance(spare, int) and isinstance(thr, int) and spare < thr:
            found.append({"disk": name, "severity": "mismatch",
                          "detail": "%s spare blocks %d%% below the %d%% threshold"
                          % (name, spare, thr)})
        for key, label in (("reallocated", "reallocated"),
                           ("pending", "pending"),
                           ("offline_uncorrectable", "offline-uncorrectable")):
            n = h.get(key)
            if isinstance(n, int) and n > 0:
                found.append({"disk": name, "severity": "warn",
                              "detail": "%s has %d %s sector(s)" % (name, n, label)})
        me = h.get("media_errors")
        if isinstance(me, int) and me > 0:
            found.append({"disk": name, "severity": "warn",
                          "detail": "%s reports %d media error(s)" % (name, me)})
        used = h.get("percentage_used")
        if isinstance(used, int) and used >= 90:
            found.append({"disk": name, "severity": "warn",
                          "detail": "%s is %d%% worn" % (name, used)})
    return found

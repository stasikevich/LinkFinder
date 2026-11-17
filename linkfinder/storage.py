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

# Management CLIs, per family. Presence is what decides whether array detail is
# read; absence is normal and simply leaves the layout unread.
HW_CLIS = ("storcli64", "storcli", "perccli64", "perccli",
           "ssacli", "hpssacli", "MegaCli64", "MegaCli")

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


def _run_hw_cli(run: Run, tool: str) -> List[Dict[str, Any]]:
    if tool.startswith(("storcli", "perccli")):
        return parse_storcli(run("%s /call show all J 2>/dev/null" % tool))
    if tool.startswith(("ssacli", "hpssacli")):
        return parse_ssacli(run("%s ctrl all show config 2>/dev/null" % tool))
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

    # The array layout needs the controller's own CLI; ask only what is here.
    hardware: List[Dict[str, Any]] = []
    present = [t for t in HW_CLIS
               if (run("command -v %s 2>/dev/null" % t) or "").strip()]
    for tool in present:
        rows = _run_hw_cli(run, tool)
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
                        "(ssacli / storcli / perccli) to read the array layout")
    if present:
        raid["cli_present"] = present

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

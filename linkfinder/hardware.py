"""The serial number stamped on the box, however the box will admit to it.

An inventory is keyed by serial - NetBox is, asset tags are, and so is the
label on the front of the machine. A map that cannot say which physical unit a
node is cannot be compared with one. Every access path already used here can
answer it, so each one is asked in its own language and the answers are folded
into one field.

Nothing new is sent to a device: these are reads over connections the probes
have already opened.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

# SMBIOS fields, best first. product_serial is the unit; chassis and board are
# the fallbacks, and on a whitebox they are sometimes the only real ones.
DMI_FIELDS = ("product_serial", "chassis_serial", "board_serial",
              "product_name", "product_uuid", "sys_vendor", "board_vendor",
              "chassis_asset_tag", "chassis_type")

DMI_SCRIPT = (
    "for f in product_serial chassis_serial board_serial product_name"
    " product_uuid sys_vendor board_vendor chassis_asset_tag chassis_type; do"
    " v=$(cat /sys/class/dmi/id/$f 2>/dev/null);"
    " printf '%s=%s\\n' \"$f\" \"$v\"; done")

WINDOWS_SCRIPT = (
    "powershell -NoProfile -Command \""
    "$b = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue;"
    " $s = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue;"
    " $e = Get-CimInstance Win32_SystemEnclosure -ErrorAction SilentlyContinue;"
    " 'product_serial=' + $b.SerialNumber;"
    " 'product_name=' + $s.Model;"
    " 'sys_vendor=' + $s.Manufacturer;"
    " 'chassis_type=' + ($e.ChassisTypes -join ',')\"")

ESXI_COMMAND = "esxcli hardware platform get"

# ENTITY-MIB. The serial of the chassis, its model, and what each entry IS -
# a switch answers for every fan and transceiver too, and only entry class 3
# (chassis) is the box itself.
OID_ENT_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"
OID_ENT_MODEL = "1.3.6.1.2.1.47.1.1.1.1.13"
OID_ENT_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"
ENT_CLASS_CHASSIS = "3"

# What a board says when it has nothing to say. Treating these as a serial is
# worse than having none: every machine of that model would merge into one.
JUNK = ("to be filled by o.e.m.", "to be filled by oem", "not specified",
        "not available", "default string", "none", "n/a", "na", "unknown",
        "system serial number", "chassis serial number", "0123456789",
        "123456789", "00000000", "empty", "no asset tag", "undefined",
        "oem", "x", "0", "-")


def is_real(value: str) -> bool:
    """A serial that identifies one unit, rather than a placeholder."""
    text = str(value or "").strip()
    if len(text) < 3:
        return False
    if text.lower() in JUNK:
        return False
    if not re.search(r"[0-9A-Za-z]", text):
        return False
    return not re.fullmatch(r"[0.\-_ ]+", text)


def parse_fields(text: str) -> Dict[str, str]:
    """Read the key=value dump the scripts above print."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


def parse_esxi(text: str) -> Dict[str, str]:
    """`esxcli hardware platform get` prints an indented Key: value block."""
    out: Dict[str, str] = {}
    pairs = {"serial number": "product_serial", "product name": "product_name",
             "vendor name": "sys_vendor", "enclosure serial number":
             "chassis_serial", "bios asset tag": "chassis_asset_tag"}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = pairs.get(key.strip().lower())
        if field:
            out[field] = value.strip()
    return out


def serial_of(fields: Dict[str, str]) -> str:
    """The best serial in a record, or "" when the box published nothing real."""
    for key in ("product_serial", "chassis_serial", "board_serial"):
        value = (fields or {}).get(key, "")
        if is_real(value):
            return str(value).strip()
    return ""


def describe(fields: Dict[str, str]) -> str:
    """Vendor and model as one phrase, for the panel and the log."""
    vendor = str((fields or {}).get("sys_vendor") or "").strip()
    model = str((fields or {}).get("product_name") or "").strip()
    bits = [b for b in (vendor, model) if is_real(b) or b]
    return " ".join(bits).strip()


def read_linux(run: Callable[[str], str]) -> Dict[str, str]:
    """SMBIOS over an open SSH session. product_serial is root-only on many
    kernels, which is why chassis and board are read in the same breath."""
    return parse_fields(run("sh -c " + _quote(DMI_SCRIPT)))


def read_windows(run: Callable[[str], str]) -> Dict[str, str]:
    return parse_fields(run(WINDOWS_SCRIPT))


def read_esxi(run: Callable[[str], str]) -> Dict[str, str]:
    return parse_esxi(run(ESXI_COMMAND))


FIELD_OF = {"SerialNumber": "product_serial", "Model": "product_name",
            "Manufacturer": "sys_vendor", "PartNumber": "part_number"}


def read_redfish(client: Any) -> Dict[str, str]:
    """A BMC knows the serial of the machine it is bolted to, and says so
    without any of the guesswork SMBIOS needs. The collections are listed
    rather than guessed: not every BMC calls the first one "1"."""
    out: Dict[str, str] = {}
    paths = []
    for collection in ("/redfish/v1/Chassis", "/redfish/v1/Systems"):
        try:
            found = client.members(collection) or []
        except Exception:
            found = []
        paths.extend(found[:2] or [collection + "/1"])
    for path in paths:
        keys = ("SerialNumber", "Model", "Manufacturer", "PartNumber")
        try:
            body = client.get(path)
        except Exception:
            continue
        if not isinstance(body, dict):
            continue
        for key in keys:
            value = body.get(key)
            if not value:
                continue
            # A model or a maker is worth keeping as written; a serial is only
            # worth keeping when it identifies one unit.
            if key == "SerialNumber" and not is_real(str(value)):
                continue
            out.setdefault(FIELD_OF[key], str(value).strip())
    return out


def read_snmp(walk: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """ENTITY-MIB, keeping the chassis entry rather than a fan or an optic."""
    try:
        serials = walk(OID_ENT_SERIAL) or {}
    except Exception:
        return {}
    if not serials:
        return {}
    classes, models = {}, {}
    try:
        classes = walk(OID_ENT_CLASS) or {}
        models = walk(OID_ENT_MODEL) or {}
    except Exception:
        pass
    chassis = [index for index, kind in classes.items()
               if str(kind).strip() == ENT_CLASS_CHASSIS]
    order = chassis + [i for i in serials if i not in chassis]
    for index in order:
        if is_real(serials.get(index, "")):
            out = {"product_serial": str(serials[index]).strip()}
            if is_real(models.get(index, "")):
                out["product_name"] = str(models[index]).strip()
            return out
    return {}


def _quote(text: str) -> str:
    return "'%s'" % text.replace("'", "'\\''")


def attach(result: Any, fields: Dict[str, str],
           say: Optional[Callable[[str], None]] = None) -> str:
    """Record what was found on a probe result and return the serial."""
    if not fields:
        return ""
    kept = {k: v for k, v in fields.items() if str(v or "").strip()}
    if not kept:
        return ""
    result.extra["hardware"] = kept
    serial = serial_of(kept)
    if serial:
        result.device_serial = serial
    if say:
        told = describe(kept)
        if serial and told:
            say("  hardware: %s, serial %s" % (told, serial))
        elif serial:
            say("  hardware: serial %s" % serial)
        elif told:
            say("  hardware: %s, no serial published" % told)
    return serial

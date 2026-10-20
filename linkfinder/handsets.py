"""What is plugged into a host over USB, and which of those are handsets.

A lab NUC is a network device with a second, invisible fan-out: the phones on
its USB ports. When a test fails the question is rarely "is the NUC up" - it is
"is UE 3 still attached, and to which port". That is a link like any other, so
it is drawn as one, but it is NOT Ethernet and must never be mistaken for it.

Two layers, deliberately separate. Reading the USB tree costs nothing and
changes nothing: it is sysfs on Linux and the PnP registry on Windows, so it
always runs. Talking to adb may START the adb server on the host, which is a
change, so it is opt-in and only ever asked to list and to read properties.

Nothing here writes, pushes, installs or reboots anything.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# One record per device, keys we care about. Interfaces (1-3:1.0) have no
# idVendor and are skipped, which also drops the endpoint noise.
USB_SCRIPT = r'''
for d in /sys/bus/usb/devices/*; do
  [ -r "$d/idVendor" ] || continue
  printf 'DEV %s\n' "${d##*/}"
  for f in idVendor idProduct manufacturer product serial speed version \
           bDeviceClass maxchild; do
    if [ -r "$d/$f" ]; then
      printf '%s=%s\n' "$f" "$(tr -d '\r\n' < "$d/$f" 2>/dev/null)"
    fi
  done
  # A maker's name is a weak answer: the firms that build handsets also
  # build motherboards, keyboards and RGB lighting. What a device OFFERS
  # is not a guess - ff/42/01 is the ADB interface, 03 is human input.
  # Command substitution already drops the trailing newline, so no escapes
  # are needed here and none can be mangled on the way in.
  ifs=""
  for i in "$d"/*:*; do
    [ -r "$i/bInterfaceClass" ] || continue
    ifs="$ifs,$(cat "$i/bInterfaceClass")"
    ifs="$ifs$(cat "$i/bInterfaceSubClass" 2>/dev/null)"
    ifs="$ifs$(cat "$i/bInterfaceProtocol" 2>/dev/null)"
  done
  [ -n "$ifs" ] && printf 'interfaces=%s\n' "${ifs#,}"
done
'''

# Windows has no speed property on a PnP device. What it does have is the port
# the device sits on: a location path ending ACPI(SSnn) is a SuperSpeed port,
# ACPI(HSnn) a high-speed one. That is the generation of the connection, which
# is the question being asked.
USB_SCRIPT_WINDOWS = (
    "powershell -NoProfile -Command \""
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |"
    " Where-Object { $_.InstanceId -like 'USB\\*' } | ForEach-Object {"
    " $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId"
    " -KeyName 'DEVPKEY_Device_LocationPaths' -ErrorAction SilentlyContinue;"
    " 'DEV ' + $_.InstanceId;"
    " 'product=' + $_.FriendlyName;"
    " 'maker=' + $_.Manufacturer;"
    " 'location=' + (($p.Data) -join ';') }\"")

CLASS_HUB = "09"
# USB-serial bridges. They are not handsets and must not be counted as ones:
# an FTDI cable declaring USB 2.00 is a console lead, not a slow phone.
UART_MAKERS = ("ftdi", "future technology", "silicon labs", "prolific",
               "qinheng", "ch340", "cp210")
PHONE_MAKERS = ("google", "oneplus", "samsung", "xiaomi", "motorola", "sony",
                "lge", "lg electronics", "huawei", "oppo", "vivo", "asus",
                "htc", "nokia", "zte", "realme", "quectel", "fibocom",
                "sierra wireless", "telit")

# Covers USB serials and network `ip:port` transport ids. Anything else is
# REJECTED, never stripped: a serial is pasted into a command line, and a
# mutated one would quietly address the wrong phone.
SERIAL_OK = re.compile(r"[A-Za-z0-9._:-]+")

_ADB_LINE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>device|offline|unauthorized|recovery|bootloader)\b"
    r"(?P<rest>.*)$")

_LOCATION_PORT = re.compile(r"ACPI\((?P<tag>[HS]S[0-9A-Fa-f]+)\)")


def valid_serial(serial: str) -> str:
    """Allowlist a serial, rejecting rather than repairing a bad one."""
    text = str(serial or "")
    if not SERIAL_OK.fullmatch(text):
        raise ValueError(
            "invalid adb serial %r: letters, digits, dot, underscore, colon"
            " and hyphen only" % serial)
    return text


def _records(text: str) -> List[Dict[str, str]]:
    """Split the DEV-separated key=value dump into records."""
    out: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in (text or "").splitlines():
        line = line.rstrip()
        if line.startswith("DEV "):
            current = {"id": line[4:].strip()}
            out.append(current)
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return out


def _number(value: str) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_usb(text: str) -> Dict[str, Dict[str, Any]]:
    """Turn the sysfs dump into one entry per real device, hubs excluded."""
    found: Dict[str, Dict[str, Any]] = {}
    for rec in _records(text):
        if rec.get("bDeviceClass") == CLASS_HUB:
            continue        # a hub is wiring, not a thing plugged in
        maker = rec.get("manufacturer", "")
        product = rec.get("product", "")
        if not (maker or product or rec.get("serial")):
            continue
        port = rec["id"]
        entry = {
            "port": port, "parent": port.rsplit(".", 1)[0] if "." in port else "",
            "maker": maker, "product": product, "serial": rec.get("serial", ""),
            "vendor_id": rec.get("idVendor", ""), "product_id": rec.get("idProduct", ""),
            "speed_mbps": _number(rec.get("speed", "")),
            "version": (rec.get("version") or "").strip(),
            "interfaces": (rec.get("interfaces") or "").strip(),
        }
        entry["kind"] = classify(entry)
        found[port] = entry
    return found


def parse_usb_windows(text: str) -> Dict[str, Dict[str, Any]]:
    """Same shape from the PnP registry, with the port standing in for speed."""
    found: Dict[str, Dict[str, Any]] = {}
    for rec in _records(text):
        instance = rec.get("id", "")
        upper = instance.upper()
        if not instance or upper.startswith("USB\\ROOT_HUB") or "&MI_" in upper:
            continue
        serial = instance.rsplit("\\", 1)[-1] if "\\" in instance else ""
        if "&" in serial:
            serial = ""     # a generated instance id, not a device serial
        entry = {
            "port": instance, "parent": "", "maker": rec.get("maker", ""),
            "product": rec.get("product", ""), "serial": serial,
            "vendor_id": "", "product_id": "", "speed_mbps": None,
            "version": _version_from_location(rec.get("location", "")),
        }
        if _is_hub(entry):
            continue
        entry["kind"] = classify(entry)
        found[instance] = entry
    return fold_windows(found)


# The part of a PnP instance id before the last backslash, split on &. A
# device plugged in once answers as several: the composite parent carries the
# serial, and each function it offers - ADB, MTP, modem - is a separate PnP
# device with a name of its own. One phone came back as four, drawn as four
# boxes around the host.
_HARDWARE = re.compile(r"^USB\\(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})(&.*)?$", re.I)

# Words that mean the name describes a function rather than the thing. The
# parent is called "SAMSUNG Mobile USB Composite Device"; the model name
# "Galaxy A54 5G" sits on the MTP function, which is the one a person reads.
FUNCTION_WORDS = ("composite", "interface", "adb", "modem", "mtp", "bulk",
                  "controller", "driver", "gadget", "function")


def _hardware_of(instance: str) -> Tuple[str, bool]:
    """(vendor and product id, whether this is the device or one of its parts)."""
    head = str(instance or "").rsplit("\\", 1)[0]
    match = _HARDWARE.match(head)
    if not match:
        return "", False
    return match.group(1).upper(), not match.group(2)


def _is_a_model_name(product: str) -> bool:
    low = str(product or "").lower()
    return bool(low) and not any(word in low for word in FUNCTION_WORDS)


def fold_windows(found: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One entry per physical device, not one per function it offers.

    Where a composite parent is present it is the device: it holds the real
    serial, which is the only identity worth having, and four identical
    handsets on one machine stay four. The parts are dropped, but their
    names are not - the model name lives on one of them.
    """
    out: Dict[str, Dict[str, Any]] = {}
    whole: Dict[str, Dict[str, Dict[str, Any]]] = {}
    parts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for instance, entry in found.items():
        hardware, is_whole = _hardware_of(instance)
        if not hardware:
            out[instance] = entry            # not a USB VID/PID id at all
        elif is_whole:
            whole.setdefault(hardware, {})[instance] = entry
        else:
            parts.setdefault(hardware, {})[instance] = entry

    def model_name(bits: Dict[str, Dict[str, Any]]) -> str:
        for _instance, entry in sorted(bits.items()):
            if _is_a_model_name(entry.get("product", "")):
                return str(entry.get("product"))
        return ""

    for hardware, devices in whole.items():
        named = model_name(parts.get(hardware) or {})
        for instance, entry in devices.items():
            if named and not _is_a_model_name(entry.get("product", "")):
                entry["product"] = named
                entry["kind"] = classify(entry)
            out[instance] = entry

    # A device that shows only as functions, with no parent among them, is
    # still a device: keep one of its parts rather than losing it entirely.
    for hardware, bits in parts.items():
        if hardware in whole:
            continue
        named = model_name(bits)
        instance, entry = sorted(bits.items())[0]
        if named:
            entry["product"] = named
            entry["kind"] = classify(entry)
        out[instance] = entry
    return out


def _is_hub(entry: Dict[str, Any]) -> bool:
    text = ("%s %s" % (entry.get("product", ""), entry.get("maker", ""))).lower()
    return "hub" in text and "usb" in text


def _version_from_location(location: str) -> str:
    """ACPI(SSnn) is a SuperSpeed port; ACPI(HSnn) a high-speed one."""
    tags = _LOCATION_PORT.findall(location or "")
    if not tags:
        return ""
    if any(tag.upper().startswith("SS") for tag in tags):
        return "3.00"
    return "2.00"


ADB = "ff4201"          # vendor-specific / ADB / protocol 1, exactly
HID = "03"              # a keyboard, a mouse, an RGB controller

# Things that say in their own name what they are. The firms that build
# handsets also build motherboards, lighting and docks, so the maker is the
# last thing to go on and a product name beats it.
NOT_A_HANDSET = ("led", "rgb", "aura", "keyboard", "mouse", "webcam",
                 "camera", "hub", "dock", "receiver", "fingerprint",
                 "card reader", "sensor", "ups", "printer", "scanner")


def classify(entry: Dict[str, Any]) -> str:
    """What sort of thing this is, from what it offers and what it is called.

    The maker's name alone put a motherboard's RGB controller on the map as
    a handset, because the firm that built it also builds phones. What the
    device offers settles it where it can: the ADB interface is a handset
    outright, and a thing that only offers human-input interfaces is not
    one. Between those two lies the case this got wrong - an RGB controller
    offering a vendor-specific interface alongside HID, which is neither
    "all HID" nor ADB, so it fell through to the maker and became a phone.
    A product that names its own function is taken at its word.
    """
    offered = [i for i in str(entry.get("interfaces", "")).split(",") if i]
    maker = str(entry.get("maker", "")).lower()
    product = str(entry.get("product", "")).lower()
    both = "%s %s" % (maker, product)
    if ADB in offered:
        return "phone"
    if offered and all(i.startswith(HID) for i in offered):
        return "peripheral"
    if any(word in product for word in NOT_A_HANDSET):
        return "peripheral"
    if any(word in both for word in UART_MAKERS):
        return "serial"
    if any(word in both for word in PHONE_MAKERS):
        return "phone"
    if "android" in both:
        return "phone"
    return "peripheral"


def parse_adb_devices(text: str) -> List[Dict[str, str]]:
    """Parse `adb devices -l` into [{serial, state, model, product, device}]."""
    devices: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        match = _ADB_LINE.match(line)
        if not match:
            continue
        found = {"serial": match.group("serial"), "state": match.group("state"),
                 "model": "", "product": "", "device": ""}
        for pair in match.group("rest").split():
            if ":" in pair:
                key, value = pair.split(":", 1)
                if key in found:
                    found[key] = value
        devices.append(found)
    return devices


# Read-only properties, enough to name a handset on the map. Nothing is set.
PROPS = ("ro.product.model", "ro.product.manufacturer", "ro.build.version.release")


def _quote_posix(text: str) -> str:
    return "'%s'" % text.replace("'", "'\\''")


def adb_shell(adb: str, serial: str, command: str, windows: bool = False) -> str:
    """`adb -s <serial> shell <command>`, quoted for the HOST's login shell.

    The inner quoting is for the Android shell; the outer wrap differs, because
    cmd.exe does not strip single quotes - a Windows controller would silently
    run nothing. A double-quote in the command is rejected on Windows rather
    than deleted, since it is the one character that escapes that wrap.
    """
    serial = valid_serial(serial)
    if " " in adb and not adb.startswith('"'):
        adb = '"%s"' % adb
    if windows:
        if '"' in command:
            raise ValueError("a double-quote cannot be passed through cmd.exe")
        return '%s -s "%s" shell "%s"' % (adb, serial, command)
    return "%s -s %s shell %s" % (adb, _quote_posix(serial), _quote_posix(command))


def parse_props(text: str) -> Dict[str, str]:
    """Read `getprop` lines of the form [key]: [value]."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        match = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$", line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


# A USB product string is not always a name. An Android with no adb running
# offers the MTP descriptor - "KALAMA-MTP_CID:0437_SN:5C97E23F" - where
# KALAMA is the chipset, CID the carrier and SN the serial. Windows reads the
# model from the driver and shows "Galaxy A54 5G", so the same bench looked
# tidy on one machine and unreadable on the other.
_DESCRIPTOR = re.compile(r"(_SN:|_CID:|-MTP\b|_MTP\b)", re.I)


def _is_a_descriptor(product: str, serial: str) -> bool:
    """Whether this product string is a machine's words rather than a name."""
    text = str(product or "")
    if not text:
        return False
    if _DESCRIPTOR.search(text):
        return True
    # The clearest sign of all: the serial is inside the name. No maker
    # writes the unit's serial into the model.
    return bool(serial) and str(serial).lower() in text.lower()


def describe(entry: Dict[str, Any]) -> str:
    """The name a person would use for this thing."""
    for key in ("model", "adb_model"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    product = str(entry.get("product") or "").strip()
    maker = str(entry.get("maker") or "").strip()
    serial = str(entry.get("serial") or "").strip()
    if product and maker and _is_a_descriptor(product, serial):
        # The maker is real and the serial tells two of them apart, which is
        # the same shape the map already uses for a device known by its MAC.
        return "%s %s" % (maker, serial) if serial else maker
    for key in ("product", "maker"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return str(entry.get("serial") or entry.get("port") or "usb device")


def merge(usb: Dict[str, Dict[str, Any]],
          adb: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Join what the bus says with what adb says, matching on the serial.

    A phone adb can see but the bus cannot is still reported: it may be
    attached over the network, or on a port sysfs did not name. It is marked so,
    rather than dropped, because a missing handset is exactly what people chase.
    """
    by_serial = {entry["serial"].lower(): entry
                 for entry in usb.values() if entry.get("serial")}
    for found in adb:
        entry = by_serial.get(found["serial"].lower())
        if entry is None:
            entry = {"port": "", "parent": "", "maker": "", "product": "",
                     "serial": found["serial"], "vendor_id": "", "product_id": "",
                     "speed_mbps": None, "version": "", "kind": "phone",
                     "not_on_bus": True}
            usb["adb:%s" % found["serial"]] = entry
        entry["adb"] = found["state"]
        entry["adb_serial"] = found["serial"]
        entry["kind"] = "phone"
        for key in ("model", "product", "device"):
            if found.get(key):
                entry.setdefault("adb_%s" % key, found[key])
    return usb


def collect(run: Callable[[str], str], say: Callable[[str], None],
            windows: bool = False, adb: bool = False,
            adb_path: str = "adb") -> Dict[str, Dict[str, Any]]:
    """Read the USB tree, and optionally ask adb to name the handsets on it."""
    try:
        text = run(USB_SCRIPT_WINDOWS if windows else "sh -c " + _quote_posix(USB_SCRIPT))
    except Exception as problem:                       # a probe never kills a scan
        say("  usb: could not read the bus (%s)" % problem)
        return {}
    found = parse_usb_windows(text) if windows else parse_usb(text)
    if found:
        say("  usb: %d device(s) attached" % len(found))
    if not adb:
        return found
    try:
        listed = parse_adb_devices(run("%s devices -l" % adb_path))
    except Exception as problem:
        say("  adb: not asked again (%s)" % problem)
        return found
    found = merge(found, listed)
    for entry in found.values():
        if entry.get("adb") != "device" or not entry.get("adb_serial"):
            continue
        try:
            props = parse_props(run(adb_shell(adb_path, entry["adb_serial"],
                                              "getprop", windows=windows)))
        except Exception:
            continue
        if props.get("ro.product.model"):
            entry["model"] = props["ro.product.model"]
        if props.get("ro.product.manufacturer") and not entry.get("maker"):
            entry["maker"] = props["ro.product.manufacturer"]
        if props.get("ro.build.version.release"):
            entry["android"] = props["ro.build.version.release"]
    phones = [e for e in found.values() if e.get("kind") == "phone"]
    if phones:
        say("  adb: %d handset(s): %s"
            % (len(phones), ", ".join(sorted(describe(e) for e in phones))))
    return found

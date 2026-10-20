"""What a MikroTik switch knows about itself, beyond its neighbour table.

A RouterOS box was only ever asked who it could see. It also knows the
temperature of every transceiver, the light going into and out of it, which
power supply has died and how fast the fans are turning - the same readings
`ethtool -m` gives on a server, from a device nobody can put ethtool on.

Printing is the problem. `/interface/ethernet/monitor [find] once` lays the
ports out side by side in columns, so a thirty-port switch answers in lines
thousands of characters wide with no way to tell which value belongs to
which port. Asking RouterOS to walk the ports itself and print one line each
turns that into something a parser can trust.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional, Tuple

# A port is named by whoever racked it, and a name on a live switch is
# "sfp28-1 (UL-IT)" as often as not. One vertical bar typed into such a name
# would split a record in the wrong place, so the separator is one nobody
# types.
MARK = "~|~"

# One line per port, every value named. `as-value` returns raw integers -
# the scaling is done here, where it can be tested, rather than trusting a
# printed string that changes with the locale and the RouterOS version.
WANTED = (
    ("name", "name"), ("status", "status"), ("rate", "rate"),
    ("present", "sfp-module-present"), ("vendor", "sfp-vendor-name"),
    ("part", "sfp-vendor-part-number"), ("serial", "sfp-vendor-serial"),
    ("revision", "sfp-vendor-revision"), ("made", "sfp-manufacturing-date"),
    ("wavelength", "sfp-wavelength"), ("temperature", "sfp-temperature"),
    ("voltage", "sfp-supply-voltage"), ("tx", "sfp-tx-power"),
    ("rx", "sfp-rx-power"), ("bias", "sfp-tx-bias-current"),
    ("rxloss", "sfp-rx-loss"), ("txfault", "sfp-tx-fault"),
    ("connector", "sfp-connector-type"), ("type", "sfp-type"),
    ("om1", "sfp-link-length-om1"), ("om2", "sfp-link-length-om2"),
    ("om3", "sfp-link-length-om3"), ("sm", "sfp-link-length-sm"),
)

PORTS_SCRIPT = (
    ":foreach i in=[/interface/ethernet/find] do={"
    ":local m [/interface/ethernet/monitor $i once as-value]; "
    ':put ("LFPORT"'
    + "".join(' . "%s%s=" . ($m->"%s")' % (MARK, short, key)
                for short, key in WANTED)
    + ")}"
)

HEALTH_SCRIPT = (
    ':foreach i in=[/system/health/find] do={:put ("LFHEALTH' + MARK + '"'
    ' . [/system/health/get $i name] . "' + MARK + '"'
    ' . [/system/health/get $i value] . "' + MARK + '"'
    ' . [/system/health/get $i type])}'
)

BOARD_COMMAND = "/system/routerboard/print"

# RouterOS 6 keeps health as flat properties on one record - "temperature: 45C"
# - and has no list to walk, so the script above comes back with nothing at
# all. The printed form is the only thing both versions agree on.
HEALTH_FALLBACK = "/system/health/print"
HEALTH_LINE = re.compile(r"^\s*([a-z0-9-]+)\s*:\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")

# RouterOS reports these as whole numbers in smaller units. Dividing here
# keeps the record identical in shape to the one read off a Linux host, so
# the panel and the CSV need to know nothing about where it came from.
SCALE = {"wavelength": 100.0, "voltage": 1000.0, "tx": 1000.0, "rx": 1000.0,
         "sm": 1000.0}

# Only these are numbers. Running the rest through a number reader turned a
# part number into 8571 and a serial into 39, which is worse than not reading
# them at all: a wrong serial is one somebody will search an inventory for.
NUMERIC = ("wavelength", "temperature", "voltage", "tx", "rx", "bias",
           "om1", "om2", "om3", "sm")

# A module reports every reach field and fills the ones that do not apply
# with zero. Kept, a multimode module claims a single-mode reach of 0 km,
# which is not a measurement - it is the absence of one. Temperature and
# optical power are left alone: 0 is a real reading for both.
ZERO_MEANS_ABSENT = ("wavelength", "om1", "om2", "om3", "sm")

OPTIC_KEYS = (
    ("vendor", "vendor"), ("part", "part"), ("serial", "serial"),
    ("revision", "revision"), ("made", "date_code"),
    ("connector", "connector"), ("type", "identifier"),
    ("wavelength", "wavelength_nm"), ("temperature", "temperature_c"),
    ("voltage", "voltage_v"), ("tx", "tx_dbm"), ("rx", "rx_dbm"),
    ("bias", "bias_ma"), ("om3", "reach_om3_m"), ("sm", "reach_smf_km"),
)

RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([GM])bps", re.I)


def _number(text: str) -> Optional[float]:
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    if not match:
        return None
    value = float(match.group(0))
    return int(value) if value == int(value) else value


def rate_to_mbps(text: str) -> str:
    """"10Gbps" as the kernel would have written it, so one reader fits both."""
    match = RATE.match(str(text or ""))
    if not match:
        return ""
    value = float(match.group(1))
    if match.group(2).upper() == "G":
        value *= 1000
    return "%dMb/s" % int(value)


def _fields(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for piece in line.split(MARK)[1:]:
        if "=" in piece:
            key, value = piece.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def parse_ports(text: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (optics by port, ports by port) from the one-line-per-port dump.

    A port with no module gets a ports entry and no optics entry: it is still
    worth drawing as a port that exists and carries nothing, which is exactly
    the port somebody is looking for.
    """
    optics: Dict[str, Any] = {}
    ports: Dict[str, Any] = {}
    for line in str(text or "").splitlines():
        if not line.strip().startswith("LFPORT"):
            continue
        got = _fields(line)
        name = got.get("name") or ""
        if not name:
            continue
        up = got.get("status", "") == "link-ok"
        ports[name] = {"role": "physical", "mac": "",
                       "state": "up" if up else "down",
                       "speed": rate_to_mbps(got.get("rate", "")),
                       "mtu": "", "addresses": [],
                       "carrier": "up" if up else ""}
        if got.get("present") != "true":
            continue
        record: Dict[str, Any] = {}
        for short, key in OPTIC_KEYS:
            raw = got.get(short, "")
            if raw in ("", "false", "true"):
                continue
            if short not in NUMERIC:
                record[key] = raw
                continue
            value = _number(raw)
            if value is None or (not value and short in ZERO_MEANS_ABSENT):
                continue
            divisor = SCALE.get(short)
            if divisor:
                value = round(value / divisor, 3)
            # 850.0 nm reads like a measurement taken to a tenth. It is not:
            # a whole number stays whole.
            record[key] = int(value) if value == int(value) else value
        # A transceiver shouting about itself is worth hearing even when the
        # link is fine: loss of receive and a transmitter fault are the two
        # things a module reports that nothing else on the map can.
        for short, key in (("rxloss", "rx_loss"), ("txfault", "tx_fault")):
            if got.get(short) == "true":
                record[key] = True
        if record:
            optics[name] = record
    return optics, ports


# A reading whose value is a word rather than a number is a state, and only
# one word means healthy. Anything else is worth putting in front of a person.
GOOD_WORDS = ("ok", "true", "normal")


def parse_health(text: str) -> Dict[str, Any]:
    """Temperatures, fans and power supplies, each with the unit it came with."""
    out: Dict[str, Any] = {}
    for line in str(text or "").splitlines():
        if not line.strip().startswith("LFHEALTH"):
            continue
        parts = [p.strip() for p in line.split(MARK)]
        if len(parts) < 3 or not parts[1]:
            continue
        value = parts[2]
        unit = parts[3] if len(parts) > 3 else ""
        number = _number(value) if unit else None
        out[parts[1]] = {"value": number if number is not None else value,
                         "unit": unit,
                         "ok": True if number is not None
                         else value.lower() in GOOD_WORDS}
    return out


def parse_health_printed(text: str) -> Dict[str, Any]:
    """The RouterOS 6 shape: one record of properties, units glued to numbers."""
    out: Dict[str, Any] = {}
    for line in str(text or "").splitlines():
        match = HEALTH_LINE.match(line)
        if not match:
            continue
        name, value, unit = match.groups()
        if name in ("routerboard", "model", "revision"):
            continue
        out[name] = {"value": _number(value), "unit": unit, "ok": True}
    return out


BOARD_FIELDS = {"model": "product_name", "serial-number": "product_serial",
                "current-firmware": "firmware", "revision": "board_revision"}


def parse_board(text: str) -> Dict[str, str]:
    """`/system/routerboard/print` in the shape the serial reader already takes."""
    out: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = BOARD_FIELDS.get(key.strip().lower())
        if field and value.strip():
            out[field] = value.strip()
    if out:
        out.setdefault("sys_vendor", "MikroTik")
    return out


def collect(run: Callable[[str], str],
            say: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Everything a RouterOS box will say about its own hardware.

    Every step is optional: an older RouterOS without `/system/health`, or a
    box with no transceivers at all, returns nothing for that step and the
    rest still arrives.
    """
    talk = say or (lambda _m: None)
    found: Dict[str, Any] = {}
    try:
        optics, ports = parse_ports(run(PORTS_SCRIPT))
    except Exception as problem:
        talk("  routeros: ports not read: %s" % problem)
        optics, ports = {}, {}
    if optics:
        found["optics"] = optics
        talk("  optics: %d module(s) read off the switch" % len(optics))
    if ports:
        found["ports"] = ports
    try:
        health = parse_health(run(HEALTH_SCRIPT))
        if not health:
            # RouterOS 6 has no health list to walk, so the script says
            # nothing rather than failing. The printed record still answers.
            health = parse_health_printed(run(HEALTH_FALLBACK))
    except Exception as problem:
        talk("  routeros: health not read: %s" % problem)
        health = {}
    if health:
        found["health"] = health
        bad = [name for name, rec in sorted(health.items()) if not rec.get("ok")]
        talk("  health: %d reading(s)%s"
             % (len(health), ", %s not ok" % ", ".join(bad) if bad else ""))
    try:
        board = parse_board(run(BOARD_COMMAND))
    except Exception as problem:
        talk("  routeros: board not read: %s" % problem)
        board = {}
    if board:
        found["hardware"] = board
    return found

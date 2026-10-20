"""Who made the thing, from its MAC and nothing else.

The first 24 bits of a MAC are assigned to a company by the IEEE, and the
register of who holds what ships inside this binary. So a device that
answers nothing, has no address and was only ever mentioned by a neighbour
still gets a name worth reading: "Super Micro" says more than
`ac:1f:6b:c6:3a:3c`, and it costs no packet and no password.

Two things the naive lookup gets wrong, and this does not:

- **Not every prefix is 24 bits.** The IEEE also hands out 28- and 36-bit
  prefixes carved out of a few 24-bit blocks. Taking the first three octets
  there credits the block's administrator instead of the maker, so the
  longest assignment wins.

- **Half the MACs on a modern host were never assigned to anybody.** A veth,
  a bridge, a tap, a container's port: the kernel makes their addresses up
  and sets the locally-administered bit to say so. Looking those up returns
  somebody else's name by coincidence, which is worse than saying nothing.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import gzip
import os
import re
import sys
from typing import Any, Dict, Optional

ASSET = "oui.gz"

LENGTHS = (9, 7, 6)

_TABLE: Optional[Dict[str, str]] = None

HEX = re.compile(r"[^0-9a-fA-F]")


def _assets_dir() -> str:
    from .render_topology import assets_dir

    return assets_dir()


def table() -> Dict[str, str]:
    """The register, read once and kept.

    A missing file is not fatal: every caller treats an unknown maker as a
    maker it does not know, which is what it would be anyway.
    """
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    _TABLE = {}
    try:
        path = os.path.join(_assets_dir(), ASSET)
        with gzip.open(path, "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except Exception:
        return _TABLE
    for line in text.splitlines():
        prefix, _tab, name = line.partition("\t")
        if prefix and name:
            _TABLE[prefix] = name
    return _TABLE


def made_up(mac: str) -> bool:
    """Is this an address the kernel invented rather than one IEEE assigned?

    Bit 1 of the first octet is the locally-administered flag. Set means
    nobody registered it, so there is no maker to find.
    """
    digits = HEX.sub("", str(mac or ""))
    if len(digits) < 2:
        return False
    try:
        return bool(int(digits[:2], 16) & 0x02)
    except ValueError:
        return False


def vendor(mac: str) -> str:
    """The maker's name, or "" when there is none to be had."""
    digits = HEX.sub("", str(mac or "")).upper()
    if len(digits) < 6 or made_up(digits):
        return ""
    known = table()
    for size in LENGTHS:
        if len(digits) >= size:
            hit = known.get(digits[:size])
            if hit:
                return hit
    return ""


# What a maker is known to build, so a device we only have a MAC for is still
# drawn as the hardware it is. The first match wins, so the narrow names come
# before the broad ones; a maker that builds several classes is left out
# rather than guessed at.
MAKER_KIND = [
    ("timing", ("trimble", "seiko", "meinberg", "oscilloquartz", "symmetricom",
                "spectracom", "orolia", "endrun", "frequency electronics")),
    ("radio", ("parallel wireless", "comba telecom", "zillnk", "airspan",
               "baicells", "corning optical", "jma wireless", "casa systems")),
    ("switch", ("cisco", "juniper", "arista", "aruba", "hewlett packard enterprise",
                "mikrotik", "routerboard", "fs com", "fibrolan", "extreme networks",
                "brocade", "allied telesis", "edge-core", "accton", "ubiquiti")),
    ("host", ("super micro", "supermicro", "dell inc", "lenovo", "asustek",
              "gigabyte", "quanta computer", "inspur", "intel corporate")),
    ("phone", ("polycom", "yealink", "grandstream", "snom")),
    ("bmc", ("american megatrends", "aspeed")),
]


def kind_from_maker(name: str) -> str:
    """The hardware class a maker is known for, or "" when it is not clear.

    The MAC register names the maker, and for a device we never reached that is
    the only thing we have; drawing it as its own kind of box beats a question
    mark, as long as the panel says the icon came from the maker.
    """
    low = (name or "").lower()
    if not low:
        return ""
    for kind, names in MAKER_KIND:
        for maker in names:
            if maker in low:
                return kind
    return ""


def multicast(mac: str) -> bool:
    """Bit 0 of the first octet: a group, not a device."""
    digits = HEX.sub("", str(mac or ""))
    if len(digits) < 2:
        return False
    try:
        return bool(int(digits[:2], 16) & 0x01)
    except ValueError:
        return False


def mac_kind(mac: str) -> str:
    """What sort of address this is, which decides whether to look it up."""
    digits = HEX.sub("", str(mac or ""))
    if len(digits) != 12:
        return ""
    if multicast(digits):
        return "multicast"
    return "locally administered" if made_up(digits) else "universal"


def describe(mac: str) -> str:
    """A phrase to put after a MAC, or "" when nothing can be said."""
    name = vendor(mac)
    if name:
        return name
    if made_up(mac):
        return "locally administered, so no maker"
    return ""


PLACEHOLDER = ("unknown", "n/a", "none", "-", "to be filled by o.e.m.",
               "system manufacturer", "default string", "not specified",
               "not applicable", "chassis manufacturer", "oem")

NOT_A_NAME_AT_ALL = re.compile(r"^[\d\s.:+_/-]+$")


def names_a_maker(text: str) -> bool:
    """Would a person read this as a company, or as a version number?

    What a device says about itself over LLDP or SNMP is a description, not
    a maker's field: one switch here answers `8.24.2.2-B3 2024-11-04...`,
    which is a firmware build and nothing else. Boards are no better - DMI
    is full of `To be filled by O.E.M.` Treating either as a maker buries
    the register's correct answer under a string that names nobody.
    """
    said = " ".join(str(text or "").split())
    if not said or said.lower() in PLACEHOLDER:
        return False
    return not NOT_A_NAME_AT_ALL.match(said)


# Only fields that are meant to hold a maker's name. A model or firmware
# string is kept where it belongs, as the model, and never promoted here.
SOURCES = (
    ("the board's own record (DMI)", "high",
     lambda node: ((node.get("extra") or {}).get("virtual")
                   or {}).get("sys_vendor")),
)


def vendor_of(node: Dict[str, Any]) -> Dict[str, str]:
    """Who made this device, where that came from, and how sure it is.

    Derived rather than measured, so it is worked out here instead of being
    written into the topology JSON, which holds what the probes actually
    saw. The ladder matters more than the lookup: a switch that told us
    "Aruba JL676A PL.10.13.1161" over LLDP has said more about itself than
    its MAC prefix ever will, and must not be overwritten by it.
    """
    chassis = str(node.get("chassis_id") or "")
    kind = mac_kind(chassis)
    for label, sure, pick in SOURCES:
        try:
            said = str(pick(node) or "").strip()
        except Exception:
            said = ""
        if names_a_maker(said):
            return {"vendor": said, "vendor_source": label,
                    "vendor_confidence": sure, "mac_type": kind}

    if kind == "universal":
        name = vendor(chassis)
        if name:
            return {"vendor": name, "vendor_source": "MAC prefix (IEEE)",
                    "vendor_confidence": "medium", "mac_type": kind}
    return {"vendor": "", "vendor_source": "", "vendor_confidence": "none",
            "mac_type": kind}


def main() -> int:
    """python -m linkfinder.oui aa:bb:cc:dd:ee:ff ..."""
    rows = sys.argv[1:]
    if not rows:
        print("%d prefixes loaded" % len(table()))
        return 0
    for mac in rows:
        print("%-20s %s" % (mac, describe(mac) or "not in the register"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

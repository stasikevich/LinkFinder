"""Build the vendor table the tool ships with, from the IEEE registry.

Run: python tools/make_oui.py

A MAC's first 24 bits are assigned to a company by the IEEE, and the register
of who holds what is public. Knowing it turns a bare `ac:1f:6b:c6:3a:3c` on
the map into a maker's name - no packet, no credential, no network.

The tool must never reach the internet, so the table is built here and
shipped inside the binary. Re-run this when the register has moved on; it is
the only part of the project that fetches anything.

Three registers matter. MA-L hands out 24-bit prefixes, which is the familiar
case. MA-M and MA-S hand out 28- and 36-bit prefixes carved out of a few MA-L
blocks, so a lookup has to try the longest prefix first or every device in
one of those blocks is attributed to the block's administrator.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import csv
import gzip
import io
import os
import re
import sys
import urllib.request

SOURCES = (
    ("MA-L", "https://standards-oui.ieee.org/oui/oui.csv", 6),
    ("MA-M", "https://standards-oui.ieee.org/oui28/mam.csv", 7),
    ("MA-S", "https://standards-oui.ieee.org/oui36/oui36.csv", 9),
)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "linkfinder", "assets", "oui.gz")

TIDY = re.compile(r"[\s,.]+$")
NOISE = ("IEEE Registration Authority", "Private")


def clean(name: str) -> str:
    """One company, written the way a person would say it.

    The register is full of legal suffixes that make a map column unreadable
    and tell the reader nothing they wanted to know.
    """
    text = " ".join(str(name or "").split())
    for junk in (", Inc.", " Inc.", ", Inc", " Inc", ", LLC", " LLC",
                 ", Ltd.", " Ltd.", ", Ltd", " Ltd", " Co., Ltd.",
                 " CO.,LTD.", " Co.,Ltd.", ", Corp.", " Corp.",
                 " Corporation", " GmbH", " S.A.", " B.V.", " A/S",
                 " Limited", " PLC", " plc"):
        if text.endswith(junk):
            text = text[:-len(junk)]
    return TIDY.sub("", text).strip()


def fetch(url: str) -> str:
    """The register answers 418 to a bare urllib, so it gets a real agent."""
    ask = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; linkfinder build script)"})
    with urllib.request.urlopen(ask, timeout=180) as handle:
        return handle.read().decode("utf-8", "replace")


def main() -> int:
    rows = {}
    for label, url, width in SOURCES:
        try:
            text = fetch(url)
        except Exception as exc:
            print("  %s: could not fetch (%s: %s)" % (label, type(exc).__name__,
                                                      exc))
            return 2
        kept = 0
        for row in csv.DictReader(io.StringIO(text)):
            prefix = re.sub(r"[^0-9A-Fa-f]", "", row.get("Assignment") or "")
            name = clean(row.get("Organization Name") or "")
            if len(prefix) != width or not name or name in NOISE:
                continue
            rows[prefix.upper()] = name
            kept += 1
        print("  %-5s %5d prefixes of %d hex digits" % (label, kept, width))

    packed = "\n".join("%s\t%s" % (p, rows[p]) for p in sorted(rows))
    with gzip.open(OUT, "wb") as handle:
        handle.write(packed.encode("utf-8"))
    print("%s  %d entries, %d KB packed (%d KB plain)"
          % (OUT, len(rows), os.path.getsize(OUT) // 1024,
             len(packed.encode("utf-8")) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())

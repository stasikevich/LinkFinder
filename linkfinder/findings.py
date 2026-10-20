"""What a map disagrees with, as files somebody can open.

Three questions get asked of a finished map: does NetBox agree, what moved
since last time, and what does not add up on its own terms. All three used
to be answered by buttons on the map page, which put a panel between the
reader and the picture and left the answers nowhere once the tab closed.

A table is the right shape for all three. It sorts, it filters, it goes in
a ticket, and it is still there tomorrow. So they are written beside the
map every time one is drawn, without anybody pressing anything - the map
is for seeing how things are joined, and these are for working through.

Nothing here asks anything of the network. It reads what the probes
already collected.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import csv
import io
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# (column heading, key in the record). Headings are what a person reads in
# a spreadsheet, so they are words rather than field names.
# The two sides go in two columns. They were a sentence - "10.0.0.5 holds
# serial X, but NetBox records Y there" - which reads well and compares
# badly: a room full of people cannot scan prose down a page. Both values
# are kept separately, so nothing has to be picked back out of the text.
NETBOX_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("verdict", "status"), ("device", "label"), ("field", "field"),
    ("on the network", "actual"), ("in NetBox", "documented"),
    ("what it means", "detail"), ("evidence", "evidence"),
    ("confidence", "confidence"), ("needs confirming", "confirm"),
    ("netbox record", "netbox"),
)

MOVED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("when", "at"), ("device", "device"), ("what", "label"),
    ("kind", "kind"), ("setup", "belongs"), ("was", "from"),
    ("now", "to"), ("detail", "detail"),
)

ODD_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("verdict", "verdict"), ("kind", "kind"), ("what", "detail"),
    ("evidence", "evidence"), ("devices", "devices"),
)


def _flat(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_flat(part) for part in value)
    if isinstance(value, dict):
        return "; ".join("%s=%s" % (k, _flat(v)) for k, v in sorted(value.items()))
    return "" if value is None else str(value)


def as_csv(rows: Sequence[Dict[str, Any]],
           columns: Sequence[Tuple[str, str]]) -> str:
    """One row per finding. The heading row goes out even with no rows, so
    an empty file reads as "asked, nothing found" and not as "never ran"."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([head for head, _key in columns])
    for row in rows or []:
        writer.writerow([_flat(row.get(key)) for _head, key in columns])
    return out.getvalue()


def write(path: str, rows: Sequence[Dict[str, Any]],
          columns: Sequence[Tuple[str, str]]) -> str:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(as_csv(rows, columns))
    return path


def beside_map(stem: str, netbox=None, moved=None, odd=None,
               log: Optional[Callable[[str], None]] = None) -> List[str]:
    """Write whichever of the three were worked out, next to the map.

    A question that was never asked gets no file at all: an empty
    netbox.csv sitting beside the map would say NetBox agreed about
    everything, when nobody asked it anything.
    """
    say = log or (lambda _m: None)
    written: List[str] = []
    for rows, columns, tail in ((netbox, NETBOX_COLUMNS, ".netbox.csv"),
                                (moved, MOVED_COLUMNS, ".moved.csv"),
                                (odd, ODD_COLUMNS, ".odd.csv")):
        if rows is None:
            continue
        try:
            written.append(write(stem + tail, rows, columns))
        except Exception as problem:     # never lose a finished map over one
            say("  %s not written: %s" % (tail.strip("."), problem))
    return written

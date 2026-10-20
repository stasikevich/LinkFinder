"""The maps this tool drew before, so "what changed" needs no file hunting.

A moved cable is what breaks a bench, and it is invisible in any single scan:
the map looks perfectly reasonable, it is just not last week's. Answering that
needs an earlier map, and asking a person to find one is how the question
stops being asked.

So every map the tool draws is kept here, smallest possible: a folder of
topology files named by when they were taken, capped so it cannot grow
without limit. Nothing else reads this and nothing depends on it existing.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

KEEP = 20                                # enough for a month of weekly scans
FOLDER = os.path.join(os.path.expanduser("~"), ".linkfinder-history")


def folder(path: Optional[str] = None) -> str:
    """Where the maps are kept: the one asked for, the team's, else here.

    A shared folder is what makes "what moved since last time" the same
    answer for everybody, instead of one per laptop.
    """
    if path:
        return path
    named = os.environ.get("LINKFINDER_HISTORY")
    if named:
        return named
    from . import shared

    return shared.folder(shared.HISTORY) or FOLDER


def listing(path: Optional[str] = None) -> List[Tuple[str, str]]:
    """Every kept map, newest first, as (when, file)."""
    where = folder(path)
    if not os.path.isdir(where):
        return []
    found = []
    for name in os.listdir(where):
        if name.endswith(".json"):
            found.append((name[:-5], os.path.join(where, name)))
    return sorted(found, reverse=True)


def keep(topology: Dict[str, Any], path: Optional[str] = None) -> str:
    """Put this map in the history and prune the oldest beyond the cap.

    A map with nothing in it is not kept: an interrupted scan must not become
    the thing a later run is compared against.
    """
    if not (topology or {}).get("nodes"):
        return ""
    where = folder(path)
    try:
        if not os.path.isdir(where):
            os.makedirs(where)
        from . import shared

        # The author is in the name, so a shared folder shows who drew what
        # and two people saving in the same second cannot collide.
        stamp = "%s-%s" % (time.strftime("%Y-%m-%d_%H%M%S"), shared.who())
        target = os.path.join(where, stamp + ".json")
        with io.open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(topology, ensure_ascii=False))
    except OSError:
        return ""                        # history is a convenience, never a blocker
    for _when, old in listing(path)[KEEP:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return target


def previous(path: Optional[str] = None,
             skip: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The most recent map that is not the one just taken."""
    for _when, found in listing(path):
        if skip and os.path.abspath(found) == os.path.abspath(skip):
            continue
        try:
            with io.open(found, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            continue
    return None


def drift(topology: Dict[str, Any], path: Optional[str] = None,
          skip: Optional[str] = None) -> Dict[str, Any]:
    """What moved between the last kept map and this one."""
    from .diff import compare

    earlier = previous(path, skip=skip)
    if earlier is None:
        return {"changes": [], "summary": "",
                "note": "no earlier map has been kept yet - this one is now "
                        "the first, and the next scan can be compared to it"}
    report = compare(earlier, topology).to_dict()
    if not report.get("changes"):
        report["note"] = "nothing moved since the last map"
    return report

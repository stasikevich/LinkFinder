"""Which setup a device belongs to, remembered between runs.

A lab is not one flat network: it is a handful of benches, each a few devices,
and the question people actually ask is "show me setup 2", not "show me every
host in 10.0.0.0/24". Nothing on a device says which bench it stands on, so the
label has to come from a person - typed once, against a list of real devices
they are looking at, and then remembered so the next scan does not ask again.

A device is remembered by its chassis id first and its management address
second, for the same reason the map identifies it that way: an address can be
handed to another machine, a chassis id cannot.

The file holds site addresses and device names, so it stays beside the user's
other local state and never travels with the tool.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

FILE = ".linkfinder-setups.json"


def store_path(path: Optional[str] = None) -> str:
    if path:
        return path
    return os.path.join(os.path.expanduser("~"), FILE)


def _field(node: Any, name: str, default: Any = "") -> Any:
    if isinstance(node, dict):
        return node.get(name, default)
    return getattr(node, name, default)


def keys_for(node: Any) -> List[str]:
    """Every name this device can be recognised by later, best first.

    Takes the record as the topology JSON holds it or as the crawl holds it;
    the two carry the same three fields and there is no reason to convert.
    """
    out = []
    serial = str(_field(node, "serial") or "").strip().lower()
    if serial:
        out.append("serial:" + serial)
    chassis = str(_field(node, "chassis_id") or "").strip().lower()
    if chassis:
        out.append("chassis:" + chassis)
    for ip in _field(node, "mgmt_ips", []) or []:
        if str(ip).strip():
            out.append("ip:" + str(ip).strip())
    node_id = str(_field(node, "id") or "").strip()
    if node_id and node_id not in out:
        out.append(node_id)
    return out


def lookup(node: Any, extra: Optional[Dict[str, str]] = None,
           path: Optional[str] = None,
           known: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """The setups this one device belongs to, remembered or just listed."""
    table = load(path) if known is None else known
    found: List[str] = []
    for key in keys_for(node):
        found.extend(table.get(key) or [])
    for ip in _field(node, "mgmt_ips", []) or []:
        name = (extra or {}).get(str(ip))
        if name:
            found.append(str(name))
    return sorted(set(found))


def load(path: Optional[str] = None) -> Dict[str, List[str]]:
    """Everybody's marks when a shared folder is set, else this machine's."""
    if path is None:
        from . import shared

        if shared.folder(shared.SETUPS):
            joined, _who = shared.merge_setups()
            return joined
    try:
        with io.open(store_path(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if isinstance(value, list):
            out[str(key)] = [str(v) for v in value if str(v).strip()]
        elif str(value).strip():
            out[str(key)] = [str(value)]
    return out


def save(data: Dict[str, List[str]], path: Optional[str] = None) -> None:
    """Write our own file only. Nobody else's is ever touched.

    On a shared folder this is what removes the need for a lock: two people
    marking at once write two files, and a reader merges them.
    """
    if path is None:
        from . import shared

        own = shared.mine(shared.SETUPS)
        if own:
            _joined, said_by = shared.merge_setups()
            theirs = shared.read_json(own)
            keep = dict(theirs) if isinstance(theirs, dict) else {}
            for key, names in data.items():
                keep[key] = list(names)
            # A key we no longer assert is dropped from our file; if somebody
            # else still asserts it, their file keeps it, which is the point.
            for key in list(keep):
                if key not in data and shared.who() in (said_by.get(key) or []):
                    del keep[key]
            if shared.write_json(own, keep):
                return
    target = store_path(path)
    with io.open(target, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True))


def remember(nodes: Iterable[Dict[str, Any]], names: List[str],
             path: Optional[str] = None) -> int:
    """Put these devices in these setups, and keep it for next time."""
    data = load(path)
    wanted = [n for n in (names or []) if str(n).strip()]
    touched = 0
    for node in nodes:
        keys = keys_for(node)
        if not keys:
            continue
        for key in keys:
            data[key] = sorted(set(wanted))
        touched += 1
    save(data, path)
    return touched


def forget(nodes: Iterable[Dict[str, Any]], path: Optional[str] = None) -> int:
    data = load(path)
    gone = 0
    for node in nodes:
        for key in keys_for(node):
            if data.pop(key, None) is not None:
                gone += 1
    save(data, path)
    return gone


def apply(topology: Dict[str, Any], extra: Optional[Dict[str, str]] = None,
          path: Optional[str] = None) -> int:
    """Stamp every node with what is remembered, plus anything just listed.

    *extra* maps an address to one setup name, which is what a host list file
    carries; what the user marked by hand is already in the store.
    """
    known = load(path)
    listed = {str(k): str(v) for k, v in (extra or {}).items() if str(v).strip()}
    stamped = 0
    for node in topology.get("nodes") or []:
        found = lookup(node, listed, known=known)
        if found:
            node["setups"] = found
            stamped += 1
        else:
            node.setdefault("setups", [])
    return stamped


def names(topology: Dict[str, Any]) -> List[str]:
    """Every setup name present in a topology, for a picker."""
    out = set()
    for node in topology.get("nodes") or []:
        out.update(str(s) for s in (node.get("setups") or []) if str(s).strip())
    return sorted(out)


def stamp(topology: Any, listed: Optional[Dict[str, str]] = None,
          path: Optional[str] = None) -> int:
    """Carry remembered setup names onto a fresh crawl, and say how many.

    A device marked last week is the same device today, so it is looked up by
    serial, chassis id and address rather than by where it sits in the list.
    """
    known = load(path)
    listed = listed or {}
    if not known and not listed:
        return 0
    marked = 0
    for node in topology.nodes.values():
        found = lookup(node, listed, known=known)
        if found:
            node.setups = found
            marked += 1
    return marked

def match_line(topology: Dict[str, Any], line: str) -> Optional[Dict[str, Any]]:
    """Find the device a pasted line refers to - an address, serial, MAC or name.

    One line, one device, and only on an exact match. A near miss is reported
    back as unmatched rather than guessed at: marking the wrong box is worse
    than marking nothing, because nobody goes looking for it afterwards.
    """
    want = str(line or "").strip()
    if not want or want.startswith("#"):
        return None
    low = want.lower()
    flat = low.replace("-", ":").replace(".", ":")
    for node in topology.get("nodes") or []:
        if want in [str(a).strip() for a in (node.get("mgmt_ips") or [])]:
            return node
    for node in topology.get("nodes") or []:
        if str(node.get("serial") or "").strip().lower() == low:
            return node
    for node in topology.get("nodes") or []:
        if str(node.get("chassis_id") or "").strip().lower() == flat:
            return node
    for node in topology.get("nodes") or []:
        if str(node.get("name") or "").strip().lower() == low:
            return node
    for node in topology.get("nodes") or []:
        if str(node.get("id") or "").strip() == want:
            return node
    return None


def mark_many(topology: Dict[str, Any], lines: Iterable[str],
              names: List[str], path: Optional[str] = None
              ) -> Tuple[List[str], List[str]]:
    """Put a whole list in a setup at once. Returns (marked, not recognised).

    A bench is entered as a bench - the addresses or serials you already have
    in a ticket - rather than clicked one at a time on the picture.
    """
    wanted, missing, found = [], [], []
    for line in lines:
        text = str(line or "").strip()
        if not text or text.startswith("#"):
            continue
        node = match_line(topology, text)
        if node is None:
            missing.append(text)
        elif not any(seen is node for seen in wanted):
            # An address and a serial on two lines can name one device; it is
            # marked once and counted once.
            wanted.append(node)
            found.append(str(node.get("name") or node.get("id") or text))
    if wanted:
        if names:
            remember(wanted, list(names), path=path)
        else:
            forget(wanted, path=path)
        for node in wanted:
            node["setups"] = sorted(set(list(node.get("setups") or [])
                                        + list(names))) if names else []
    return found, missing

# The columns of the sheet people actually edit. "setup" comes first because
# it is the only one they type in; the rest are there so a human can tell one
# row from another, and the key is there so a renamed device still matches.
SHEET = ("setup", "device", "serial", "address", "kind", "state",
         "key (do not edit)")


def template_csv(topology: Dict[str, Any]) -> str:
    """Every device in the map, as a sheet with its setup ready to be typed.

    Starting from a blank file is how a bench gets half entered. Starting
    from the list of what is actually there, with today's setups already in
    it, means the work is typing a word next to the rows that need one.
    """
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, lineterminator=chr(10))
    writer.writerow(SHEET)
    for node in topology.get("nodes") or []:
        writer.writerow([
            ", ".join(node.get("setups") or []),
            str(node.get("name") or ""),
            str(node.get("serial") or ""),
            ", ".join(node.get("mgmt_ips") or []),
            str(node.get("kind") or ""),
            str(node.get("state") or ""),
            str(node.get("id") or ""),
        ])
    return buf.getvalue()


def read_marks(text: str) -> Tuple[List[Tuple[str, List[str]]], bool]:
    """Read either sheet or plain list. Returns (pairs, it_was_a_sheet).

    A sheet says what every row's setup should be, so clearing a cell takes
    a device out - what you see in the file is what you get. A plain list
    names devices only; the setup comes from the command.
    """
    import csv
    import io as _io

    lines = (text or "").splitlines()
    head = next((l for l in lines if l.strip()), "")
    if "setup" in head.lower() and "," in head:
        rows = list(csv.DictReader(_io.StringIO(text)))
        out = []
        for row in rows:
            key = (row.get("key (do not edit)") or row.get("serial")
                   or row.get("address") or row.get("device") or "")
            key = str(key).split(",")[0].strip()
            if not key:
                continue
            names = [n.strip() for n in str(row.get("setup") or "").split(",")
                     if n.strip()]
            out.append((key, names))
        return out, True
    return [(l.strip(), []) for l in lines
            if l.strip() and not l.strip().startswith("#")], False


def mark_from_text(topology: Dict[str, Any], text: str,
                   names: Optional[List[str]] = None,
                   path: Optional[str] = None
                   ) -> Tuple[List[str], List[str]]:
    """Apply a sheet or a list. A sheet carries its own setups per row."""
    pairs, was_sheet = read_marks(text)
    if not was_sheet:
        return mark_many(topology, [key for key, _ in pairs], names or [],
                         path=path)
    marked, missing = [], []
    for key, wanted in pairs:
        node = match_line(topology, key)
        if node is None:
            missing.append(key)
            continue
        if wanted:
            remember([node], wanted, path=path)
        else:
            forget([node], path=path)
        node["setups"] = list(wanted)
        marked.append("%s -> %s" % (node.get("name") or key,
                                    ", ".join(wanted) or "no setup"))
    return marked, missing

def who_says(node: Any, path: Optional[str] = None) -> List[str]:
    """Who put this device in a setup - empty when nothing is shared.

    With several people marking, "which bench is this on" has an author, and
    a disagreement is worth seeing rather than silently merging away.
    """
    if path is not None:
        return []
    from . import shared

    if not shared.folder(shared.SETUPS):
        return []
    _joined, said_by = shared.merge_setups()
    out: List[str] = []
    for key in keys_for(node):
        for teller in said_by.get(key) or []:
            if teller not in out:
                out.append(teller)
    return out

def move_to_shared() -> List[str]:
    """Carry marks made before the folder was shared into the team's copy.

    Without this, turning sharing on silently hides everything a person has
    already marked: it stays in their home folder and nobody else sees it.
    Runs once - afterwards there is nothing left to move.
    """
    from . import shared

    own = shared.mine(shared.SETUPS)
    if not own:
        return []
    local = store_path()
    if not os.path.exists(local):
        return []
    try:
        with io.open(local, "r", encoding="utf-8") as handle:
            theirs = json.load(handle)
    except Exception:
        return []
    if not isinstance(theirs, dict) or not theirs:
        return []
    joined, _said = shared.merge_setups()
    moving = {}
    for key, value in theirs.items():
        names = ([str(v) for v in value if str(v).strip()]
                 if isinstance(value, list)
                 else ([str(value)] if str(value).strip() else []))
        if names and sorted(names) != sorted(joined.get(str(key)) or []):
            moving[str(key)] = names
    if not moving:
        return []
    mine_now = shared.read_json(own)
    keep = dict(mine_now) if isinstance(mine_now, dict) else {}
    for key, names in moving.items():
        known = keep.setdefault(key, [])
        for name in names:
            if name not in known:
                known.append(name)
    if not shared.write_json(own, keep):
        return []
    try:
        os.rename(local, local + ".moved-to-shared")
    except OSError:
        pass
    return sorted(moving)


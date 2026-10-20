"""One bench, both directions: what is recorded against what is actually there.

The comparison the rest of the tool does runs from the scan outwards, so a
device the inventory places on a bench that is not physically there cannot
appear in it - and that is the finding people most want. Asking NetBox for a
setup instead gives the other half: a list of what should be here.

Put side by side, a bench of three devices stops being a page of search
results and becomes three rows and a short list of absences.

Nothing here writes to NetBox. A match found only by a hint is marked as
needing a person's eye rather than being presented as fact.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

IN_BOTH = "IN BOTH"
ONLY_NETBOX = "ONLY IN NETBOX"
ONLY_HERE = "ONLY HERE"
# A class this tool has no way into at all. An attenuator, a chamber, a CPE:
# not finding one says nothing about whether it is there, and reporting it as
# missing sends somebody to the lab for nothing.
CANNOT_CHECK = "CANNOT CHECK"

BY_SERIAL = "the serial number"
BY_SERIAL_IN_NAME = "its serial inside the NetBox name"
BY_NAME = "the name, exactly"
BY_NAME_TAIL = "one name ends with the other"
BY_ADDRESS = "an address"

# Which signals settle it on their own, and which only point.
SETTLES_IT = (BY_SERIAL, BY_SERIAL_IN_NAME)
HIGH, MEDIUM, LOW = "high", "medium", "low"

COLUMNS = ("verdict", "setup", "netbox name", "netbox role", "netbox serial",
           "netbox status", "found as", "found serial", "found at",
           "matched by", "confidence", "needs a look")

# A serial short enough to appear inside an unrelated name by chance is not
# evidence of anything.
SHORTEST_SERIAL = 5
SHORTEST_NAME = 6


def tidy(text: Any) -> str:
    return str(text or "").strip()


def role_of(record: Dict[str, Any]) -> str:
    """What the inventory says this is - the column that explains most rows."""
    role = record.get("role")
    if isinstance(role, dict):
        return tidy(role.get("name") or role.get("display"))
    return tidy(role)


def _key(text: Any) -> str:
    """A name reduced to what two systems are likely to agree on."""
    return re.sub(r"[^a-z0-9]+", "", tidy(text).lower())


def _tail_match(a: str, b: str) -> bool:
    """One name is the other with a prefix - MM-0-n78LC-00 vs Integrity-MM-0-...

    A bench device commonly announces itself with the product family in front
    of the name the inventory keeps. That is a real pattern and worth seeing,
    but it is a hint: it says look, not it is so.
    """
    first, second = _key(a), _key(b)
    if not first or not second or first == second:
        return False
    short, long_one = sorted((first, second), key=len)
    return len(short) >= SHORTEST_NAME and long_one.endswith(short)


def _signals(record: Dict[str, Any], node: Dict[str, Any]) -> List[str]:
    """Every reason to think this record and this device are the same thing."""
    found = []
    serial = tidy(record.get("serial")).lower()
    ours = tidy(node.get("serial")).lower()
    if serial and ours and serial == ours:
        found.append(BY_SERIAL)
    name = tidy(record.get("name"))
    if ours and len(ours) >= SHORTEST_SERIAL and ours in name.lower():
        found.append(BY_SERIAL_IN_NAME)
    if serial and len(serial) >= SHORTEST_SERIAL \
            and serial in tidy(node.get("name")).lower():
        found.append(BY_SERIAL_IN_NAME)
    if name and _key(name) == _key(node.get("name")):
        found.append(BY_NAME)
    elif _tail_match(name, node.get("name")):
        found.append(BY_NAME_TAIL)
    held = {tidy(a) for a in (record.get("addresses") or []) if a}
    mine = {tidy(a) for a in (node.get("mgmt_ips") or []) if a}
    if held & mine:
        found.append(BY_ADDRESS)
    return found


def _standing(signals: List[str]) -> Tuple[str, bool]:
    """How far to trust a match, and whether to ask somebody."""
    if any(s in SETTLES_IT for s in signals):
        return HIGH, False
    if BY_NAME in signals:
        return (MEDIUM, False) if len(signals) > 1 else (MEDIUM, True)
    if signals:
        return (MEDIUM if len(signals) > 1 else LOW), True
    return LOW, True


def compare(setup: str, recorded: Iterable[Dict[str, Any]],
            found: Iterable[Dict[str, Any]],
            out_of_reach: Optional[Iterable[str]] = None
            ) -> List[Dict[str, Any]]:
    """Rows for one bench: matched pairs first, then each side's absences.

    `recorded` are NetBox devices carrying the setup's tag; `found` are the
    devices this scan puts in the same setup.
    """
    recorded, found = list(recorded), list(found)
    beyond = {str(r).strip().lower() for r in (out_of_reach or []) if str(r).strip()}
    pairs: List[Tuple[Dict, Dict, List[str]]] = []
    taken_here = set()

    # Strongest evidence first, so a weak hint can never steal a device from
    # the record that actually proves it.
    for wanted in (SETTLES_IT, (BY_NAME,), (BY_ADDRESS,), (BY_NAME_TAIL,)):
        for record in recorded:
            if any(r is record for r, _n, _s in pairs):
                continue
            for index, node in enumerate(found):
                if index in taken_here:
                    continue
                signals = _signals(record, node)
                if any(s in wanted for s in signals):
                    pairs.append((record, node, signals))
                    taken_here.add(index)
                    break

    rows = []
    for record, node, signals in pairs:
        confidence, ask = _standing(signals)
        rows.append({
            "verdict": IN_BOTH, "setup": setup,
            "netbox name": tidy(record.get("name")),
            "netbox role": role_of(record),
            "netbox serial": tidy(record.get("serial")),
            "netbox status": tidy((record.get("status") or {}).get("label")
                                  if isinstance(record.get("status"), dict)
                                  else record.get("status")),
            "found as": tidy(node.get("name")) or tidy(node.get("id")),
            "found serial": tidy(node.get("serial")),
            "found at": ", ".join(node.get("mgmt_ips") or []),
            "matched by": ", ".join(signals),
            "confidence": confidence,
            "needs a look": "yes" if ask else "",
            "url": tidy(record.get("display_url")),
        })

    matched = {id(r) for r, _n, _s in pairs}
    for record in recorded:
        if id(record) in matched:
            continue
        role = role_of(record)
        rows.append({
            "verdict": (CANNOT_CHECK if role.lower() in beyond
                        else ONLY_NETBOX),
            "setup": setup,
            "netbox name": tidy(record.get("name")),
            "netbox role": role,
            "netbox serial": tidy(record.get("serial")),
            "netbox status": tidy((record.get("status") or {}).get("label")
                                  if isinstance(record.get("status"), dict)
                                  else record.get("status")),
            "found as": "", "found serial": "", "found at": "",
            "matched by": "", "confidence": "",
            # What was tried, not an accusation. "Nothing answered" reads as
            # "it is gone" even when the tool has no way to reach the class.
            "needs a look": ("this tool has no way into a %s" % role
                             if role.lower() in beyond else
                             "no serial, name or address in this scan matched"),
            "url": tidy(record.get("display_url")),
        })
    for index, node in enumerate(found):
        if index in taken_here:
            continue
        rows.append({
            "verdict": ONLY_HERE, "setup": setup,
            "netbox name": "", "netbox role": "", "netbox serial": "",
            "netbox status": "",
            "found as": tidy(node.get("name")) or tidy(node.get("id")),
            "found serial": tidy(node.get("serial")),
            "found at": ", ".join(node.get("mgmt_ips") or []),
            "matched by": "", "confidence": "",
            "needs a look": "not recorded under this setup", "url": "",
        })

    order = {IN_BOTH: 0, ONLY_NETBOX: 1, ONLY_HERE: 2, CANNOT_CHECK: 3}
    rows.sort(key=lambda r: (order[r["verdict"]],
                             (r["netbox name"] or r["found as"]).lower()))
    return rows


def summary(rows: List[Dict[str, Any]]) -> str:
    """The one line that answers "is this bench what we think it is"."""
    counted: Dict[str, int] = {}
    for row in rows:
        counted[row["verdict"]] = counted.get(row["verdict"], 0) + 1
    if not counted:
        return "nothing on either side"
    # Every record absent and nothing of ours present is almost never a bench
    # that vanished - it is a setup nobody has marked here yet, or a name that
    # is not the tag. Saying "8 missing" would send somebody to the lab.
    if counted.get(ONLY_NETBOX) == len(rows):
        return ("%d recorded, none of them marked here - mark this bench "
                "first (--mark-setup), or the name is not the NetBox tag"
                % counted[ONLY_NETBOX])
    said = ", ".join("%d %s" % (counted[k], k.lower())
                     for k in (IN_BOTH, ONLY_NETBOX, ONLY_HERE) if k in counted)
    asked = sum(1 for r in rows if r["needs a look"] == "yes")
    return said + (", %d to confirm" % asked if asked else "")


def to_csv(rows: List[Dict[str, Any]]) -> str:
    """The same rows as a file a spreadsheet opens."""
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, lineterminator=chr(10))
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row.get(name, "") for name in COLUMNS])
    return buf.getvalue()


def in_setup(topology: Dict[str, Any], setup: str) -> List[Dict[str, Any]]:
    """The devices this map puts in a named setup."""
    want = setup.strip().lower()
    out = []
    for node in topology.get("nodes") or []:
        names = [str(s).strip().lower() for s in (node.get("setups") or [])]
        if want in names:
            out.append(node)
    return out


def split_name(wanted: str) -> Tuple[str, str]:
    """"bench by the window=bench-a" -> the bench here, and the tag there.

    A setup is what people here call a bench; a tag is what the inventory
    calls it. They are usually the same word and are under no obligation to
    be, so the pairing can always be said out loud.
    """
    text = str(wanted or "").strip()
    if "=" in text:
        here, _sep, there = text.partition("=")
        return here.strip(), there.strip()
    return text, ""


def tag_for(setup: str, tag_of: Optional[Dict[str, str]] = None) -> str:
    """The NetBox tag to ask about, and never a silent guess."""
    named = (tag_of or {}).get(setup)
    if named:
        return str(named).strip()
    return re.sub(r"[^a-z0-9]+", "-", str(setup).lower()).strip("-")


def review(topology: Dict[str, Any], client, setups: Iterable[str],
           tag_of: Optional[Dict[str, str]] = None,
           say: Optional[Any] = None,
           beyond_reach: Optional[Iterable[str]] = None
           ) -> List[Dict[str, Any]]:
    """One bench at a time, asking NetBox for the whole of it."""
    rows: List[Dict[str, Any]] = []
    pairs = dict(tag_of or {})
    beyond = list(beyond_reach or [])
    for wanted in setups:
        here, there = split_name(wanted)
        if there:
            pairs[here] = there
        slug = tag_for(here, pairs)
        if say and not (tag_of or {}).get(here) and not there:
            say("%s: asking NetBox for the tag %r - name it yourself with "
                "%s=<tag> if that is not the one" % (here, slug, here))
        recorded = client.devices_with_tag(slug)
        rows.extend(compare(here, recorded, in_setup(topology, here),
                            out_of_reach=beyond))
    return rows

def roles_never_matched(rows: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """Classes this comparison never once matched, with how many there are.

    Offered rather than assumed. A list of unreachable roles baked into the
    code would be wrong the day somebody adds a role, and wrong silently.
    One yes per class settles it for every later run.
    """
    matched, missed = set(), {}
    for row in rows:
        role = tidy(row.get("netbox role"))
        if not role:
            continue
        if row["verdict"] == IN_BOTH:
            matched.add(role.lower())
        elif row["verdict"] == ONLY_NETBOX:
            missed[role] = missed.get(role, 0) + 1
    return sorted(((role, count) for role, count in missed.items()
                   if role.lower() not in matched),
                  key=lambda pair: (-pair[1], pair[0]))


def out_of_reach(config: Optional[Dict[str, Any]] = None) -> List[str]:
    """The classes somebody has already said this tool cannot reach."""
    held = (config or {}).get("out_of_reach_roles") or []
    if isinstance(held, str):
        held = [held]
    return [str(r).strip() for r in held if str(r).strip()]


def remember_out_of_reach(config: Dict[str, Any],
                          roles: Iterable[str]) -> Dict[str, Any]:
    """The shape the defaults file keeps them in.

    A role written with a leading minus is taken back out. A confirmation
    that cannot be undone is a trap: somebody says yes to the wrong class
    once and the tool hides a real absence for ever.
    """
    known = list(out_of_reach(config))
    for role in roles:
        name = str(role).strip()
        if not name:
            continue
        if name.startswith("-"):
            gone = name[1:].strip().lower()
            known = [k for k in known if k.lower() != gone]
            continue
        if name.lower() not in {k.lower() for k in known}:
            known.append(name)
    return {"out_of_reach_roles": sorted(known)}


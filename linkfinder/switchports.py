"""Matching a switch port the scan saw to the one the inventory records.

LLDP says `1/1/49`, `Te0/9`, `sfp-sfpplus5`. NetBox says `Port49`, `port9`,
`SFP+5`. The relationship is regular but it is a convention, not a guarantee,
and guessing silently is exactly what must not happen here: a wrong port is
worse than no port, because somebody will unplug a cable on the strength of
it.

So this proposes a rule and shows its working. The rule is **per switch** -
one confirmation covers every port on it - which is why the human cost of
being certain is a single yes rather than fifty-two.

Nothing here writes to the inventory; it only decides which two names refer
to the same hole in the same box.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

# How certain a proposal is, in words a person can act on.
CERTAIN = "every port the scan saw lands on exactly one record"
LIKELY = "most ports land, a few do not"
WEAK = "too few ports agree to call it a rule"

# A port name nearly always ends in the number that identifies it. Everything
# before that is the vendor's way of saying "ethernet".
TAIL = re.compile(r"(\d+)\s*$")


def port_number(name: str) -> Optional[int]:
    """The number a port name ends in - 1/1/49 and Port49 are both 49."""
    found = TAIL.search(str(name or "").strip())
    return int(found.group(1)) if found else None


def shape_of(names: Iterable[str]) -> str:
    """What every NetBox name on this switch looks like, minus its number."""
    stems = set()
    for name in names:
        text = str(name or "").strip()
        found = TAIL.search(text)
        if found:
            stems.add(text[:found.start(1)])
    return stems.pop() if len(stems) == 1 else ""


def propose(switch: str, seen: Iterable[str],
            recorded: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Suggest how this switch's port names line up, with the evidence.

    `seen` are the names the scan heard over LLDP; `recorded` are the
    interface names the inventory holds for the same switch.
    """
    seen = [s for s in (str(x).strip() for x in seen) if s]
    recorded = [s for s in (str(x).strip() for x in recorded) if s]
    if not seen or not recorded:
        return None

    by_number: Dict[int, List[str]] = {}
    for name in recorded:
        number = port_number(name)
        if number is not None:
            by_number.setdefault(number, []).append(name)

    lands, lost, shown = {}, [], []
    for name in sorted(set(seen)):
        number = port_number(name)
        hits = by_number.get(number) if number is not None else None
        if number is None or not hits or len(hits) > 1:
            lost.append(name)
            continue
        lands[name] = hits[0]
        if len(shown) < 3:
            shown.append("%s = %s" % (name, hits[0]))

    if not lands:
        return None
    share = float(len(lands)) / len(set(seen))
    standing = CERTAIN if not lost else (LIKELY if share >= 0.6 else WEAK)
    return {
        "switch": switch,
        "rule": "the number the port name ends in",
        "stem": shape_of(recorded),
        "matched": lands,
        "unmatched": lost,
        "covers": len(by_number),
        "standing": standing,
        "example": ", ".join(shown),
        "confirmed": False,
    }


def resolve(rule: Dict[str, Any], port: str,
            recorded: Iterable[str]) -> str:
    """The inventory's name for a port, once the rule has been confirmed."""
    if not rule or not rule.get("confirmed"):
        return ""
    direct = (rule.get("matched") or {}).get(str(port).strip())
    if direct:
        return direct
    number = port_number(port)
    if number is None:
        return ""
    for name in recorded:
        if port_number(name) == number:
            return str(name)
    return ""


def confirmed_rules(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The switches somebody has already said yes to."""
    held = (config or {}).get("switch_ports") or {}
    if not isinstance(held, dict):
        return {}
    out = {}
    for switch, rule in held.items():
        if isinstance(rule, dict) and rule.get("confirmed"):
            out[str(switch)] = rule
    return out


def remember(config: Dict[str, Any], switch: str,
             rule: Dict[str, Any]) -> Dict[str, Any]:
    """The shape the defaults file keeps a confirmed rule in."""
    held = dict((config or {}).get("switch_ports") or {})
    kept = dict(rule or {})
    kept["confirmed"] = True
    kept.pop("unmatched", None)
    held[str(switch)] = kept
    return {"switch_ports": held}


def disagreements(links: List[Dict[str, Any]], rules: Dict[str, Any],
                  inventory: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Where the cable the scan found is not the cable on record.

    Only switches with a confirmed rule are judged. Without one there is no
    disagreement to report - only two naming schemes, which is not a fault.
    """
    out = []
    for link in links or []:
        switch = str(link.get("switch") or "")
        rule = rules.get(switch)
        if not rule:
            continue
        ours = resolve(rule, link.get("port") or "",
                       (inventory.get(switch) or {}).get("ports") or [])
        theirs = str((inventory.get(switch) or {}).get("connected", {})
                     .get(link.get("device") or "", "") or "")
        if not ours or not theirs:
            continue
        if ours != theirs:
            out.append({"switch": switch, "device": link.get("device"),
                        "scan_says": ours, "netbox_says": theirs})
    return out


def seen_on_switches(topology: Dict[str, Any]) -> Dict[str, List[str]]:
    """Which switch port names this map actually landed on, by switch name."""
    nodes = {n.get("id"): n for n in topology.get("nodes") or []}
    found: Dict[str, set] = {}
    for edge in topology.get("edges") or []:
        for near, port in ((edge.get("a"), edge.get("a_iface")),
                           (edge.get("b"), edge.get("b_iface"))):
            node = nodes.get(near) or {}
            name = str(node.get("name") or "").strip()
            port = str(port or "").strip()
            is_switch = str(node.get("kind") or "") == "switch"
            if name and port and port != "?" and is_switch:
                found.setdefault(name, set()).add(port)
    return dict((k, sorted(v)) for k, v in found.items())


def proposals(topology: Dict[str, Any], client,
              config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """One suggestion per switch this map touched that NetBox also holds.

    A switch already confirmed is not asked about again, and a switch NetBox
    does not hold under the name it announces is reported as that, which is
    its own finding rather than a failure.
    """
    already = confirmed_rules(config or {})
    seen = seen_on_switches(topology)
    wanted = [name for name in seen if name not in already]
    if not wanted:
        return []
    records = client.devices_by("name", wanted)
    by_name = dict((str(d.get("name") or "").lower(), d) for d in records)
    ports = client.ports_of([d.get("id") for d in records])
    out = []
    for name in sorted(wanted):
        record = by_name.get(name.lower())
        if not record:
            out.append({"switch": name, "standing": "not in NetBox under the "
                        "name it announces", "matched": {}, "covers": 0,
                        "example": "", "unmatched": seen[name],
                        "confirmed": False})
            continue
        plan = propose(name, seen[name],
                       ports.get(str(record.get("id"))) or [])
        if plan:
            plan["netbox_id"] = record.get("id")
            plan["netbox_name"] = record.get("name")
            plan["url"] = record.get("display_url") or ""
            out.append(plan)
    return out

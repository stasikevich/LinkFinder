"""One address, two owners - and knowing when that is not what it looks like.

A duplicate IP is a fault that hides: connections work intermittently, from
some places and not others, and every individual device insists it is
configured correctly. The giveaway is that two machines on the same segment
have learned different hardware addresses for it.

The evidence here is the neighbour cache, which is hearsay. An entry can be
minutes old, left over from a device that has since gone, or a gateway that
legitimately answers with a virtual address. So nothing from it is ever
reported above medium confidence, both accounts are shown, and the age of the
entry is part of the finding rather than hidden behind it.

A MAC in two places, within a single scan, is a duplicate - not a move. Telling
a move from a duplicate needs two observations separated by time, which is what
comparing two snapshots is for.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

NEIGH_COMMAND = "ip -4 neigh show 2>/dev/null; ip -6 neigh show 2>/dev/null"

# An entry the kernel itself does not believe is not evidence of anything.
USELESS_STATES = ("FAILED", "INCOMPLETE", "NONE")
IGNORED_MACS = ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")


def parse_neigh(text: str) -> Dict[str, Dict[str, str]]:
    """{address: {mac, state, dev}} from `ip neigh show`, believable rows only."""
    from .netaddr import is_ip, normalise_ip

    out: Dict[str, Dict[str, str]] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or "lladdr" not in parts:
            continue
        address = normalise_ip(parts[0])
        if not is_ip(address):
            continue
        mac = parts[parts.index("lladdr") + 1].lower()
        if mac in IGNORED_MACS or mac.startswith(("01:00:5e", "33:33")):
            continue
        state = parts[-1].upper()
        if state in USELESS_STATES:
            continue
        dev = parts[parts.index("dev") + 1] if "dev" in parts else ""
        out[address] = {"mac": mac, "state": state, "dev": dev}
    return out


def collect(run: Callable[[str], str],
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Dict[str, str]]:
    """One cheap command; a host without `ip` simply contributes nothing."""
    say = log or (lambda _m: None)
    try:
        return parse_neigh(run(NEIGH_COMMAND))
    except Exception as exc:
        say("  neighbour cache: %s" % exc)
        return {}


def _owner(result) -> str:
    return getattr(result, "device_name", "") or getattr(result, "target", "")


def find_duplicate_ips(results) -> List[Dict[str, Any]]:
    """Addresses that two hosts have learned different hardware addresses for.

    Agreement is the normal case and is silent. Disagreement is reported with
    both accounts and the state of each entry, because the likeliest innocent
    explanation - one host holding a stale entry for a device that moved - looks
    exactly the same from here.
    """
    seen: Dict[str, Dict[str, List[str]]] = {}
    for result in results or []:
        who = _owner(result)
        for address, entry in ((getattr(result, "extra", {}) or {}).get(
                "neighbours") or {}).items():
            seen.setdefault(address, {}).setdefault(entry["mac"], []).append(
                "%s says %s (%s)" % (who, entry["mac"], entry.get("state", "?")))

    out: List[Dict[str, Any]] = []
    for address, by_mac in sorted(seen.items()):
        if len(by_mac) < 2:
            continue
        evidence = [line for lines in by_mac.values() for line in lines]
        out.append({
            "address": address,
            "macs": sorted(by_mac),
            "verdict": "mismatch",
            "confidence": "medium",
            "detail": "%s resolves to %s depending on who is asked"
                      % (address, " and ".join(sorted(by_mac))),
            "evidence": sorted(evidence) + [
                "a neighbour cache can hold a stale entry for a device that moved"],
        })
    return out


def find_duplicate_macs(results) -> List[Dict[str, Any]]:
    """The same hardware address configured on two machines we scanned.

    Only our own interfaces count here. A MAC seen in two forwarding tables is
    usually one device reachable two ways, which is not a fault; a MAC that is
    the configured address of two different servers is.
    """
    owners: Dict[str, List[str]] = {}
    for result in results or []:
        who = _owner(result)
        for nb in getattr(result, "neighbors", []) or []:
            mac = (getattr(nb, "local_mac", "") or "").lower()
            if not mac or mac in IGNORED_MACS:
                continue
            where = "%s %s" % (who, getattr(nb, "local_iface", ""))
            if where not in owners.setdefault(mac, []):
                owners[mac].append(where)

    out: List[Dict[str, Any]] = []
    for mac, places in sorted(owners.items()):
        hosts = {place.split(" ")[0] for place in places}
        if len(hosts) < 2:
            continue
        out.append({
            "mac": mac,
            "places": sorted(places),
            "verdict": "mismatch",
            "confidence": "high",
            "detail": "%s is configured on %s" % (mac, " and ".join(sorted(places))),
            "evidence": ["interface address read from /sys on each host"],
        })
    return out


def find(results, log: Optional[Callable[[str], None]] = None) -> Dict[str, List]:
    """Both searches, with every finding also recorded on the device it concerns."""
    say = log or (lambda _m: None)
    ips = find_duplicate_ips(results)
    macs = find_duplicate_macs(results)
    for finding in ips:
        say("  duplicate address: %s" % finding["detail"])
    for finding in macs:
        say("  duplicate MAC: %s" % finding["detail"])
    if ips or macs:
        for result in results or []:
            extra = getattr(result, "extra", None)
            if extra is None:
                continue
            extra["duplicates"] = {"ips": ips, "macs": macs}
    return {"ips": ips, "macs": macs}


def render(found: Dict[str, List]) -> str:
    """The report, or nothing at all when there is nothing to say."""
    ips = (found or {}).get("ips") or []
    macs = (found or {}).get("macs") or []
    if not ips and not macs:
        return ""
    lines = ["Address conflicts"]
    for finding in ips:
        lines.append("  %s" % finding["detail"])
        for line in finding["evidence"]:
            lines.append("      %s" % line)
    for finding in macs:
        lines.append("  %s" % finding["detail"])
    return "\n".join(lines)

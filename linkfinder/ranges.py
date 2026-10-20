"""Turning a range of addresses into the addresses themselves.

A person holding a bench rarely holds its addresses; they hold "the .74
network" or ".200 to .210". Typing those out is the work this removes.

Both forms a person actually writes are taken:

    10.0.0.0/24          the usable hosts in that network
    10.0.0.5-10.0.0.40   that run
    10.0.0.5-40          the same, written short

Nothing is expanded silently past LIMIT. Sweeping a /16 is sixty-five
thousand login attempts, and a tool that starts one because of a typo is a
tool nobody is allowed to run twice.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ipaddress
import re
from typing import List, Tuple

LIMIT = 1024

_SHORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3})$")
_FULL = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})$")


def looks_like_a_range(value: str) -> bool:
    text = str(value or "").strip()
    return bool("/" in text or _SHORT.match(text) or _FULL.match(text))


def expand(value: str, limit: int = LIMIT) -> Tuple[List[str], str]:
    """The addresses in a range, or an empty list and the reason why not."""
    text = str(value or "").strip()
    if not text:
        return [], ""

    if "/" in text:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as why:
            return [], "%s is not a network (%s)" % (text, why)
        if net.num_addresses > limit + 2:
            return [], ("%s holds %d addresses - more than the %d this will "
                        "sweep. Narrow it, or scan the parts you mean"
                        % (text, net.num_addresses, limit))
        hosts = [str(ip) for ip in net.hosts()] or [str(net.network_address)]
        return hosts, ""

    first = last = ""
    short = _SHORT.match(text)
    full = _FULL.match(text)
    if short:
        first = short.group(1)
        last = ".".join(first.split(".")[:3] + [short.group(2)])
    elif full:
        first, last = full.group(1), full.group(2)
    else:
        return [], ""

    try:
        low = ipaddress.ip_address(first)
        high = ipaddress.ip_address(last)
    except ValueError as why:
        return [], "%s is not a range (%s)" % (text, why)
    if int(high) < int(low):
        return [], "%s runs backwards - put the lower address first" % text
    count = int(high) - int(low) + 1
    if count > limit:
        return [], ("%s holds %d addresses - more than the %d this will "
                    "sweep. Narrow it" % (text, count, limit))
    return [str(ipaddress.ip_address(n))
            for n in range(int(low), int(high) + 1)], ""


def expand_all(values: List[str], limit: int = LIMIT
               ) -> Tuple[List[str], List[str], List[str]]:
    """Every value, with ranges opened out.

    Returns (addresses, the ranges that were opened, the reasons any were
    refused) so the caller can say what it did rather than quietly doing it.
    """
    out: List[str] = []
    opened: List[str] = []
    refused: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not looks_like_a_range(text):
            out.append(text)
            continue
        hosts, why = expand(text, limit)
        if why:
            refused.append(why)
            continue
        if not hosts:
            out.append(text)
            continue
        opened.append("%s -> %d address(es)" % (text, len(hosts)))
        out.extend(hosts)
    return list(dict.fromkeys(a for a in out if a)), opened, refused

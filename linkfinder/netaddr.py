"""Deciding whether an address is one we can actually connect to.

A device can advertise an address that is perfectly valid and still useless to
us: an IPv6 link-local needs a zone index to be reachable at all, a loopback
answers only on the device itself, and Supermicro publishes a MAC in the same
field as the management IP. Treating any of those as a management address costs
a connection attempt and then reports the device as unreachable, which hides the
real reason. One place decides, so every layer agrees.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

MAC_LOOK = re.compile(r"^(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")

REASON_LINK_LOCAL = "link-local, needs a zone index to be reachable"
REASON_LOOPBACK = "loopback, answers only on the device itself"
REASON_UNSPECIFIED = "unspecified address"
REASON_MULTICAST = "multicast, not a host address"
REASON_NOT_AN_IP = "not an IP address"


def is_mac(text: str) -> bool:
    return bool(text and MAC_LOOK.match(text.strip()))


def _v4_octets(text: str) -> Optional[List[int]]:
    parts = text.split(".")
    if len(parts) != 4:
        return None
    out = []
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return None
        out.append(int(part))
    return out


def _v6_groups(text: str) -> Optional[List[int]]:
    """Parse an IPv6 address, with or without :: and with or without zone."""
    text = text.split("%", 1)[0].strip()
    if not text or ":" not in text:
        return None
    if text.count("::") > 1:
        return None
    head, _sep, tail = text.partition("::")
    try:
        left = [int(g, 16) for g in head.split(":") if g != ""]
        right = [int(g, 16) for g in tail.split(":") if g != ""]
    except ValueError:
        return None
    if any(g < 0 or g > 0xFFFF for g in left + right):
        return None
    if _sep:
        gap = 8 - len(left) - len(right)
        if gap < 1:
            return None
        return left + [0] * gap + right
    return left + right if len(left + right) == 8 else None


def is_ip(text: str) -> bool:
    """True for a syntactically valid IPv4 or IPv6 address. A MAC is not one."""
    if not text:
        return False
    text = text.strip()
    if is_mac(text):
        return False
    return _v4_octets(text) is not None or _v6_groups(text) is not None


def compress_ipv6(text: str) -> str:
    """fe80:0000:0000:0000:7a9a:18ff:fe19:1234 -> fe80::7a9a:18ff:fe19:1234.

    An uncompressed address is correct but unreadable and will not fit a table
    column, and nobody recognises their own gear in it.
    """
    groups = _v6_groups(text)
    if groups is None:
        return text
    zone = text.split("%", 1)[1] if "%" in text else ""
    best_at, best_len, at, length = -1, 0, -1, 0
    for i, g in enumerate(groups + [1]):
        if g == 0:
            at = i if at < 0 else at
            length += 1
            continue
        if length > best_len:
            best_at, best_len = at, length
        at, length = -1, 0
    parts = ["%x" % g for g in groups]
    if best_len > 1:
        out = ":".join(parts[:best_at]) + "::" + ":".join(parts[best_at + best_len:])
    else:
        out = ":".join(parts)
    return out + ("%" + zone if zone else "")


def normalise_ip(text: str) -> str:
    """Canonical form of an address, left untouched when it is not one."""
    if not text:
        return ""
    text = text.strip()
    if _v6_groups(text) is not None:
        return compress_ipv6(text)
    return text


def mgmt_problem(text: str) -> str:
    """Why this address cannot be used to reach a device, or "" when it can."""
    if not text:
        return REASON_NOT_AN_IP
    text = text.strip()
    if not is_ip(text):
        return REASON_NOT_AN_IP
    octets = _v4_octets(text)
    if octets is not None:
        if octets[0] == 127:
            return REASON_LOOPBACK
        if octets == [0, 0, 0, 0]:
            return REASON_UNSPECIFIED
        if octets[0] == 169 and octets[1] == 254:
            return REASON_LINK_LOCAL
        if 224 <= octets[0] <= 239:
            return REASON_MULTICAST
        return ""
    groups = _v6_groups(text) or []
    if groups == [0] * 8:
        return REASON_UNSPECIFIED
    if groups == [0] * 7 + [1]:
        return REASON_LOOPBACK
    if groups and (groups[0] & 0xFFC0) == 0xFE80:
        return REASON_LINK_LOCAL if "%" not in text else ""
    if groups and (groups[0] & 0xFF00) == 0xFF00:
        return REASON_MULTICAST
    return ""


def is_reachable_mgmt(text: str) -> bool:
    """Can we open a connection to this address from somewhere else?"""
    return mgmt_problem(text) == ""


def pick_mgmt_ip(candidates: Iterable[str]) -> Tuple[str, str]:
    """Best usable address from a list, else the first one and why it is no good.

    Keeping the unusable address rather than dropping it matters: it is still
    the only identity some neighbours publish, and the graph should show it with
    the reason attached instead of showing a nameless node.
    """
    seen: List[str] = []
    for raw in candidates:
        value = normalise_ip(raw or "")
        if value and value not in seen:
            seen.append(value)
    for value in seen:
        if is_reachable_mgmt(value):
            return value, ""
    if seen:
        return seen[0], mgmt_problem(seen[0])
    return "", REASON_NOT_AN_IP

"""Reading what a person typed, and saying so plainly when it cannot be read.

A field nobody can parse has three possible fates, and only one is honest:

- **Crash.** `int("1O")` raises, and in a windowed build there is no console
  to raise into, so the button simply does nothing and the engineer is left
  guessing.
- **Quietly substitute a default.** The worst of the three. Someone typing
  `1O` meant ten and gets zero, and nothing on screen disagrees with them.
  Worse still for a fence: a mistyped network silently becomes no fence at
  all, so the safety control fails open, which is the one direction a
  safety control must never fail.
- **Name the field, quote what was in it, say what was expected, refuse to
  start.** This module.

Every limit is bounded at both ends. The lower bound catches a zero that
would make the run pointless; the upper bound catches the extra digit that
turns an eight-hop walk into eighty and a long afternoon of other people's
switches answering questions.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import ipaddress
from typing import Any, Dict, List, Optional, Tuple

BOUNDS = {
    "max depth": (0, 8),
    "max devices": (1, 500),
    "parallel targets": (1, 64),
    "parallel devices": (1, 32),
    "seconds to watch": (1, 3600),
    "reading interval": (0.1, 60.0),
    "expected speed": (0, 400000),
    "SSH port": (1, 65535),
    "BMC port": (1, 65535),
}


def _said(text: Any) -> str:
    return str("" if text is None else text).strip()


def whole_number(text: Any, field: str,
                 default: Optional[int] = None) -> Tuple[Optional[int], str]:
    """A count, or a complaint naming the field and quoting what was in it."""
    low, high = BOUNDS.get(field, (0, 2 ** 31))
    said = _said(text)
    if not said:
        return default, ""
    try:
        value = int(said, 10)
    except ValueError:
        if said.replace(",", ".").replace(".", "", 1).lstrip("-").isdigit():
            return None, ("%s: %r is not a whole number" % (field, said))
        return None, ("%s: %r is not a number" % (field, said))
    if value < low or value > high:
        return None, ("%s: %d is outside %d to %d" % (field, value, low, high))
    return value, ""


def decimal_number(text: Any, field: str,
                   default: Optional[float] = None
                   ) -> Tuple[Optional[float], str]:
    """The same, for the one setting measured in seconds and fractions."""
    low, high = BOUNDS.get(field, (0.0, float(2 ** 31)))
    said = _said(text)
    if not said:
        return default, ""
    try:
        value = float(said.replace(",", "."))
    except ValueError:
        return None, ("%s: %r is not a number" % (field, said))
    if value != value or value in (float("inf"), float("-inf")):
        return None, ("%s: %r is not a number" % (field, said))
    if value < low or value > high:
        return None, ("%s: %s is outside %s to %s"
                      % (field, said, low, high))
    return value, ""


def networks(items: Any, field: str = "stay inside"
             ) -> Tuple[List[str], List[str]]:
    """The address fence, or every reason it could not be read.

    Returns nothing usable when any entry fails. A fence with one entry
    missing is not a smaller fence, it is a different one, and the caller
    must stop rather than walk out through the gap.
    """
    if isinstance(items, str):
        items = items.replace(",", " ").split()
    good: List[str] = []
    complaints: List[str] = []
    for item in items or []:
        said = _said(item)
        if not said:
            continue
        try:
            net = ipaddress.ip_network(said, strict=False)
        except ValueError:
            hint = (" - write a network such as 10.0.0.0/24"
                    if "/" not in said else "")
            complaints.append("%s: %r is not a network%s" % (field, said, hint))
            continue
        good.append(str(net))
    return ([], complaints) if complaints else (good, [])


def settled(pair: Tuple[Any, str], fallback: Any) -> Any:
    """The value when it could be read, the fallback when it could not."""
    return pair[0] if pair[0] is not None else fallback


def cost_note(depth: Optional[int], devices: Optional[int],
              fence: List[str]) -> str:
    """What the settings allow at worst, said before the walk starts."""
    return ("up to %s device(s), %s hop(s) out from the seed, %s"
            % (devices if devices is not None else "?",
               depth if depth is not None else "?",
               "inside %s" % ", ".join(fence) if fence
               else "with no address fence"))


def check_walk(depth: Any, devices: Any, scope: Any,
               field_depth: str = "max depth",
               field_devices: str = "max devices"
               ) -> Tuple[Dict[str, Any], List[str]]:
    """Everything the map page asks for, checked in one go.

    Every complaint at once rather than one per attempt: an engineer who
    mistyped two fields should learn that now, not after fixing the first.
    """
    complaints: List[str] = []
    hops, why = whole_number(depth, field_depth, 0)
    if why:
        complaints.append(why)
    count, why = whole_number(devices, field_devices, 25)
    if why:
        complaints.append(why)
    fence, bad = networks(scope)
    complaints.extend(bad)
    return ({"max_depth": hops, "max_devices": count, "scope": fence},
            complaints)

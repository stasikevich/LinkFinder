"""Which ports have ever announced themselves, so the rest are not waited on.

Measured on one host: 145 seconds to interrogate it, 127 of them spent
listening for discovery frames. The listening already stops early - as
soon as every port being watched has produced a frame - but that host had
four ports and two of them have no neighbour at all. The two that will
never speak held the other two hostage for the whole window, every time.

So what is worth remembering between scans is not the answer. It is which
ports produced one. A port that has spoken before is expected to speak
again, and once all of those have landed there is nothing left to wait
for.

Every port is still listened to. A port with nothing recorded against it
is captured exactly as before and, if it does speak, is remembered from
then on. The only thing that changes is what the waiting is waiting for.

The trap in this is obvious and is handled: a cache that always cuts the
listening short can never learn that a quiet port has a neighbour now. So
every host gets a full-length listen again after FULL_AGAIN, and the
report says when that last happened.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, Optional, Set

# One full-length listen per host per half day. Long enough that repeat
# scans are quick, short enough that a cable moved this morning is found
# this afternoon without anybody being told to force it.
FULL_AGAIN = 12 * 3600

# The shortest a shortened listen may be. LLDP goes out every 30 seconds
# by default (IEEE 802.1AB msgTxInterval), and a wire can carry more than
# one talker - a management port facing both a switch and a virtual
# machine was measured reporting only the switch when the listening
# stopped sooner. One full cycle plus room for the grace period to run.
AT_LEAST = 50


def _where(path: Optional[str] = None) -> str:
    if path:
        return path
    # Beside the settings, NOT in the maps folder. This is the tool's own
    # memory of which ports have spoken; mixed in with the maps it looked like
    # one, and somebody picked it to open and got a baffling error. Internal
    # files live where internal files live.
    from .engine import config_path

    folder = os.path.join(os.path.dirname(os.path.abspath(config_path())),
                          ".linkfinder")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return os.path.join(folder, "ports-heard.json")


def _load(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(_where(path), encoding="utf-8") as handle:
            held = json.load(handle)
        return held if isinstance(held, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(held: Dict[str, Any], path: Optional[str] = None) -> None:
    try:
        with open(_where(path), "w", encoding="utf-8") as handle:
            json.dump(held, handle, indent=1, sort_keys=True)
    except OSError:
        pass                      # a convenience, never a step that fails


def talkers(host: str, path: Optional[str] = None) -> Set[str]:
    """Ports on this host that have produced a discovery frame before."""
    held = (_load(path).get(str(host)) or {}).get("heard") or {}
    return {str(name) for name in held}


def due_full_listen(host: str, now: Optional[float] = None,
                    path: Optional[str] = None) -> bool:
    """Whether this host has gone long enough to deserve the whole window.

    True for a host never scanned, which is what keeps the first run of
    every machine exactly as thorough as it was before any of this.
    """
    when = (_load(path).get(str(host)) or {}).get("full_listen")
    if not when:
        return True
    return (now or time.time()) - float(when) >= FULL_AGAIN


def remember(host: str, spoke: Iterable[str], listened: Iterable[str],
             full: bool, now: Optional[float] = None,
             path: Optional[str] = None) -> None:
    """Record what was heard. A port that falls silent is forgotten, so it
    stops being waited for; one that speaks is remembered from now on."""
    stamp = now or time.time()
    held = _load(path)
    mine = held.setdefault(str(host), {})
    heard = mine.setdefault("heard", {})
    for name in spoke:
        heard[str(name)] = stamp
    if full:
        # Only a full-length listen is evidence that a port is silent. A
        # shortened one proves nothing about what it did not hear.
        for name in listened:
            if str(name) not in set(str(s) for s in spoke):
                heard.pop(str(name), None)
        mine["full_listen"] = stamp
    _save(held, path)


# Off, and here is the measurement that turned it off.
#
# One host: 145 seconds became 60, with every neighbour still found, three
# runs in a row. A whole site of nineteen: 661 seconds became 416 - and the
# map came back with ten fewer links. Two cold passes of the same site
# differ by one link, so that was not the network being moody.
#
# Exactly two hosts accounted for all of it, and they are the two whose
# radio units are reached by IPv6 neighbour discovery inside a pod's
# network namespace: one went from four neighbours to none, the other from
# six to three. The other seventeen were identical to the entry.
#
# The reason is the thing being shortened. While a capture runs the
# interface is listening, the kernel sees the advertisements going past and
# fills its neighbour cache - which is read afterwards. Listening for less
# time genuinely sees less. The links were not dropped by a bug; they were
# never heard.
#
# So this stays off until it can tell those hosts apart from the rest. It
# is not deleted because the measurement is worth keeping and the machinery
# is correct - it is the premise that was wrong.
ENABLED = False


def window(host: str, ports: Iterable[str], seconds: int,
           now: Optional[float] = None, path: Optional[str] = None):
    """How long to listen, and how many ports must land before stopping.

    Returns (seconds, expect, full). `expect` of None means the caller
    should wait the whole window, which is what a first scan does - and,
    while ENABLED is false, what every scan does.
    """
    if not ENABLED:
        return seconds, None, True
    wanted = [str(p) for p in ports]
    if due_full_listen(host, now=now, path=path):
        return seconds, None, True
    known = talkers(host, path=path)
    expect = len([p for p in wanted if p in known])
    return max(AT_LEAST, min(seconds, AT_LEAST * 2)), expect, False

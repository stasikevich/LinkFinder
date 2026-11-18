"""Mini-Circuits programmable RF switches, read over their HTTP API.

A lab's RF path runs through these - an SP6T, or a bank of them - and which
output each one is thrown to decides what is actually connected to what. They
have no shell: a plain SSH probe logs in nowhere and sees nothing. But the box
answers a handful of read-only HTTP queries, and MN?/SN? name it well enough to
put it on the map and hold it against NetBox by its serial, exactly like any
other device.

The commands here only ever read. The SET... commands that would throw a switch
are deliberately not here - a topology tool has no business changing the RF path
under a running measurement. The split (an injectable getter, a pure parser) is
what lets a future controller reuse the same fingerprint without this module
growing a write path.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

Get = Callable[[str], str]

# Read-only queries. MN/SN/FIRMWARE name the box; SWPORT is the live RF path.
# An unknown command answers "-99 ...", which is how a non-switch is told apart.
QUERIES = ("MN?", "SN?", "FIRMWARE?", "SWPORT?")
_MODEL = re.compile(r"\bRC-\d*SP\d+T", re.I)        # RC-1SP6T-A12, RC-2SP6T-A12


def _value(text: str) -> str:
    """Strip the `MN=` / `SN=` echo the box prefixes a reply with."""
    s = str(text or "").strip()
    if s.startswith("-99"):
        return ""
    return s.split("=", 1)[1].strip() if "=" in s else s


def looks_like_switch(text: str) -> bool:
    """Whether a reply to MN? names a Mini-Circuits switch.

    An unknown-command error ("-99 ...") echoes the model too, so it is
    excluded - only a clean reply counts.
    """
    s = str(text or "").strip()
    return bool(_MODEL.search(s)) and not s.startswith("-99")


def read(get: Get, log: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """Model, serial, firmware and the live switch port, or None if not one.

    `get` takes a bare query (`MN?`) and returns the body; injecting it keeps
    the HTTP out of the parser so the whole thing is testable from captures.
    """
    say = log or (lambda _m: None)
    model = _value(get("MN?"))
    if not looks_like_switch(model):
        return None
    out: Dict[str, Any] = {"maker": "Mini-Circuits", "model": model}
    serial = _value(get("SN?"))
    if serial:
        out["serial"] = serial
    firmware = _value(get("FIRMWARE?"))
    if firmware:
        out["firmware"] = firmware
    swport = _value(get("SWPORT?"))
    if swport != "":
        out["switch_port"] = swport
    say("  Mini-Circuits %s%s" % (model, " SN %s" % serial if serial else ""))
    return out


def http_get(target: str, port: int = 80, timeout: float = 5.0) -> Get:
    """A getter for the switch's HTTP API.

    The command IS the path, trailing `?` and all (`/MN?`). requests normalises
    that `?` away as an empty query string and the box then answers "unknown
    command", so http.client is used - it sends the path exactly as written.
    """
    import http.client

    def get(cmd: str) -> str:
        conn = None
        try:
            conn = http.client.HTTPConnection(target, port, timeout=timeout)
            conn.request("GET", "/" + cmd)
            resp = conn.getresponse()
            return resp.read().decode("utf-8", "replace") if resp.status == 200 else ""
        except Exception:
            return ""
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return get


def is_switch(target: str, port: int = 80, timeout: float = 4.0) -> bool:
    """One MN? query: is there a Mini-Circuits switch answering here?"""
    return looks_like_switch(http_get(target, port, timeout)("MN?"))


def probe(target: str, port: int = 80, timeout: float = 5.0,
          log: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """Read the switch over HTTP, or None when nothing Mini-Circuits answers."""
    return read(http_get(target, port, timeout), log=log)

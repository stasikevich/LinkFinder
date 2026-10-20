"""Where a team's answers live, when the team is more than one person.

Until now everything the tool remembers - which bench a device is on, the
maps it kept - sat in one person's home folder. That is right for one laptop
and wrong for a team: two engineers scan the same bench and get two different
answers, and nothing says why.

Point `shared` at a folder everybody can reach and it moves there. The layout
is chosen so a network share cannot corrupt it:

    <shared>/setups/<user>@<host>.json     written only by that person
    <shared>/history/<when>-<user>.json    the maps anyone has drawn

**Each person writes only their own file and reads everybody's.** No locking,
no last-writer-wins, no half-written file if the share blinks. A device two
people put in different benches is in both - which the model already allows,
because a shared uplink really does serve several.

If the folder cannot be reached the tool falls back to the home folder and
says so. A share that is down must never stop a scan.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import getpass
import io
import json
import os
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

ENV = "LINKFINDER_SHARED"
SETUPS = "setups"
HISTORY = "history"

_WARNED = set()


def who() -> str:
    """The name this machine writes under - a person at a machine."""
    try:
        user = getpass.getuser() or "someone"
    except Exception:
        user = "someone"
    try:
        host = socket.gethostname() or "somewhere"
    except Exception:
        host = "somewhere"
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", "%s@%s" % (user, host))


def asked_for(config: Optional[Dict[str, Any]] = None) -> str:
    """The folder somebody asked for, from the environment or the file.

    The defaults file is read when no config is handed in, so setting
    `shared` once is enough - every part of the tool then agrees.
    """
    named = os.environ.get(ENV)
    if named:
        return named.strip()
    if config is None:
        try:
            from .engine import load_config

            config = load_config()
        except Exception:
            config = {}
    return str((config or {}).get("shared") or "").strip()


def folder(kind: str, config: Optional[Dict[str, Any]] = None) -> str:
    """The folder to use for one kind of record, making it if it can.

    Returns "" when there is no shared folder or it cannot be reached, which
    the caller reads as "use the home folder".
    """
    where = asked_for(config)
    if not where:
        return ""
    target = os.path.join(where, kind)
    try:
        if not os.path.isdir(target):
            os.makedirs(target)
        return target if os.access(target, os.W_OK) else ""
    except OSError:
        return ""


def say_where(config: Optional[Dict[str, Any]] = None) -> str:
    """One line naming where the team's answers are kept, for the log.

    Silence here was the real fault: a person had no way to know their marks
    were private to their own laptop.
    """
    where = asked_for(config)
    if not where:
        return ("setups and kept maps are on this machine only - point "
                "%s at a folder the team can reach to share them" % ENV)
    if folder(SETUPS, config):
        return "setups and kept maps are shared, in %s, as %s" % (where, who())
    key = "unreachable:" + where
    if key not in _WARNED:
        _WARNED.add(key)
    return ("%s cannot be reached, so setups and kept maps are on this "
            "machine only" % where)


def mine(kind: str, config: Optional[Dict[str, Any]] = None) -> str:
    """The one file this machine is allowed to write."""
    where = folder(kind, config)
    return os.path.join(where, who() + ".json") if where else ""


def everyones(kind: str, config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Every file to read for this kind, newest last so later wins on ties."""
    where = folder(kind, config)
    if not where:
        return []
    found = []
    try:
        for name in sorted(os.listdir(where)):
            if name.endswith(".json"):
                found.append(os.path.join(where, name))
    except OSError:
        return []
    return found


def read_json(path: str) -> Any:
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def write_json(path: str, data: Any) -> bool:
    """Write our own file whole, through a temporary one.

    A share that goes away mid-write leaves the old file intact rather than
    half of a new one.
    """
    if not path:
        return False
    temporary = path + ".writing"
    try:
        with io.open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True))
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)
        return True
    except OSError:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return False


def merge_setups(config: Optional[Dict[str, Any]] = None
                 ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Everybody's marks, and who asserts each one.

    Returns (device key -> setups, device key -> the people who say so).
    """
    joined: Dict[str, List[str]] = {}
    said_by: Dict[str, List[str]] = {}
    for path in everyones(SETUPS, config):
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        teller = os.path.basename(path)[:-5]
        for key, value in data.items():
            names = ([str(v) for v in value if str(v).strip()]
                     if isinstance(value, list)
                     else ([str(value)] if str(value).strip() else []))
            if not names:
                continue
            known = joined.setdefault(str(key), [])
            for name in names:
                if name not in known:
                    known.append(name)
            tellers = said_by.setdefault(str(key), [])
            if teller not in tellers:
                tellers.append(teller)
    return joined, said_by

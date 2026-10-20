"""Default lab logins, carried by the internal build and nothing else.

A lab is full of machines on the same handful of factory passwords, and
typing one in for every scan is friction the internal team should not have.
So the internal build carries those defaults and tries them - but ONLY for a
host the person gave no login of their own. The moment a login is supplied by
hand, that one is used and, if it is refused, the host is reported refused:
the tool never then works through every default it knows behind the person's
back, which could lock an account and would hide a simple typo.

Where they live matters as much as what they are. The list is read from a
`logins.json` bundled beside the binary, exactly like the brand and the icon,
and `branding/` is deliberately never committed. The open build and a run
from the source tree find no file and get an empty list, so neither the
public download nor the repository carries a password. There is a build gate
that proves the open binaries contain none.

Nothing here is a secret worth guarding - these are published factory
defaults - but they are still not the repository's to hold.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import List

from .sshx import Creds

_CACHE: List[Creds] = []
_LOADED = False
_LOCK = threading.Lock()
BUNDLE_NAME = "logins.json"


def _folders():
    """The same places branding looks: the bundle, then beside the module.

    A working tree is NOT searched. Running from source must behave like the
    open build whatever happens to sit in branding/ on disk, so a developer
    never scans a customer with lab defaults by accident.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        yield base


def default_creds() -> List[Creds]:
    """The baked logins, in the order to try them, or [] when there are none.

    A scan probes many hosts on threads at once, so the load is guarded: the
    flag is set only AFTER the cache is filled, under a lock. Setting it first
    let a second thread see "loaded" and read the still-empty cache, so that
    host fell back to an empty login and tried the local account by accident.
    """
    global _LOADED
    if _LOADED:
        return list(_CACHE)
    with _LOCK:
        if _LOADED:
            return list(_CACHE)
        loaded: List[Creds] = []
        for folder in _folders():
            path = os.path.join(folder, BUNDLE_NAME)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(data, list):
                loaded = _read(data)
                break
        _CACHE.extend(loaded)
        _LOADED = True
    return list(_CACHE)


def _read(rows) -> List[Creds]:
    out: List[Creds] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = str(row.get("user") or "").strip()
        if not user:
            continue
        out.append(Creds(
            user=user,
            password=str(row.get("password") or ""),
            become_password=str(row.get("become_password") or ""),
            port=int(row.get("port") or 22)))
    return out


def _bundled(name: str):
    """A JSON file bundled beside the binary, or None. Internal build only.

    Like the logins: read from sys._MEIPASS and nowhere else, so the open
    build and a run from source carry none, and a build gate proves it.
    """
    for folder in _folders():
        try:
            with open(os.path.join(folder, name), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            continue
    return None


# Logins for a radio's NETCONF, carried by the internal build only. A scan
# tries them against a discovered radio to read its serial; the open build has
# none and simply reads no serial. Published factory defaults, not the repo's
# to hold - they live in the gitignored branding/radio-logins.json.
RADIO_BUNDLE = "radio-logins.json"


def radio_creds() -> List[tuple]:
    """(user, password) pairs to try against a radio's NETCONF, in order."""
    data = _bundled(RADIO_BUNDLE)
    out = []
    for row in data or []:
        if isinstance(row, dict) and row.get("user"):
            out.append((str(row["user"]), str(row.get("password") or "")))
    return out


# Where a host writes a radio's serial in a file whose NAME is that serial -
# a fallback when the radio's own M-plane is down. The glob and prefix are
# site-specific, so they live in the gitignored branding bundle, never in the
# tool: the generic reader just applies whatever rules it is handed.
RADIO_FILES_BUNDLE = "radio-sources.json"


def radio_file_rules() -> List[dict]:
    """Rules {glob, prefix} for lifting radio serials off a host's own files."""
    data = _bundled(RADIO_FILES_BUNDLE)
    return [r for r in (data or []) if isinstance(r, dict) and r.get("glob")]

"""Remembered switches - opt in only.

Every LLDP/CDP frame carries the sender's management address, and a switch that
answers SNMP will name its own neighbours. With --learn those get written here,
so the tool builds its own inventory instead of asking to be told --switch
again. Nothing is written without --learn, and SNMP communities are only stored
on top of that with --remember-credentials: this file holds site addresses and
must never end up in a repository.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".linkfinder-switches.json")
RETRY_BAD_AFTER = 24 * 3600
_LOCK = threading.Lock()


def store_path() -> str:
    return os.environ.get("LINKFINDER_STORE") or DEFAULT_PATH


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load(path: Optional[str] = None) -> Dict[str, dict]:
    try:
        with open(path or store_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: Dict[str, dict], path: Optional[str] = None) -> None:
    target = path or store_path()
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    except OSError:
        pass


def _usable_ip(ip: str) -> bool:
    if not ip or ":" in ip:
        return False
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) < 256 for p in parts):
        return False
    return ip not in ("0.0.0.0", "127.0.0.1") and not ip.startswith("169.254.")


def learn(ip: str, name: str = "", descr: str = "", seen_from: str = "",
          path: Optional[str] = None) -> bool:
    """Record a switch we have evidence of. Returns True if it is new."""
    if not _usable_ip(ip):
        return False
    with _LOCK:
        data = load(path)
        entry = data.get(ip)
        fresh = entry is None
        if fresh:
            entry = {"first_seen": _now(), "seen_from": []}
        entry["last_seen"] = _now()
        if name:
            entry["name"] = name
        if descr:
            entry["descr"] = descr
        if seen_from and seen_from not in entry.get("seen_from", []):
            entry.setdefault("seen_from", []).append(seen_from)
            entry["seen_from"] = entry["seen_from"][-10:]
        data[ip] = entry
        save(data, path)
        return fresh


def mark_snmp(ip: str, ok: bool, community: str = "", name: str = "", descr: str = "",
              store_community: bool = False, path: Optional[str] = None) -> None:
    """community is a credential: it is only written when the caller opts in."""
    if not _usable_ip(ip):
        return
    with _LOCK:
        data = load(path)
        entry = data.setdefault(ip, {"first_seen": _now(), "seen_from": []})
        entry["snmp_ok"] = ok
        entry["snmp_last_try"] = time.time()
        entry["last_seen"] = _now()
        if ok and community and store_community:
            entry["community"] = community
        if name:
            entry["name"] = name
        if descr:
            entry["descr"] = descr
        save(data, path)


def candidates(default_community: str, include_failed: bool = False,
               path: Optional[str] = None) -> List[str]:
    """Switch specs to ask, newest knowledge first; ip@community form."""
    data = load(path)
    out = []
    for ip, entry in sorted(data.items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True):
        if not include_failed and entry.get("snmp_ok") is False:
            age = time.time() - float(entry.get("snmp_last_try") or 0)
            if age < RETRY_BAD_AFTER:
                continue
        community = entry.get("community") or default_community
        out.append("%s@%s" % (ip, community) if community else ip)
    return out


def forget(ip: str = "", path: Optional[str] = None) -> int:
    with _LOCK:
        data = load(path)
        if not ip:
            count = len(data)
            save({}, path)
            return count
        if ip in data:
            del data[ip]
            save(data, path)
            return 1
        return 0


def describe(path: Optional[str] = None) -> str:
    data = load(path)
    if not data:
        return "no switches remembered yet (%s)" % store_path()
    rows = ["remembered switches (%s):" % store_path()]
    for ip, entry in sorted(data.items()):
        snmp = entry.get("snmp_ok")
        state = "snmp ok" if snmp else ("snmp no answer" if snmp is False else "snmp untried")
        rows.append("  %-16s %-20s %-16s %s" % (
            ip, entry.get("name", "-"), state,
            "seen from " + ", ".join(entry.get("seen_from", [])[:3]) if entry.get("seen_from") else ""))
        if entry.get("descr"):
            rows.append("  %-16s   %s" % ("", entry["descr"][:90]))
    return "\n".join(rows)

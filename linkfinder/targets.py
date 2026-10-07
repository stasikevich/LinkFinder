"""Working out what the user meant by a target, and saying so when they slipped.

Two jobs, both small and both previously missing. A list of addresses can carry
a login for the ones that need their own - `root@10.0.0.5`, or
`admin:secret@10.0.0.9` - borrowed from ssh so there is nothing new to learn
and nothing to change for the common case where every machine takes the same
password.

The second job is catching 198.51.100.1801. Four dotted numbers with one out of
range is not a hostname anybody meant to type, and reporting it as "no SSH, no
HTTPS" sends a person to check a firewall when the fault is an extra digit.
Anything that is not four dotted numbers is left alone - a real hostname must
still work.

Credentials typed into a target are held in memory for that run only; nothing
here writes them anywhere.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

NUMERIC = re.compile(r"^[\d.]+$")
HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


@dataclass
class Target:
    """One address, and the login it was given if it was given one."""

    host: str
    user: str = ""
    password: str = ""
    problem: str = ""

    @property
    def has_login(self) -> bool:
        return bool(self.user or self.password)


def _check(host: str) -> str:
    """Why this cannot be a target, or "" when it can."""
    from .netaddr import is_ip

    if not host:
        return "empty"
    if ":" in host and is_ip(host):
        return ""
    if NUMERIC.match(host):
        return "" if is_ip(host) else "not a valid IPv4 address"
    if is_ip(host):
        return ""
    if not HOSTNAME.match(host):
        return "not an address or a hostname"
    return ""


def parse_one(value: str) -> Target:
    """`admin:secret@host`, `root@host` or plain `host`.

    Split from the right: a password may contain an @, a hostname may not.
    """
    text = (value or "").strip()
    if not text:
        return Target(host="", problem="empty")
    user = password = ""
    if "@" in text:
        login, _sep, host = text.rpartition("@")
        user, _colon, password = login.partition(":")
    else:
        host = text
    return Target(host=host, user=user, password=password, problem=_check(host))


def parse(values: List[str], targets_file: str = "") -> List[Target]:
    """Everything the user listed, in order, each with its own verdict."""
    raw: List[str] = []
    for value in values or []:
        raw.extend(part for part in (value or "").replace(",", " ").split() if part)
    if targets_file:
        with open(targets_file, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#")[0].strip()
                if line:
                    raw.append(line)
    out: List[Target] = []
    seen = set()
    for item in raw:
        target = parse_one(item)
        if target.host and target.host in seen:
            continue
        if target.host:
            seen.add(target.host)
        out.append(target)
    return out


def split(values: List[str], targets_file: str = ""
          ) -> Tuple[List[str], Dict[str, Tuple[str, str]], List[Target]]:
    """(hosts worth trying, per-host logins, the ones that are not addresses)."""
    parsed = parse(values, targets_file)
    good = [t for t in parsed if not t.problem]
    bad = [t for t in parsed if t.problem]
    logins = {t.host: (t.user, t.password) for t in good if t.has_login}
    return [t.host for t in good], logins, bad


def complain(bad: List[Target]) -> List[str]:
    """One line per target that will not be tried, naming the reason."""
    return ["%s: %s - skipped" % (t.host or "(blank)", t.problem) for t in bad]

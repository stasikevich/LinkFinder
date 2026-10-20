"""Patterns across a whole run that no single host's error can show.

A wall of identical failures reads as "the tool is broken", and that reading
is almost always wrong: when every host that used the shared login refuses it
and every host with its own login gets in, the login is what is wrong. One
line saying so is worth more than fourteen lines repeating the symptom.

This lives outside the window on purpose. The Linux build has no window, so a
diagnosis that only the window can make is a diagnosis Linux users never get.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# The words an actual refusal carries. Matching the friendly label shown on
# screen instead of the raw error is how this check silently did nothing:
# the error says "SSH rejected the credentials (Authentication failed.)",
# which contains none of the words the screen uses.
REFUSAL_MARKS = ("rejected the credentials", "authentication failed",
                 "did not get us in", "permission denied")


def was_refused(error: str) -> bool:
    low = str(error or "").lower()
    return any(mark in low for mark in REFUSAL_MARKS)


FRIENDLY = (
    ("rejected the credentials", "SSH authentication failed"),
    ("did not get us in", "SSH could not log in"),
    ("no snmp response", "SNMP timeout"),
    ("did not answer snmp", "SNMP timeout"),
    ("no redfish service", "Redfish unavailable or wrong credentials"),
    ("exposes no lldp", "LLDP data unavailable on this device"),
    ("no lldp/cdp received", "LLDP data unavailable on these ports"),
    ("nothing to talk to", "No reachable management service"),
    ("could not resolve a mac", "MAC address unknown for this target"),
    ("paramiko is not installed", "paramiko is missing - pip install paramiko"),
)


def friendly_error(text: str) -> str:
    low = (text or "").lower()
    for needle, message in FRIENDLY:
        if needle in low:
            return message
    return (text or "").split("\n")[0][:120]


def _short(line: str, limit: int = 96) -> str:
    """The finding, without the list of identifiers behind it.

    A finding is usually a fact, a colon, then the evidence - a clock domain
    naming every grandmaster it heard, say. On one line above a table the
    fact is the part that fits; the evidence stays in the log.
    """
    text = str(line or "")
    head = text.split(":")[0].strip()
    if head and len(head) < len(text) and len(text) > limit:
        return head
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def failed_hosts(results) -> List[Tuple[str, str]]:
    """Which addresses came back with an error, each with its own reason.

    One line of text for a run over twenty hosts cannot say which of them
    refused the password, so the address travels with the reason everywhere
    the reason goes.
    """
    return [(str(getattr(r, "target", "") or "?"), friendly_error(r.error))
            for r in results or [] if getattr(r, "error", "")]


REFUSED = "SSH authentication failed"


def run_verdict(resolved: int, bad: List[Tuple[str, str]]) -> Tuple[str, str]:
    """The one line at the bottom, and the colour it earns.

    A run that resolved something and still lost hosts is neither done nor
    failed, and calling it done is how a dead host goes unnoticed.
    """
    if not bad:
        return (("Done - %d link(s) resolved" % resolved) if resolved
                else "Nothing resolved", "UP" if resolved else "DOWN")
    if len(bad) == 1:
        host, why = bad[0]
        if resolved:
            return ("Done - %d link(s), but %s: %s" % (resolved, host, why),
                    "DEGRADED")
        return ("%s: %s" % (host, why), "DOWN")
    reasons = sorted({why for _host, why in bad})
    said = "; ".join(reasons[:2]) + (", ..." if len(reasons) > 2 else "")
    if resolved:
        return ("Done - %d link(s), but %d host(s) failed: %s"
                % (resolved, len(bad), said), "DEGRADED")
    return ("%d host(s) failed: %s - see the log for which"
            % (len(bad), said), "DOWN")


def shared_login_refused(results: Iterable[Any],
                         per_host: Optional[Dict[str, Any]] = None) -> str:
    """Say so when it is the one shared login that is being rejected."""
    own = {str(h) for h in (per_host or {})}
    refused, worked = [], []
    for found in results or []:
        host = str(getattr(found, "target", "") or "")
        if host in own:
            continue                      # it used a login of its own
        if was_refused(getattr(found, "error", "")):
            refused.append(host)
        elif not getattr(found, "error", ""):
            worked.append(host)
    if len(refused) < 2 or worked:
        return ""                         # not a pattern, just a bad host
    said = ("every one of the %d host(s) using the shared login refused it"
            % len(refused))
    if own:
        said += (", while the %d host(s) with a login of their own got in"
                 % len(own))
    return (said + " - that points at the login and password, not at the "
            "hosts. A stray space or character in the password field does "
            "exactly this.")


def banner(message: str) -> str:
    """The same text, framed, for a log where it would otherwise scroll past."""
    if not message:
        return ""
    width = min(78, max(len(line) for line in _wrap(message)))
    rule = "-" * width
    return "\n".join([rule] + _wrap(message, width) + [rule])


def _wrap(text: str, width: int = 78):
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return lines

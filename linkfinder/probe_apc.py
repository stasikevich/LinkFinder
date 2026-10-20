"""A rack PDU, and the one thing only it knows: what is plugged into it.

A network map answers which switch port a machine is in. It has nothing to
say about which socket feeds it, and that is the question asked when a
rack has to be powered down, or when a server is dead and nobody knows
whether it has power at all.

An APC network management card answers both halves. `about` gives a model,
a serial and a hardware address - enough to be a device on the map and to
be held against NetBox. `olStatus` gives every outlet, its state, and the
name somebody typed on it when the rack was built. Those names are usually
the only written record of the wiring that exists.

Getting in takes a terminal. The card has no exec channel at all: handing
it a command the ordinary way returns nothing and the session ends, which
is why a plain SSH probe sees a device that logs in and says nothing. It
wants an interactive shell, a prompt to wait for, and commands typed into
it.

Nothing here switches anything. The outlet commands that could are not
sent, and `apc` has no business turning a lab off.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

PROMPT = "apc>"
# What the card is asked. Every one of these only reads.
COMMANDS = ("about", "netstat", "olStatus all")

# "MAC Address:      28 29 86 2D 7C D2" - spaces, not colons, and the rest
# of the file wants the ordinary form.
MAC_RE = re.compile(r"^\s*MAC Address:\s*([0-9A-Fa-f]{2}(?:[ :-][0-9A-Fa-f]{2}){5})",
                    re.MULTILINE)
FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ()]+?):\s{2,}(.+?)\s*$", re.MULTILINE)
OUTLET_RE = re.compile(r"^\s*(\d+):\s*(.*?):\s*(On|Off)\s*$",
                       re.MULTILINE | re.IGNORECASE)
IPV4_RE = re.compile(r"IPv4\s+\d+\s+\w+\s+((?:\d{1,3}\.){3}\d{1,3}/\d{1,2})")


# The card's SSH server names the library it is built on, and no general
# purpose machine does. Reading it costs one connection and no login, so a
# device that cannot give a shell is recognised before anything tries to
# run a shell command on it.
BANNER_HINTS = ("cryptlib", "apc")


def banner_of(host: str, port: int = 22, timeout: float = 4.0) -> str:
    """What the SSH server calls itself, before any login is attempted."""
    import socket

    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return sock.recv(200).decode("utf-8", "replace").strip()
    except Exception:
        return ""
    finally:
        try:
            sock.close()
        except Exception:
            pass


def banner_says_no_shell(banner: str) -> bool:
    """Whether this is a box that logs in and then has no shell to offer."""
    low = str(banner or "").lower()
    if "openssh" in low or "dropbear" in low:
        return False
    return any(hint in low for hint in BANNER_HINTS)


def looks_like_apc(text: str) -> bool:
    """Whether what came back is an APC card rather than a shell."""
    low = (text or "").lower()
    return PROMPT in (text or "") or "american power conversion" in low \
        or "apc os(aos)" in low


def tidy_mac(text: str) -> str:
    bits = re.split(r"[ :-]", str(text or "").strip())
    return ":".join(b.lower() for b in bits if b) if len(bits) == 6 else ""


def parse_about(text: str) -> Dict[str, str]:
    """Model, serial and hardware address out of `about`.

    The card prints several blocks with the same field names - the PDU
    itself first, then its management card. The first of each is the PDU,
    which is the thing in the rack.
    """
    out: Dict[str, str] = {}
    for name, value in FIELD_RE.findall(text or ""):
        key = name.strip().lower()
        if key in ("model number", "serial number", "hardware revision",
                   "manufacture date", "management uptime") and key not in out:
            out[key] = value.strip()
    found = MAC_RE.search(text or "")
    if found:
        out["mac"] = tidy_mac(found.group(1))
    return out


def parse_outlets(text: str) -> List[Dict[str, str]]:
    """Every outlet, what it is called and whether it is on.

    The name is whatever somebody typed when the rack was built. It is not
    a hostname and must never be treated as one - it is a label, and the
    only record of the wiring that usually exists.
    """
    out = []
    for number, label, state in OUTLET_RE.findall(text or ""):
        out.append({"outlet": number, "label": label.strip(),
                    "state": state.capitalize()})
    return out


def parse_addresses(text: str) -> List[str]:
    return [found for found in IPV4_RE.findall(text or "")
            if not found.startswith("127.")]


def read(host: str, user: str, password: str, port: int = 22,
         timeout: float = 20.0,
         log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Log in, type the three read-only commands, come back with the answers."""
    say = log or (lambda _m: None)
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    said: Dict[str, str] = {}
    try:
        client.connect(host, port=port, username=user, password=password,
                       timeout=timeout, look_for_keys=False, allow_agent=False)
        shell = client.invoke_shell(width=200, height=200)
        banner = _drain(shell, 3.0)
        if not looks_like_apc(banner):
            say("  %s answered, but not as an APC card" % host)
            return {}
        for command in COMMANDS:
            said[command] = _ask(shell, command)
    finally:
        try:
            client.close()
        except Exception:
            pass

    found: Dict[str, Any] = {"kind": "pdu"}
    found.update(parse_about(said.get("about", "")))
    outlets = parse_outlets(said.get("olStatus all", ""))
    if outlets:
        found["outlets"] = outlets
    addresses = parse_addresses(said.get("netstat", ""))
    if addresses:
        found["addresses"] = addresses
    say("  APC %s, serial %s, %d outlet(s)"
        % (found.get("model number", "?"), found.get("serial number", "?"),
           len(outlets)))
    return found


def _drain(shell, wait: float) -> str:
    time.sleep(wait)
    out = b""
    while shell.recv_ready():
        out += shell.recv(65535)
        time.sleep(0.3)
    return out.decode("utf-8", "replace")


def _ask(shell, command: str, wait: float = 3.0) -> str:
    shell.send(command + "\r\n")
    return _drain(shell, wait)

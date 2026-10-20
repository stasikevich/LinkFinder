"""What a device is, asked without a password.

A server tells you what it is before it asks who you are. An SSH daemon
sends its version string first; a web server names itself in a header; a
TLS certificate carries a subject. None of it needs an account, and none of
it is a login attempt, so nothing lands in anybody's failed-auth log.

That matters most where credentials are hardest to come by. A radio unit on
a fronthaul link has no address an operator knows and no password in the
scanner's hands, and ends up on the map as a bare MAC. Measured on a real
one, three seconds of listening give:

    169.254.12.11:22   SSH-2.0-dropbear_2020.80      an embedded box
    169.254.12.11:830  SSH-2.0-libssh_0.8.90         NETCONF, so M-plane

while the base station beside it answers `OpenSSH ... Ubuntu` on 22 and
nothing on 830 - which is how the two are told apart rather than assumed.

The probe writes nothing except the one HTTP HEAD a web server needs before
it will speak, and HEAD is the method that exists to ask without fetching.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import re
from typing import Any, Callable, Dict, List, Optional

PORTS = ((22, "ssh"), (830, "netconf"), (443, "https"), (80, "http"),
         (23, "telnet"))

WAIT = 1.5

SCRIPT = r'''
import socket, ssl, sys, threading
PORTS = %(ports)s
TARGETS = %(targets)s
WAIT = %(wait)s
_lock = threading.Lock()
_out = []
def probe(ip, port):
    s = socket.socket()
    s.settimeout(WAIT)
    try:
        s.connect((ip, port))
    except Exception:
        s.close()
        return
    lines = ["OPEN|%%s|%%s" %% (ip, port)]
    said = ""
    try:
        if port == 443:
            ctx = ssl._create_unverified_context()
            w = ctx.wrap_socket(s, server_hostname=ip)
            cert = w.getpeercert() or {}
            bits = []
            for part in (cert.get("subject") or ()):
                for key, value in part:
                    bits.append("%%s=%%s" %% (key, value))
            said = ", ".join(bits) or str(w.version() or "")
        elif port in (80, 8080):
            s.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            said = s.recv(400).decode("utf-8", "replace")
        else:
            said = s.recv(200).decode("utf-8", "replace")
    except Exception:
        said = ""
    s.close()
    first = said.replace("\r", "\n").split("\n")[0].strip()
    if port in (80, 8080):
        for line in said.replace("\r", "\n").split("\n"):
            if line.lower().startswith("server:"):
                first = line.strip()
                break
    if first:
        lines.append("SAID|%%s|%%s|%%s" %% (ip, port, first[:160]))
    with _lock:
        _out.extend(lines)
# Every address and port at once: a filtered port costs the whole timeout, and
# one after another that was minutes on a host with many fronthaul addresses.
_threads = []
for ip in TARGETS:
    for port, _name in PORTS:
        t = threading.Thread(target=probe, args=(ip, port))
        t.daemon = True
        t.start()
        _threads.append(t)
for t in _threads:
    t.join(WAIT + 2)
sys.stdout.write("\n".join(_out) + ("\n" if _out else ""))
'''

OPEN_LINE = re.compile(r"^OPEN\|([^|]+)\|(\d+)$")
SAID_LINE = re.compile(r"^SAID\|([^|]+)\|(\d+)\|(.*)$")

EMBEDDED = ("dropbear", "libssh", "wolfssh", "busybox")


def parse(text: str) -> Dict[str, Dict[str, Any]]:
    """What each address had open, and what it volunteered there."""
    out: Dict[str, Dict[str, Any]] = {}
    for raw in (text or "").splitlines():
        hit = OPEN_LINE.match(raw.strip())
        if hit:
            seen = out.setdefault(hit.group(1), {"open": [], "said": {}})
            port = int(hit.group(2))
            if port not in seen["open"]:
                seen["open"].append(port)
            continue
        hit = SAID_LINE.match(raw.strip())
        if hit and hit.group(3).strip():
            seen = out.setdefault(hit.group(1), {"open": [], "said": {}})
            seen["said"][int(hit.group(2))] = hit.group(3).strip()
    return out


def describe(record: Dict[str, Any]) -> Dict[str, Any]:
    """Turn what was heard into a sentence and, where fair, a kind.

    A kind is only claimed when two independent things agree: NETCONF on
    830 says the device is managed the way an O-RAN radio is managed, and
    an embedded SSH daemon says it is not a general-purpose machine. One
    alone is not enough - plenty of switches speak NETCONF, and plenty of
    appliances run dropbear.
    """
    opened = list(record.get("open") or [])
    said = record.get("said") or {}
    names = dict(PORTS)

    # Every banner, not the first one that answered. A radio here runs a
    # general-purpose OpenSSH on 22 and serves NETCONF over libssh on 830, so
    # reading only port 22 threw away the signal and called ten radios
    # nothing at all.
    heard = " ".join(str(v) for v in said.values()).lower()
    embedded = any(mark in heard for mark in EMBEDDED)
    netconf = 830 in opened

    # One line, not a transcript. Which ports answered is what a reader acts
    # on; the banner strings behind it decided the kind and are kept in the
    # JSON for anyone who wants to argue with that decision.
    if opened:
        facts = ["answers on %s"
                 % ", ".join(names.get(p, str(p)) for p in sorted(opened))]
    else:
        facts = ["nothing answered on the ports a managed device usually "
                 "listens on"]

    kind = "radio" if (netconf and embedded) else ""
    return {"kind": kind, "open": sorted(opened), "said": said,
            "facts": facts}


def collect(run: Callable[[str], str], targets: List[str], pid: str = "",
            log: Optional[Callable[[str], None]] = None
            ) -> Dict[str, Dict[str, Any]]:
    """Ask each address what it is, from wherever its address exists.

    `pid` names a process whose network namespace the probe must run in;
    without it the probe runs where the host itself stands.
    """
    import shlex

    say = log or (lambda _m: None)
    wanted = [t for t in dict.fromkeys(targets or []) if t]
    if not wanted:
        return {}

    body = SCRIPT % {"ports": repr(list(PORTS)), "targets": repr(wanted),
                     "wait": WAIT}
    where = "nsenter -t %s -n " % pid if pid else ""
    try:
        text = run("%spython3 -c %s" % (where, shlex.quote(body)))
    except Exception as exc:
        say("  identify: %s: %s" % (type(exc).__name__, exc))
        return {}
    if "OPEN|" not in (text or ""):
        try:
            text = run("%spython -c %s" % (where, shlex.quote(body)))
        except Exception:
            return {}

    found = {ip: describe(rec) for ip, rec in parse(text).items()}
    for ip, rec in sorted(found.items()):
        if rec["facts"]:
            say("  %s: %s%s" % (ip, rec["facts"][0],
                                " - a %s" % rec["kind"] if rec["kind"] else ""))
    return found

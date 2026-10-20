"""Read a radio's own identity over NETCONF, when its M-plane is up.

A fronthaul radio is found by probing (it answers SSH and NETCONF on a pod's
link-local address). That says "a radio is here", not which one. The radio
knows: the standard `ietf-hardware` model carries its serial, model and
revisions, served over NETCONF on 830. This reads that - standard YANG, no
vendor knowledge - and attaches it to the radio on the map.

Best-effort and silent: it runs inside the pod's namespace through the host's
own ssh client (nothing is installed), tries the logins the build was given,
and if the M-plane is down or no login fits, it simply adds nothing. A `<get>`
changes no state on the radio.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import re
import shlex
from typing import Callable, Dict, List, Optional, Tuple

Run = Callable[[str], str]

NETCONF_PORT = 830
SCRIPT_PATH = "/tmp/.linkfinder_nc.py"

# Driven on the host with the stdlib only: a pty is the one way to hand a
# password to the ssh client and then speak NETCONF's `]]>]]>`-framed hello
# and get over the same channel. argv carries pid, address, user, password.
SCRIPT = r'''
import os, pty, select, sys, time
PID, IP, USER, PW = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
HELLO = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><capabilities>'
         '<capability>urn:ietf:params:netconf:base:1.0</capability>'
         '</capabilities></hello>\n]]>]]>\n')
GET = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<rpc message-id="1" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
       '<get><filter type="subtree">'
       '<hardware xmlns="urn:ietf:params:xml:ns:yang:ietf-hardware"/>'
       '</filter></get></rpc>\n]]>]]>\n')
argv = ["nsenter", "-t", PID, "-n", "ssh", "-p", "830", "-s",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=6",
        "%s@%s" % (USER, IP), "netconf"]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)
buf = b""; sent_pw = False; sent_rpc = False; start = time.time()
while time.time() - start < 12:
    r, _, _ = select.select([fd], [], [], 1)
    if fd in r:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        buf += data
        if not sent_pw and b"password:" in buf.lower():
            os.write(fd, (PW + "\n").encode()); sent_pw = True
        elif sent_pw and not sent_rpc and buf.count(b"]]>]]>") >= 1:
            os.write(fd, HELLO.encode()); time.sleep(0.3); os.write(fd, GET.encode())
            sent_rpc = True
        # The pty echoes what we write, so our own hello's ]]>]]> counts as a
        # marker too. Do not break on the count - wait for the reply itself,
        # then drain what is left, or the serial is cut off before it arrives.
        if sent_rpc and (b"</rpc-reply>" in buf or b"</data>" in buf
                         or b"</hardware>" in buf or b"rpc-error" in buf):
            time.sleep(0.4)
            while True:
                r2, _, _ = select.select([fd], [], [], 1)
                if fd not in r2:
                    break
                try:
                    more = os.read(fd, 65536)
                except OSError:
                    break
                if not more:
                    break
                buf += more
            break
sys.stdout.buffer.write(buf)
'''

# The leaves worth lifting out of ietf-hardware, named as the panel shows them.
_WANT = (("serial", "serial-num"), ("model", "model-name"),
         ("made_by", "mfg-name"), ("hw_rev", "hardware-rev"),
         ("sw_rev", "software-rev"), ("fw_rev", "firmware-rev"))


def parse_hardware(xml: str) -> Dict[str, str]:
    """The identity leaves from an ietf-hardware reply, if the get succeeded."""
    if "Permission denied" in xml or "<serial-num" not in xml:
        return {}
    out: Dict[str, str] = {}
    for key, tag in _WANT:
        for value in re.findall(r"<%s>([^<]+)</%s>" % (tag, tag), xml):
            value = value.strip()
            # A boolean leaf of the same local name as model-name turns up in
            # some builds; the real model carries letters, so skip a bare bool.
            if value and value not in ("true", "false"):
                out[key] = value
                break
    return out


def deploy(run: Run) -> bool:
    """Write the NETCONF helper to the host once. Returns whether it landed.

    Pushed as base64 so no quoting of a multi-line python file over SSH can go
    wrong, and because the host carries no NETCONF client of its own.
    """
    blob = base64.b64encode(SCRIPT.encode("utf-8")).decode("ascii")
    cmd = "echo %s | base64 -d > %s && echo OK" % (blob, shlex.quote(SCRIPT_PATH))
    try:
        return "OK" in (run("sh -c %s" % shlex.quote(cmd)) or "")
    except Exception:
        return False


def cleanup(run: Run) -> None:
    try:
        run("rm -f %s" % shlex.quote(SCRIPT_PATH))
    except Exception:
        pass


def read_hardware(run: Run, pid: str, ip: str,
                  creds: List[Tuple[str, str]],
                  log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Try each login against the radio's NETCONF and return its identity.

    `run` executes a command on the host with root (the namespace needs it);
    the deployed script enters the pod's namespace itself. First login that
    gets a reply wins; a refusal moves to the next; anything else (830 shut,
    no route) ends it - there is nothing to read.
    """
    say = log or (lambda _m: None)
    cmd_for = lambda user, password: "python3 %s %s %s %s %s" % (
        SCRIPT_PATH, shlex.quote(str(pid)), shlex.quote(ip),
        shlex.quote(user), shlex.quote(password))
    for user, password in creds or []:
        authed = False
        # A couple of tries per login: the session is over a pty and the reply
        # can arrive a beat after the hello, so a pass that logged in but caught
        # no hardware is worth one repeat before giving up on the login.
        for _attempt in (1, 2):
            try:
                out = run(cmd_for(user, password)) or ""
            except Exception:
                return {}
            if "Permission denied" in out:
                break                      # wrong login, move to the next
            got = parse_hardware(out)
            if got:
                say("    %s: %s%s over NETCONF (as %s)"
                    % (ip, got.get("model", "radio"),
                       " serial %s" % got["serial"] if got.get("serial") else "",
                       user))
                return got
            if "]]>]]>" in out:
                authed = True             # we got in; the reply was just missed
                continue                  # retry this same login once
            # Neither a refusal nor a reply: 830 is shut or no route. If we had
            # already authenticated on a prior login, that login was right and
            # the port simply went quiet; either way there is nothing to read.
            return {} if not authed else {}
    return {}

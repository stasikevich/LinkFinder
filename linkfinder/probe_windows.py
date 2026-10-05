"""Windows: capture LLDP/CDP with pktmon, locally or over OpenSSH.

Windows has no LLDP receiver of its own, so there is nothing to query - the
frames have to be captured. pktmon ships in Windows 10/Server 2019 and later
and can export straight to pcapng, which our decoder already reads.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import os
import re
import tempfile
import time
from typing import Callable, List, Optional, Tuple

from .decode import neighbors_from_capture
from .model import Neighbor, ProbeAttempt, TargetResult
from .proc import run_quiet
from .sshx import Ssh

PKTMON_ETHERTYPES = ("0x88CC",)


def _pktmon_commands(workdir: str, seconds: int) -> List[str]:
    etl = os.path.join(workdir, "linkfinder.etl")
    pcapng = os.path.join(workdir, "linkfinder.pcapng")
    return [
        "pktmon filter remove",
        "pktmon filter add LinkFinderLLDP -d 0x88CC",
        'pktmon start --capture --pkt-size 0 --file "%s" -s 64' % etl,
        "__SLEEP__%d" % seconds,
        "pktmon stop",
        'pktmon etl2pcap "%s" -o "%s"' % (etl, pcapng),
        "pktmon filter remove",
    ]


def _run_local(cmd: str, timeout: int) -> Tuple[int, str]:
    return run_quiet(cmd, timeout=timeout, shell=True)


def probe_windows_local(result: TargetResult, seconds: int = 65,
                        log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)
    result.kind = "windows"
    started = time.time()
    rc, _out = _run_local("pktmon --version", 15)
    if rc != 0:
        result.error = "pktmon is not available (needs Windows 10 / Server 2019 or later)"
        return result
    rc, out = _run_local("net session", 10)
    if rc != 0:
        result.error = "pktmon needs an elevated prompt - run this as Administrator"
        return result

    workdir = tempfile.mkdtemp(prefix="linkfinder-")
    say("  capturing with pktmon for %ds (working dir %s)" % (seconds, workdir))
    say("  note: 'pktmon filter remove' clears any pktmon filters already configured here")
    pcapng = os.path.join(workdir, "linkfinder.pcapng")
    for cmd in _pktmon_commands(workdir, seconds):
        if cmd.startswith("__SLEEP__"):
            time.sleep(int(cmd.split("__SLEEP__")[1]))
            continue
        rc, out = _run_local(cmd, seconds + 60)
        if rc != 0 and "etl2pcap" in cmd:
            result.error = "pktmon etl2pcap failed: %s" % out.strip()[:200]

    found = 0
    if os.path.exists(pcapng):
        with open(pcapng, "rb") as handle:
            for nb in neighbors_from_capture(handle.read()):
                nb.local_iface = nb.local_iface or "(pktmon: all adapters)"
                nb.source = "pktmon"
                nb.note = "pktmon captures all adapters at once; match by switch port description"
                result.add(nb)
                found += 1
    result.attempts.append(ProbeAttempt("pktmon", bool(found), "%d neighbour(s)" % found,
                                        round(time.time() - started, 1)))
    if not found and not result.error:
        result.error = "no LLDP frames seen in %ds - the switch port may have LLDP disabled" % seconds
    return result


def probe_windows_over_ssh(ssh: Ssh, result: TargetResult, seconds: int = 65,
                           log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)
    result.kind = "windows"
    started = time.time()
    rc, out, _err = ssh.run("pktmon --version", timeout=20)
    if rc != 0:
        result.error = ("pktmon not usable over this SSH session (needs Windows 10 / "
                        "Server 2019+ and an elevated OpenSSH session)")
        return result
    remote_dir = "%TEMP%\\linkfinder"
    etl = remote_dir + "\\lf.etl"
    pcapng = remote_dir + "\\lf.pcapng"
    say("  capturing with pktmon for %ds on the remote host" % seconds)
    ssh.run('cmd /c mkdir "%s" 2>nul' % remote_dir, timeout=20)
    ssh.run("cmd /c pktmon filter remove", timeout=30)
    ssh.run("cmd /c pktmon filter add LinkFinderLLDP -d 0x88CC", timeout=30)
    ssh.run('cmd /c pktmon start --capture --pkt-size 0 --file "%s" -s 64' % etl, timeout=60)
    time.sleep(seconds)
    ssh.run("cmd /c pktmon stop", timeout=60)
    ssh.run('cmd /c pktmon etl2pcap "%s" -o "%s"' % (etl, pcapng), timeout=120)
    ssh.run("cmd /c pktmon filter remove", timeout=30)
    rc, b64, _err = ssh.run('powershell -NoProfile -Command '
                            '"[Convert]::ToBase64String([IO.File]::ReadAllBytes(\'%s\'))"' % pcapng,
                            timeout=120)
    found = 0
    if rc == 0 and b64.strip():
        try:
            blob = base64.b64decode("".join(b64.split()))
        except Exception:
            blob = b""
        for nb in neighbors_from_capture(blob):
            nb.local_iface = nb.local_iface or "(pktmon: all adapters)"
            nb.source = "pktmon"
            result.add(nb)
            found += 1
    ssh.run('cmd /c del /q "%s" "%s"' % (etl, pcapng), timeout=30)
    result.attempts.append(ProbeAttempt("pktmon-ssh", bool(found), "%d neighbour(s)" % found,
                                        round(time.time() - started, 1)))
    if not found and not result.error:
        result.error = "no LLDP frames captured on the Windows host in %ds" % seconds
    return result

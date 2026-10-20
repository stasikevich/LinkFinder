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

from . import cancel as _cancel
from . import handsets, hardware, serialports
from .decode import neighbors_from_capture
from .model import ProbeAttempt, TargetResult
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
                        log: Optional[Callable[[str], None]] = None,
                        read_serial: bool = False,
                        serial_baud: int = 115200, cancel=None) -> TargetResult:
    say = log or (lambda _m: None)
    result.kind = "windows"
    started = time.time()
    name, macs = _windows_identity(lambda cmd: _run_local(cmd, 30)[1])
    if name and not result.device_name:
        result.device_name = name
    if macs:
        result.extra.setdefault("macs", {}).update(macs)
    # Before the pktmon gate: reading which COM ports exist needs no
    # Administrator and no capture, so an un-elevated run still answers
    # "which port is the radio on".
    # The chassis tells a bench NUC/desktop (where a radio console cable is)
    # from a rack server (where it is not), so the server's ports are not opened.
    hardware.attach(result, hardware.read_windows(lambda cmd: _run_local(cmd, 30)[1]), say)
    found_serial = serialports.read_windows(lambda cmd: _run_local(cmd, 30)[1])
    if read_serial and found_serial:
        from .baked import radio_creds
        say("  asking the radio console port(s) who they are at %d baud" % serial_baud)
        serialports.capture(lambda cmd: _run_local(cmd, 40)[1], found_serial,
                            platform="windows", baud=serial_baud, seconds=4,
                            creds=radio_creds(), say=say,
                            hardware=result.extra.get("hardware"), cancel=cancel)
    serialports.attach(result, found_serial, say)
    if _cancel.cancelled(cancel):
        result.error = "stopped"
        return result
    rc, _out = _run_local("where pktmon", 15)
    if rc != 0:
        result.error = ("pktmon is not on this machine (it ships with Windows 10 / "
                        "Server 2019 and later)")
        return result
    rc, _out = _run_local("net session", 10)
    if rc != 0:
        result.error = ("pktmon is installed but its driver refuses a normal user - "
                        "run LinkFinder from an Administrator prompt")
        return result

    workdir = tempfile.mkdtemp(prefix="linkfinder-")
    say("  capturing with pktmon for %ds (working dir %s)" % (seconds, workdir))
    say("  note: 'pktmon filter remove' clears any pktmon filters already configured here")
    pcapng = os.path.join(workdir, "linkfinder.pcapng")
    for cmd in _pktmon_commands(workdir, seconds):
        if cmd.startswith("__SLEEP__"):
            # Stop cuts the capture short: leave pktmon clean and bail now.
            if _cancel.sleep(int(cmd.split("__SLEEP__")[1]), cancel):
                _run_local("pktmon stop", 30)
                _run_local("pktmon filter remove", 30)
                result.error = "stopped"
                return result
            continue
        rc, out = _run_local(cmd, seconds + 60)
        if rc != 0 and "etl2pcap" in cmd:
            result.error = "pktmon etl2pcap failed: %s" % out.strip()[:200]

    found = 0
    if os.path.exists(pcapng):
        with open(pcapng, "rb") as handle:
            # The local port is left empty on purpose. pktmon listens to every
            # adapter at once, so which one heard a frame is not known - and
            # "(pktmon: all adapters)" was being drawn on the map as though it
            # were the name of a port. Not knowing is said by saying nothing;
            # the note below explains it where there is room to read it.
            for nb in neighbors_from_capture(handle.read()):
                nb.source = "pktmon"
                nb.note = "pktmon captures all adapters at once; match by switch port description"
                result.add(nb)
                found += 1
    result.attempts.append(ProbeAttempt("pktmon", bool(found), "%d neighbour(s)" % found,
                                        round(time.time() - started, 1)))
    if not found and not result.error:
        result.error = "no LLDP frames seen in %ds - the switch port may have LLDP disabled" % seconds
    return result


def _win_mac(value: str) -> str:
    hexes = re.findall(r"[0-9a-fA-F]{2}", str(value or ""))
    return ":".join(h.lower() for h in hexes) if len(hexes) == 6 else ""


def _windows_identity(run: Callable[[str], str]):
    """The host's own name and NIC MACs - so it is a named node, and the switch
    that reports it by MAC merges onto it instead of standing up a second box."""
    name = ""
    try:
        name = (run("hostname") or "").strip().splitlines()[0].strip()
    except Exception:
        pass
    macs = {}
    ps = ('powershell -NoProfile -Command "$ErrorActionPreference=\'SilentlyContinue\';'
          ' Get-NetAdapter | Where-Object Status -eq Up |'
          ' ForEach-Object { $_.Name + \'=\' + $_.MacAddress }"')
    try:
        for line in (run(ps) or "").splitlines():
            if "=" in line:
                iface, _, mac = line.partition("=")
                mac = _win_mac(mac)
                if mac:
                    macs[iface.strip()] = mac
    except Exception:
        pass
    return name, macs


def probe_windows_over_ssh(ssh: Ssh, result: TargetResult, seconds: int = 65,
                           log: Optional[Callable[[str], None]] = None,
                           adb: bool = False, adb_path: str = "adb",
                           read_serial: bool = False,
                           serial_baud: int = 115200, cancel=None) -> TargetResult:
    say = log or (lambda _m: None)
    result.kind = "windows"
    started = time.time()
    name, macs = _windows_identity(lambda cmd: ssh.run(cmd, timeout=30)[1])
    if name and not result.device_name:
        result.device_name = name
    if macs:
        result.extra.setdefault("macs", {}).update(macs)
    # Before the pktmon gate on purpose: a NUC whose session may not drive
    # pktmon can still say which handsets are plugged into it.
    hardware.attach(result, hardware.read_windows(
        lambda cmd: ssh.run(cmd, timeout=60)[1]), say)

    attached = handsets.collect(lambda cmd: ssh.run(cmd, timeout=90)[1], say,
                                windows=True, adb=adb, adb_path=adb_path)
    if attached:
        result.extra["usb"] = attached

    found_serial = serialports.read_windows(lambda cmd: ssh.run(cmd, timeout=60)[1])
    if read_serial and found_serial:
        from .baked import radio_creds
        say("  asking the radio console port(s) who they are at %d baud" % serial_baud)
        serialports.capture(lambda cmd: ssh.run(cmd, timeout=40)[1], found_serial,
                            platform="windows", baud=serial_baud, seconds=4,
                            creds=radio_creds(), say=say,
                            hardware=result.extra.get("hardware"), cancel=cancel)
    serialports.attach(result, found_serial, say)
    if _cancel.cancelled(cancel):
        result.error = "stopped"
        return result
    # pktmon has no --version and never had one: asking for it fails on every
    # Windows there is, so this check could only ever refuse. The local path
    # was fixed long ago; its twin over SSH was not. Ask whether the binary
    # exists, then whether this session may drive it, and say which failed.
    rc, _out, _err = ssh.run("where pktmon", timeout=20)
    if rc != 0:
        result.error = ("pktmon is not on that machine (it ships with Windows 10 / "
                        "Server 2019 and later)")
        return result
    rc, _out, _err = ssh.run(
        "net session >nul 2>&1 && echo ELEVATED || echo NOT-ELEVATED", timeout=20)
    if rc != 0 or "NOT-ELEVATED" in (_out or ""):
        result.error = ("pktmon is installed there but this SSH session is not "
                        "elevated - pktmon needs Administrator")
        return result
    remote_dir = "%TEMP%\\linkfinder"
    etl = remote_dir + "\\lf.etl"
    pcapng = remote_dir + "\\lf.pcapng"
    say("  capturing with pktmon for %ds on the remote host" % seconds)
    ssh.run('cmd /c mkdir "%s" 2>nul' % remote_dir, timeout=20)
    ssh.run("cmd /c pktmon filter remove", timeout=30)
    ssh.run("cmd /c pktmon filter add LinkFinderLLDP -d 0x88CC", timeout=30)
    ssh.run('cmd /c pktmon start --capture --pkt-size 0 --file "%s" -s 64' % etl, timeout=60)
    if _cancel.sleep(seconds, cancel):
        # Stop: the SSH is being closed under us, so just bail - no clean-up
        # command would get through anyway.
        result.error = "stopped"
        return result
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
        # As above: pktmon cannot say which adapter heard the frame, so the
        # local port stays empty rather than carrying a stand-in.
        for nb in neighbors_from_capture(blob):
            nb.source = "pktmon"
            nb.note = ("pktmon captures all adapters at once; "
                       "match by switch port description")
            result.add(nb)
            found += 1
    ssh.run('cmd /c del /q "%s" "%s"' % (etl, pcapng), timeout=30)
    result.attempts.append(ProbeAttempt("pktmon-ssh", bool(found), "%d neighbour(s)" % found,
                                        round(time.time() - started, 1)))
    if not found and not result.error:
        result.error = "no LLDP frames captured on the Windows host in %ds" % seconds
    return result

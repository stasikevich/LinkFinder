"""Thin paramiko wrapper: connect, run, sudo, and enumerate interfaces.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:  # keep the rest of the tool usable without it
    paramiko = None


class SshError(Exception):
    pass


LEGACY_KEX = ["diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1",
              "diffie-hellman-group-exchange-sha1"]
LEGACY_KEYS = ["ssh-rsa", "ssh-dss"]
LEGACY_CIPHERS = ["aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc"]


@dataclass
class Iface:
    name: str
    mac: str = ""
    operstate: str = ""
    speed: str = ""
    physical: bool = False
    master: str = ""
    carrier: str = ""
    mtu: str = ""
    lowers: List[str] = field(default_factory=list)
    addrs: List[str] = field(default_factory=list)

    @property
    def virtual_prefix(self) -> bool:
        return self.name.startswith(
            ("veth", "docker", "br-", "virbr", "cni", "flannel", "tun", "tap", "dummy",
             "kube-", "vxlan", "lxc", "ovs-", "nerdctl", "cali", "weave", "antrea"))


@dataclass
class Creds:
    user: str = ""
    password: str = ""
    key_file: str = ""
    port: int = 22
    sudo_password: str = ""

    def sudo_pw(self) -> str:
        return self.sudo_password or self.password


class Ssh:
    def __init__(self, host: str, creds: Creds, timeout: int = 15):
        if paramiko is None:
            raise SshError("paramiko is not installed (pip install paramiko)")
        self.host = host
        self.creds = creds
        self.timeout = timeout
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._is_root: Optional[bool] = None
        self._have: Dict[str, bool] = {}

    def __enter__(self) -> "Ssh":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        """Network gear allows very few auth attempts and then drops the session, so
        when a password is given do not let paramiko burn them on agent keys first."""
        have_password = bool(self.creds.password)
        have_key = bool(self.creds.key_file)
        kwargs = dict(username=self.creds.user or None, port=self.creds.port,
                      timeout=self.timeout, banner_timeout=self.timeout,
                      auth_timeout=self.timeout,
                      allow_agent=not have_password,
                      look_for_keys=not (have_password or have_key))
        if have_key:
            kwargs["key_filename"] = os.path.expanduser(self.creds.key_file)
        if have_password:
            kwargs["password"] = self.creds.password
        no_creds = not (self.creds.password or self.creds.key_file or self.creds.user)
        try:
            self.client.connect(self.host, **kwargs)
        except paramiko.AuthenticationException as exc:
            raise SshError("SSH rejected the credentials (%s)" % exc)
        except (paramiko.SSHException, socket.error, OSError) as exc:
            detail = str(exc)
            if "no acceptable" in detail.lower() or "incompatible ssh peer" in detail.lower():
                try:
                    self._connect_legacy()
                    return
                except Exception as legacy_exc:
                    detail = "%s (legacy algorithms also failed: %s)" % (detail, legacy_exc)
            elif "No existing session" in detail or "Error reading SSH protocol banner" in detail:
                detail = ("the device dropped the session before login finished - old "
                          "switches allow only a couple of auth attempts")
            if no_creds:
                detail += "; no SSH username or password was given"
            raise SshError("SSH did not get us in: %s" % detail)

    def _connect_legacy(self) -> None:
        """Old switches only offer SHA-1 key exchange and CBC ciphers, which modern
        paramiko no longer proposes. Re-add them and authenticate by hand."""
        sock = socket.create_connection((self.host, self.creds.port), timeout=self.timeout)
        transport = paramiko.Transport(sock)
        for attribute, extra in (("_preferred_kex", LEGACY_KEX),
                                 ("_preferred_keys", LEGACY_KEYS),
                                 ("_preferred_ciphers", LEGACY_CIPHERS)):
            current = list(getattr(transport, attribute))
            setattr(transport, attribute,
                    current + [a for a in extra if a not in current])
        transport.start_client(timeout=self.timeout)
        user = self.creds.user or os.environ.get("USERNAME") or "admin"
        if self.creds.key_file:
            key = paramiko.RSAKey.from_private_key_file(os.path.expanduser(self.creds.key_file))
            transport.auth_publickey(user, key)
        else:
            transport.auth_password(user, self.creds.password)
        self.client._transport = transport

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def run(self, cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
        try:
            _sin, sout, serr = self.client.exec_command(cmd, timeout=timeout)
            out = sout.read().decode("utf-8", "replace")
            err = serr.read().decode("utf-8", "replace")
            return sout.channel.recv_exit_status(), out, err
        except (paramiko.SSHException, socket.timeout, socket.error) as exc:
            return 255, "", str(exc)

    def run_bytes(self, cmd: str, timeout: int = 120) -> Tuple[int, bytes, str]:
        try:
            _sin, sout, serr = self.client.exec_command(cmd, timeout=timeout)
            out = sout.read()
            err = serr.read().decode("utf-8", "replace")
            return sout.channel.recv_exit_status(), out, err
        except (paramiko.SSHException, socket.timeout, socket.error) as exc:
            return 255, b"", str(exc)

    @property
    def is_root(self) -> bool:
        if self._is_root is None:
            _rc, out, _err = self.run("id -u")
            self._is_root = out.strip() == "0"
        return self._is_root

    def sudo(self, cmd: str, timeout: int = 120) -> Tuple[int, str, str]:
        """Run with privileges. No pty is used, so stdout stays byte-clean."""
        if self.is_root:
            return self.run(cmd, timeout=timeout)
        wrapped = "sudo -S -p '' sh -c %s" % shlex.quote(cmd)
        try:
            sin, sout, serr = self.client.exec_command(wrapped, timeout=timeout)
            pw = self.creds.sudo_pw()
            if pw:
                sin.write(pw + "\n")
                sin.flush()
            out = sout.read().decode("utf-8", "replace")
            err = serr.read().decode("utf-8", "replace")
            return sout.channel.recv_exit_status(), out, err
        except (paramiko.SSHException, socket.timeout, socket.error) as exc:
            return 255, "", str(exc)

    def have(self, tool: str) -> bool:
        if tool not in self._have:
            rc, out, _err = self.run("command -v %s 2>/dev/null || which %s 2>/dev/null" % (tool, tool))
            self._have[tool] = rc == 0 and bool(out.strip())
        return self._have[tool]

    def platform(self) -> str:
        _rc, uname, _e = self.run("uname -s 2>/dev/null; cat /etc/os-release 2>/dev/null")
        low = uname.lower()
        if "vmkernel" in low:
            return "esxi"
        if "nutanix" in low:
            return "linux"
        if "suse" in low or "sles" in low:
            return "suse"
        if "linux" in low or "ubuntu" in low or "debian" in low or "centos" in low or "rhel" in low:
            return "linux"
        if "darwin" in low:
            return "darwin"
        _rc, win, _e = self.run("ver 2>&1")
        if "windows" in win.lower():
            return "windows"
        return "unknown"


IFACE_SCRIPT = r"""
for d in /sys/class/net/*; do
  n=${d##*/}
  [ "$n" = "lo" ] && continue
  mac=$(cat "$d/address" 2>/dev/null)
  st=$(cat "$d/operstate" 2>/dev/null)
  sp=$(cat "$d/speed" 2>/dev/null)
  car=$(cat "$d/carrier" 2>/dev/null)
  mtu=$(cat "$d/mtu" 2>/dev/null)
  phy=0; [ -e "$d/device" ] && phy=1
  mas=""; if [ -L "$d/master" ]; then m=$(readlink "$d/master"); mas=${m##*/}; fi
  low=""
  for l in "$d"/lower_*; do
    [ -e "$l" ] || continue
    b=${l##*/lower_}; low="$low,$b"
  done
  echo "IF|$n|$mac|$st|$sp|$phy|$mas|$car|${low#,}|$mtu"
done
ip -o addr show 2>/dev/null | awk '{print "ADDR|" $2 "|" $4}'
"""


def parse_ifaces(text: str) -> Dict[str, Iface]:
    out: Dict[str, Iface] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if parts[0] == "IF" and len(parts) >= 9:
            name = parts[1]
            out[name] = Iface(
                name=name, mac=parts[2], operstate=parts[3],
                speed=(parts[4] + "Mb/s" if parts[4].isdigit() and parts[4] != "-1" else ""),
                physical=parts[5] == "1", master=parts[6], carrier=parts[7],
                lowers=[x for x in parts[8].split(",") if x],
                mtu=parts[9] if len(parts) > 9 else "")
        elif parts[0] == "ADDR" and len(parts) >= 3:
            name = parts[1].split("@")[0]
            if name in out:
                out[name].addrs.append(parts[2])
    return out


def iface_for_ip(ifaces: Dict[str, Iface], ip: str) -> str:
    for name, itf in ifaces.items():
        for addr in itf.addrs:
            if addr.split("/")[0] == ip:
                return name
    return ""


def physical_leaves(ifaces: Dict[str, Iface], name: str, _depth: int = 0) -> List[str]:
    """Resolve bond0.3674 -> bond0 -> [enp1s0f0, enp1s0f1] using /sys lower_* links."""
    itf = ifaces.get(name)
    if itf is None or _depth > 6:
        return []
    if itf.physical and not itf.lowers:
        return [name]
    leaves: List[str] = []
    for low in itf.lowers:
        leaves.extend(physical_leaves(ifaces, low, _depth + 1))
    slaves = [i.name for i in ifaces.values() if i.master == name and i.name != name]
    for slave in slaves:
        leaves.extend(physical_leaves(ifaces, slave, _depth + 1))
    if not leaves and itf.physical:
        leaves = [name]
    seen, uniq = set(), []
    for leaf in leaves:
        if leaf not in seen:
            seen.add(leaf)
            uniq.append(leaf)
    return uniq


def push_and_run_python(ssh: Ssh, source: str, args: str, timeout: int,
                        python: str = "python3", sudo: bool = True) -> Tuple[int, str, str]:
    """Ship a script over stdin-free, quoting-safe base64 and run it remotely."""
    blob = base64.b64encode(source.encode("utf-8")).decode("ascii")
    cmd = "echo %s | base64 -d | %s - %s" % (blob, python, args)
    return (ssh.sudo if sudo else ssh.run)(cmd, timeout=timeout)


MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", re.I)


def normalise_mac(value: str) -> str:
    value = value.strip().lower().replace("-", ":").replace(".", "")
    if MAC_RE.fullmatch(value):
        return value
    hexonly = re.sub(r"[^0-9a-f]", "", value)
    if len(hexonly) == 12:
        return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
    return value

"""The address the BMC answers on, read from the host it sits in.

A server's management controller has its own address, and nobody can see it
from the outside until they already know it. The host can: `ipmitool lan
print` asks the BMC directly over the internal channel, with no network in
the way and no password to hold.

That address is worth having for three reasons. It is how you reach a
machine that will not boot; it is a thing NetBox records and often records
wrongly; and a BMC configured with an address that answers to nobody is a
quiet fault - the day you need it is the day you find out.

Nothing here is written anywhere. Where NetBox disagrees, that is reported
and a person decides.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

# When ipmitool is not installed - a bare appliance, SUSE SL-Micro, a hardened
# host - the address is still reachable: the kernel's own IPMI device answers
# the same "Get LAN Configuration" query ipmitool would send. This asks it
# directly over /dev/ipmi0, installing nothing, the same way the packet
# sniffer is pushed and run. Read-only: it reads the LAN config, nothing more.
# Proven against ipmitool on a host that had both, byte for byte.
IPMI_LAN_SRC = r'''
import ctypes, fcntl, os, select, struct

MAGIC = ord('i')
def _ioc(d, nr, size): return (d << 30) | (MAGIC << 8) | nr | (size << 16)

class SIAddr(ctypes.Structure):
    _fields_ = [("addr_type", ctypes.c_int), ("channel", ctypes.c_short),
                ("lun", ctypes.c_ubyte)]
class Msg(ctypes.Structure):
    _fields_ = [("netfn", ctypes.c_ubyte), ("cmd", ctypes.c_ubyte),
                ("data_len", ctypes.c_ushort), ("data", ctypes.c_void_p)]
class Req(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_void_p), ("addr_len", ctypes.c_uint),
                ("msgid", ctypes.c_long), ("msg", Msg)]
class Recv(ctypes.Structure):
    _fields_ = [("recv_type", ctypes.c_int), ("addr", ctypes.c_void_p),
                ("addr_len", ctypes.c_uint), ("msgid", ctypes.c_long),
                ("msg", Msg)]

SEND = _ioc(2, 13, ctypes.sizeof(Req))        # _IOR(i, 13, ipmi_req)
RECV = _ioc(3, 11, ctypes.sizeof(Recv))       # _IOWR(i, 11, ipmi_recv)
SETADDR = _ioc(2, 17, ctypes.sizeof(ctypes.c_uint))

def param(fd, channel, which):
    addr = SIAddr(0x0c, 0xf, 0)               # system interface, BMC channel
    data = (ctypes.c_ubyte * 4)(channel, which, 0, 0)
    msg = Msg(0x0c, 0x02, 4, ctypes.cast(data, ctypes.c_void_p))
    req = Req(ctypes.cast(ctypes.byref(addr), ctypes.c_void_p),
              ctypes.sizeof(addr), which, msg)
    fcntl.ioctl(fd, SEND, req)
    if not select.select([fd], [], [], 1.5)[0]:
        return None
    raddr = SIAddr(); rbuf = (ctypes.c_ubyte * 64)()
    rmsg = Msg(0, 0, 64, ctypes.cast(rbuf, ctypes.c_void_p))
    recv = Recv(0, ctypes.cast(ctypes.byref(raddr), ctypes.c_void_p),
                ctypes.sizeof(raddr), 0, rmsg)
    fcntl.ioctl(fd, RECV, recv)
    out = bytes(rbuf[:recv.msg.data_len])
    if len(out) < 2 or out[0] != 0:           # completion code must be 0
        return None
    return out[2:]                            # drop cc + parameter revision

def main():
    try:
        fd = os.open("/dev/ipmi0", os.O_RDWR)
    except OSError:
        print("LF_IPMI nodevice"); return
    try:
        fcntl.ioctl(fd, SETADDR, struct.pack("I", 0x20))
    except OSError:
        pass
    for ch in (1, 8, 3, 0, 2):
        ip = param(fd, ch, 3)
        if ip and len(ip) == 4 and any(ip):
            src = param(fd, ch, 4); mac = param(fd, ch, 5)
            print("LF_IPMI address=%d.%d.%d.%d" % tuple(ip))
            if src:
                print("LF_IPMI source=%s" % ("dhcp" if src[0] == 2 else "static"))
            if mac and len(mac) >= 6:
                print("LF_IPMI mac=%02x:%02x:%02x:%02x:%02x:%02x" % tuple(mac[:6]))
            os.close(fd); return
    os.close(fd); print("LF_IPMI none")

main()
'''


def parse_lan(text: str) -> Dict[str, Any]:
    """The address, source and MAC the kernel query printed, if any."""
    out: Dict[str, Any] = {}
    for line in str(text or "").splitlines():
        if not line.startswith("LF_IPMI "):
            continue
        rest = line[len("LF_IPMI "):].strip()
        if "=" not in rest:
            continue
        key, value = rest.split("=", 1)
        out[key.strip()] = value.strip()
    if out.get("address"):
        out["source"] = out.get("source") or "source not reported"
        out["read_by"] = "the kernel's IPMI device, without ipmitool"
    return out

# Channels are not standardised: 1 is usual, Supermicro answers on the
# default, some boards only on 8. Asking in turn costs one command each and
# stops at the first that names an address.
SCRIPT = r'''
command -v ipmitool >/dev/null 2>&1 || { echo "IPMI|absent"; exit 0; }
for ch in "" 1 8 2 3; do
  out=$(ipmitool lan print $ch 2>/dev/null)
  echo "$out" | grep -q "^IP Address  " || continue
  echo "IPMI|channel|${ch:-default}"
  echo "$out" | sed "s/^/IPMI|lan|/"
  addr=$(echo "$out" | sed -n 's/^IP Address  *: *//p' | head -1)
  if [ -n "$addr" ]; then
    if ping -c 1 -W 1 "$addr" >/dev/null 2>&1; then
      echo "IPMI|answers|yes"
    else
      echo "IPMI|answers|no"
    fi
  fi
  exit 0
done
echo "IPMI|silent"
'''

WANTED = {
    "ip address source": "source",
    "ip address": "address",
    "subnet mask": "netmask",
    "mac address": "mac",
    "default gateway ip": "gateway",
}

NOT_SET = ("0.0.0.0", "", "::")


def parse(text: str) -> Dict[str, Any]:
    """`ipmitool lan print` as the few facts worth keeping.

    "IP Address Source" starts with "IP Address", so the key is compared
    whole - matching on a prefix put the words "Static Address" in the
    field meant for the address itself.
    """
    out: Dict[str, Any] = {}
    for line in (text or "").splitlines():
        if line.startswith("IPMI|channel|"):
            out["channel"] = line.split("|", 2)[2].strip()
            continue
        if line.startswith("IPMI|answers|"):
            out["answers"] = line.rsplit("|", 1)[1].strip() == "yes"
            continue
        if line.startswith("IPMI|lan|"):
            line = line[len("IPMI|lan|"):]
        if ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        name = WANTED.get(key.strip().lower())
        if name and value.strip():
            out.setdefault(name, value.strip())
    if str(out.get("address", "")) in NOT_SET:
        out.pop("address", None)
    source = str(out.get("source", "")).lower()
    if source:
        out["source"] = "DHCP" if "dhcp" in source else "static"
    return out


def collect(run: Callable[[str], str],
            say: Optional[Callable[[str], None]] = None,
            ssh: Any = None) -> Dict[str, Any]:
    """Ask the host about its own BMC. Never fatal: a probe that cannot run
    says what it could not do and the scan carries on.

    ipmitool first where it exists; where it does not, the kernel's own IPMI
    device is asked directly, so a host with no ipmitool still gives up the
    address rather than nothing. `ssh` is needed for that second path, which
    pushes a small reader and runs it as root.
    """
    tell = say or (lambda _m: None)
    try:
        text = run("sh -c " + _quote(SCRIPT))
    except Exception as problem:
        tell("  ipmi: could not ask (%s)" % problem)
        return {}
    absent = "IPMI|absent" in (text or "")
    found = {} if absent else parse(text)
    if not found.get("address") and ssh is not None:
        kernel = _read_over_device(ssh, tell)
        if kernel.get("address"):
            return kernel
    if absent:
        return {"tool": "absent"}
    if not found.get("address"):
        tell("  ipmi: no address configured on any channel")
        return found or {"tool": "silent"}
    # Say who did the asking. A controller on a management network this host
    # is not on cannot be pinged from here and is perfectly alive; the map
    # settles that afterwards, and this line must not pre-judge it.
    tell("  ipmi: BMC on %s (%s), %s"
         % (found["address"], found.get("source", "source not reported"),
            "this host can reach it" if found.get("answers")
            else "this host cannot reach it"))
    return found


def _quote(text: str) -> str:
    """Wrap a script for `sh -c`, the way the rest of the probes do."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _read_over_device(ssh: Any, tell: Callable[[str], None]) -> Dict[str, Any]:
    """Read the BMC address off /dev/ipmi0 when ipmitool is not installed.

    Needs root to open the device and python3 on the host - both the common
    case. Any failure returns nothing, because this is a bonus path: the
    scan has already run and losing the BMC address is not losing the map.
    """
    from .sshx import push_and_run_python

    try:
        # No device, no BMC - a NUC or a desktop has none, and most Linux
        # hosts are not servers. Checking first means those never get a
        # pointless script pushed to them.
        rc, out, _err = ssh.run("test -c /dev/ipmi0 && echo yes || echo no")
        if "yes" not in out:
            return {}
        if not ssh.have("python3"):
            return {}
        # A BMC with a LAN config answers each channel read in milliseconds;
        # when the SEND blocks (no config, or a wedged BMC) the whole script is
        # capped here rather than sitting for the better part of a minute.
        rc, out, _err = push_and_run_python(ssh, IPMI_LAN_SRC, "", timeout=12)
    except Exception as problem:
        tell("  ipmi: kernel device not read (%s)" % problem)
        return {}
    found = parse_lan(out)
    if found.get("address"):
        tell("  ipmi: BMC on %s (%s), read from the kernel - no ipmitool here"
             % (found["address"], found.get("source", "source not reported")))
    return found


def describe(found: Dict[str, Any]) -> str:
    """One line a person can read, or nothing worth printing."""
    if not found or found.get("tool") == "absent":
        return ""
    address = found.get("address")
    if not address:
        return "a BMC is present but no address is configured on it"
    bits = [str(address)]
    if found.get("source"):
        bits.append(str(found["source"]))
    if found.get("seen_from"):
        bits.append("alive, seen by %s" % found["seen_from"])
    elif "answers" in found:
        bits.append("its host can reach it" if found["answers"]
                    else "its host cannot reach it")
    return ", ".join(bits)


def confirm_from_map(topology, log: Optional[Callable[[str], None]] = None
                     ) -> int:
    """A controller its own host cannot ping may still be perfectly alive.

    Measured on a live server: the host could not reach its own BMC, while
    another machine already on the map held that address in its neighbour
    cache. The evidence was there and nothing joined it up, so the report
    said "cannot reach" about a device that was answering all along.
    """
    say = log or (lambda _m: None)
    seen: Dict[str, str] = {}
    for node in topology.nodes.values():
        who = node.name or (node.mgmt_ips or [""])[0] or node.id
        for address in ((node.extra or {}).get("neighbours") or {}):
            seen.setdefault(str(address), who)

    joined = 0
    for node in topology.nodes.values():
        bmc = (node.extra or {}).get("ipmi") or {}
        address = str(bmc.get("address") or "")
        if not address or bmc.get("answers"):
            continue
        witness = seen.get(address)
        if witness and witness != (node.name or ""):
            bmc["seen_from"] = witness
            joined += 1
    if joined:
        say("  ipmi: %d controller(s) their own host cannot reach are alive "
            "elsewhere on this map" % joined)
    return joined

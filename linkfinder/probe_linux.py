"""Linux / SUSE-toolbox discovery ladder, cheapest method first.

Order: lldpctl -> networkctl -> lldptool -> tcpdump(pcap) -> tshark -> raw-socket
sniffer pushed over SSH. The last rung needs nothing on the target but python3,
which is what makes nodes with no tcpdump (SUSE without toolbox, appliances)
answerable at all.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import shlex
import time
from typing import Callable, Dict, List, Optional, Tuple

from .decode import decode_frame, iter_capture, neighbors_from_capture
from .model import Neighbor, ProbeAttempt, TargetResult
from .sshx import (Creds, Iface, Ssh, SshError, IFACE_SCRIPT, iface_for_ip,
                   parse_ifaces, physical_leaves, push_and_run_python)

GROUP_MAC_FILTER = (
    "(ether proto 0x88cc)"
    " or (ether dst 01:80:c2:00:00:0e)"
    " or (ether dst 01:80:c2:00:00:03)"
    " or (ether dst 01:80:c2:00:00:00 and ether[12:2] > 1500)"
    " or (ether dst 01:00:0c:cc:cc:cc and ether[20:2] = 0x2000)")
"""Must match LLDP and CDP and nothing else: capture stops at the first hit, and
01:80:c2:00:00:00 is shared with STP, whose BPDUs arrive every 2s and would win
every time. The length/protocol tests exclude BPDUs and the other Cisco SNAP
protocols (VTP, DTP, PAgP) that share the CDP group address."""

SNIFFER_SRC = r'''
import base64, select, socket, sys, time

LLDP = 0x88cc
VLAN = (0x8100, 0x88a8, 0x9100)
SNAP = b"\xaa\xaa\x03\x00\x00\x0c"

def interesting(f):
    if len(f) < 14:
        return False
    off = 12
    et = (f[off] << 8) | f[off + 1]
    while et in VLAN and len(f) >= off + 8:
        off += 4
        et = (f[off] << 8) | f[off + 1]
    if et == LLDP:
        return True
    if et <= 1500:
        return f[off + 2:off + 8] == SNAP
    return False

names = [x for x in sys.argv[1].split(",") if x]
deadline = time.time() + float(sys.argv[2])
socks, done = [], set()
for name in names:
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        s.bind((name, 0))
        s.setblocking(False)
        socks.append((name, s))
    except Exception as exc:
        sys.stderr.write("ERR %s %s\n" % (name, exc))
if not socks:
    sys.exit(3)
by_fd = dict((s.fileno(), (n, s)) for n, s in socks)
while time.time() < deadline and len(done) < len(socks):
    ready, _, _ = select.select([s for _, s in socks], [], [], 1.0)
    for s in ready:
        name, sock = by_fd[s.fileno()]
        try:
            data = sock.recv(65535)
        except Exception:
            continue
        if interesting(data):
            sys.stdout.write("FRAME %s %s\n" % (name, base64.b64encode(data).decode()))
            sys.stdout.flush()
            done.add(name)
'''


def _capture_script(ifaces: List[str], seconds: int, tool: str) -> str:
    """One tcpdump/tshark per interface in parallel, results returned base64.

    The script always waits the whole window, so taking several frames instead of
    one costs nothing and gives both protocols a chance to show up.
    """
    lines = ["d=$(mktemp -d /tmp/lf.XXXXXX)"]
    for name in ifaces:
        safe = name.replace("/", "_")
        if tool == "tcpdump":
            cap = ("tcpdump -i %s -s 0 -c 5 -p -U -w $d/%s.pcap %s >/dev/null 2>&1 &"
                   % (shlex.quote(name), safe, shlex.quote(GROUP_MAC_FILTER)))
        else:
            cap = ("tshark -i %s -c 5 -w $d/%s.pcap -f %s >/dev/null 2>&1 &"
                   % (shlex.quote(name), safe, shlex.quote(GROUP_MAC_FILTER)))
        lines.append(cap)
    lines.append("sleep %d" % seconds)
    lines.append("for p in $d/*.pcap; do [ -s \"$p\" ] || continue; "
                 "n=${p##*/}; echo \"PCAP ${n%.pcap}\"; base64 \"$p\" | tr -d '\\n'; echo; done")
    lines.append("kill $(jobs -p) 2>/dev/null; rm -rf $d")
    return "\n".join(lines)


def _parse_capture_output(text: str, name_map: Dict[str, str], source: str,
                          say: Optional[Callable[[str], None]] = None) -> List[Neighbor]:
    out: List[Neighbor] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("PCAP "):
            continue
        safe = line[5:].strip()
        name = name_map.get(safe, safe)
        if i + 1 >= len(lines):
            continue
        try:
            blob = base64.b64decode(lines[i + 1].strip())
        except Exception:
            continue
        frames = sum(1 for _ in iter_capture(blob))
        found = neighbors_from_capture(blob)
        if say and frames and not found:
            say("    %s: captured %d frame(s) but none were LLDP or CDP" % (name, frames))
        for nb in found:
            nb.local_iface = name
            nb.source = source
            out.append(nb)
    return out


def prefer_lldp(neighbors: List[Neighbor]) -> List[Neighbor]:
    """A device speaking both often names its physical port only in LLDP; CDP may
    answer with a logical interface. Keep LLDP, but record what CDP said."""
    by_iface: Dict[str, List[Neighbor]] = {}
    for nb in neighbors:
        by_iface.setdefault(nb.local_iface, []).append(nb)
    out: List[Neighbor] = []
    for rows in by_iface.values():
        lldp = [r for r in rows if r.proto == "LLDP" and r.resolved]
        cdp = [r for r in rows if r.proto == "CDP" and r.resolved]
        if not (lldp and cdp):
            out.extend(rows)
            continue
        keep = lldp[0]
        ports = [c.best_port for c in cdp if c.best_port and c.best_port != keep.best_port]
        if ports:
            extra = "CDP on the same link reports " + ", ".join(dict.fromkeys(ports))
            keep.note = "%s; %s" % (keep.note, extra) if keep.note else extra
        for other in cdp:
            keep.vlan = keep.vlan or other.vlan
            if "." in other.mgmt_ip and (not keep.mgmt_ip or ":" in keep.mgmt_ip):
                keep.mgmt_ip = other.mgmt_ip
        out.extend([r for r in rows if not any(r is c for c in cdp)])
    return out


LLDPCTL_SECTIONS = (".chassis.", ".port.", ".vlan", ".via", ".rid", ".age",
                    ".ttl", ".unknown-tlvs", ".ppvid", ".pi", ".med", ".lldp-med")


def _split_lldpctl_key(key: str) -> Tuple[str, str]:
    """lldp.bond0.3674.chassis.name -> ('bond0.3674', 'chassis.name')."""
    if not key.startswith("lldp."):
        return "", ""
    rest = key[5:]
    best = len(rest)
    for token in LLDPCTL_SECTIONS:
        pos = rest.find(token)
        if pos != -1:
            best = min(best, pos)
    if best == len(rest):
        return rest, ""
    return rest[:best], rest[best + 1:]


def parse_lldpctl(text: str) -> List[Neighbor]:
    per: Dict[str, Dict[str, str]] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        iface, field = _split_lldpctl_key(key.strip())
        if not iface or not field:
            continue
        per.setdefault(iface, {})[field] = value.strip()
    out = []
    for iface, kv in per.items():
        nb = Neighbor(local_iface=iface, source="lldpctl", proto=kv.get("via", "LLDP"))
        nb.switch = kv.get("chassis.name", "")
        for cand in ("chassis.mac", "chassis.ifname", "chassis.local", "chassis.ip",
                     "chassis.port", "chassis.iface"):
            if kv.get(cand):
                nb.chassis_id = kv[cand]
                nb.chassis_id_subtype = cand.split(".")[-1]
                break
        for cand in ("port.ifname", "port.local", "port.mac", "port.ifalias", "port.port"):
            if kv.get(cand):
                nb.port = kv[cand]
                nb.port_id_subtype = cand.split(".")[-1]
                break
        nb.port_desc = kv.get("port.descr", "")
        nb.sys_desc = " ".join(kv.get("chassis.descr", "").split())
        nb.mgmt_ip = kv.get("chassis.mgmt-ip", "")
        nb.vlan = kv.get("vlan.vlan-id", "") or kv.get("vlan", "")
        nb.capabilities = ",".join(
            k.split(".")[1] for k, v in sorted(kv.items())
            if k.startswith("chassis.") and k.endswith(".enabled") and v.lower() == "on")
        if nb.switch or nb.chassis_id:
            out.append(nb)
    return out


def parse_lldptool(text: str, iface: str) -> Optional[Neighbor]:
    nb = Neighbor(local_iface=iface, source="lldptool", proto="LLDP")
    current = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith((" ", "\t")):
            current = raw.strip().rstrip(":")
            continue
        value = raw.strip()
        if current is None:
            continue
        low = current.lower()
        if "system name" in low:
            nb.switch = value
        elif "port description" in low:
            nb.port_desc = value
        elif "port id" in low:
            nb.port = value.split(":", 1)[-1].strip() if ":" in value else value
        elif "chassis id" in low:
            nb.chassis_id = value.split(":", 1)[-1].strip() if value.count(":") > 5 else value
        elif "system description" in low:
            nb.sys_desc = " ".join((nb.sys_desc + " " + value).split())
        elif "management address" in low and not nb.mgmt_ip:
            nb.mgmt_ip = value.split()[-1]
        elif "port vlan" in low or "pvid" in low:
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                nb.vlan = digits
    return nb if (nb.switch or nb.chassis_id) else None


NETWORKCTL_HEADERS = ["LINK", "CHASSIS ID", "SYSTEM NAME", "CAPS", "PORT ID", "PORT DESCRIPTION"]
NETWORKCTL_CAPS = {"o": "Other", "p": "Repeater", "b": "Bridge", "w": "WLAN-AP", "r": "Router",
                   "t": "Telephone", "d": "DOCSIS", "a": "Station", "c": "Customer-VLAN",
                   "s": "Service-VLAN", "m": "TPMR"}


def parse_networkctl(text: str) -> List[Neighbor]:
    """Column order differs between systemd versions, so slice by header position."""
    lines = text.splitlines()
    header_at = next((i for i, l in enumerate(lines)
                      if "CHASSIS ID" in l and "PORT ID" in l), None)
    if header_at is None:
        return []
    header = lines[header_at]
    found = sorted((header.index(h), h) for h in NETWORKCTL_HEADERS if h in header)
    spans = []
    for pos, (start, name) in enumerate(found):
        end = found[pos + 1][0] if pos + 1 < len(found) else None
        spans.append((name, start, end))

    out = []
    for line in lines[header_at + 1:]:
        if not line.strip() or line.startswith(("Capabilities:", " ")) or "listed" in line:
            continue
        cell = {name: line[start:end].strip() for name, start, end in spans}
        link = cell.get("LINK", "")
        if not link:
            continue
        nb = Neighbor(local_iface=link, source="networkctl", proto="LLDP",
                      chassis_id=cell.get("CHASSIS ID", ""), switch=cell.get("SYSTEM NAME", ""),
                      port=cell.get("PORT ID", ""), port_desc=cell.get("PORT DESCRIPTION", ""),
                      capabilities=",".join(NETWORKCTL_CAPS[c] for c in cell.get("CAPS", "")
                                            if c in NETWORKCTL_CAPS))
        if nb.switch or nb.chassis_id:
            out.append(nb)
    return out


def _toolbox_run(ssh: Ssh, command: str, seconds: int) -> str:
    """SUSE CoreOS hosts keep tcpdump inside `toolbox`, which needs a real shell."""
    shell = ssh.client.invoke_shell(width=512)
    shell.settimeout(2.0)
    buf = ""
    shell.send("toolbox --no-pull\n")
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            buf += shell.recv(8192).decode("utf-8", "replace")
        except Exception:
            pass
        if "toolbox-" in buf and "#" in buf.split("toolbox-")[-1]:
            break
        time.sleep(0.5)
    else:
        shell.close()
        return ""
    marker = "LF-DONE-%d" % int(time.time())
    shell.send(command + "\necho " + marker + "\n")
    out = ""
    deadline = time.time() + seconds + 45
    while time.time() < deadline:
        try:
            out += shell.recv(65535).decode("utf-8", "replace")
        except Exception:
            pass
        if out.count(marker) > 1:
            break
        time.sleep(0.5)
    shell.close()
    return out


def select_ifaces(ifaces: Dict[str, Iface], target_ip: str, want: Optional[List[str]],
                  mgmt_only: bool) -> List[str]:
    if want:
        chosen: List[str] = []
        for name in want:
            chosen.extend(physical_leaves(ifaces, name) or [name])
        return list(dict.fromkeys(chosen))
    if mgmt_only:
        mgmt = iface_for_ip(ifaces, target_ip)
        if mgmt:
            return physical_leaves(ifaces, mgmt) or [mgmt]
    live = [i for i in ifaces.values()
            if i.physical and not i.virtual_prefix and i.carrier == "1"]
    if not live:
        live = [i for i in ifaces.values() if i.physical and not i.virtual_prefix]
    return [i.name for i in live]


def probe_linux(ssh: Ssh, result: TargetResult, target_ip: str,
                want_ifaces: Optional[List[str]] = None, mgmt_only: bool = False,
                seconds: int = 65, platform: str = "linux",
                log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)

    rc, out, _err = ssh.run("sh -c %s" % shlex.quote(IFACE_SCRIPT), timeout=30)
    ifaces = parse_ifaces(out)
    result.device_name = ssh.run("hostname 2>/dev/null || uname -n", timeout=15)[1].strip()
    if not ifaces:
        result.error = "could not enumerate interfaces (is this a Linux host?)"
        return result
    mgmt_iface = iface_for_ip(ifaces, target_ip)
    mgmt_legs = set(physical_leaves(ifaces, mgmt_iface) or [mgmt_iface]) if mgmt_iface else set()
    chosen = select_ifaces(ifaces, target_ip, want_ifaces, mgmt_only)
    if not want_ifaces:
        chosen = list(dict.fromkeys(chosen + [m for m in sorted(mgmt_legs) if m in ifaces]))
    physical = sum(1 for i in ifaces.values() if i.physical and not i.virtual_prefix)
    say("interfaces: %d found, %d physical | probing: %s"
        % (len(ifaces), physical, ", ".join(chosen) or "none"))
    if mgmt_iface:
        through = " (physically %s)" % ", ".join(sorted(mgmt_legs)) \
            if mgmt_legs != {mgmt_iface} else ""
        say("the address you gave, %s, lives on %s%s" % (target_ip, mgmt_iface, through))
    if not chosen:
        result.error = "no physical interface with carrier found"
        return result

    def role_of(name: str) -> str:
        if name == mgmt_iface:
            return "mgmt"
        if name in mgmt_legs:
            return "mgmt-leg"
        itf = ifaces.get(name)
        return "bond-slave" if itf and itf.master else "physical"

    def attach(nbs: List[Neighbor]) -> int:
        added = 0
        for nb in nbs:
            itf = ifaces.get(nb.local_iface)
            if itf:
                nb.local_mac, nb.local_speed = itf.mac, itf.speed
                nb.local_parent = itf.master
            nb.local_role = role_of(nb.local_iface)
            result.add(nb)
            added += 1
        return added

    def step(name: str, fn: Callable[[], List[Neighbor]], only_for: Optional[List[str]] = None) -> bool:
        started = time.time()
        try:
            found = fn()
        except Exception as exc:
            result.attempts.append(ProbeAttempt(name, False, "%s: %s" % (type(exc).__name__, exc),
                                                round(time.time() - started, 1)))
            say("  %-22s error: %s" % (name, exc))
            return False
        wanted = only_for if only_for is not None else chosen
        found = [n for n in found if not wanted or n.local_iface in wanted or not n.local_iface]
        attach(found)
        ok = bool(found)
        result.attempts.append(ProbeAttempt(name, ok, "%d neighbour(s)" % len(found),
                                            round(time.time() - started, 1)))
        say("  %-22s %s" % (name, "%d neighbour(s)" % len(found) if ok else "nothing"))
        return ok

    def missing() -> List[str]:
        have = {n.local_iface for n in result.neighbors if n.resolved}
        return [i for i in chosen if i not in have]

    if ssh.have("lldpctl"):
        step("lldpctl", lambda: parse_lldpctl(ssh.run("lldpctl -f keyvalue", timeout=25)[1]))
    elif ssh.have("lldpcli"):
        step("lldpcli", lambda: parse_lldpctl(ssh.run("lldpcli -f keyvalue show neighbors", timeout=25)[1]))
    if missing() and ssh.have("networkctl"):
        step("networkctl-lldp", lambda: parse_networkctl(ssh.run("networkctl lldp --no-pager", timeout=20)[1]))
    if missing() and ssh.have("lldptool"):
        pending = missing()

        def lldptool_all() -> List[Neighbor]:
            found = []
            for name in pending:
                text = ssh.run("lldptool -t -n -i %s" % shlex.quote(name), timeout=20)[1]
                nb = parse_lldptool(text, name)
                if nb:
                    found.append(nb)
            return found
        step("lldptool", lldptool_all, only_for=pending)

    pending = missing()
    captured = {"ran": False}
    if pending:
        has_tcpdump = ssh.have("tcpdump")
        has_tshark = ssh.have("tshark")
        name_map = {n.replace("/", "_"): n for n in pending}
        if has_tcpdump or has_tshark:
            tool = "tcpdump" if has_tcpdump else "tshark"
            say("  capturing with %s for %ds on %s" % (tool, seconds, ", ".join(pending)))
            script = _capture_script(pending, seconds, tool)

            def run_capture() -> List[Neighbor]:
                rc, out, _err = ssh.sudo("sh -c %s" % shlex.quote(script), timeout=seconds + 60)
                captured["ran"] = rc == 0
                return _parse_capture_output(out, name_map, tool, say)
            step("%s-capture" % tool, run_capture, only_for=pending)
        elif platform == "suse" and ssh.have("toolbox"):
            say("  no tcpdump on the host; entering toolbox for %ds" % seconds)
            script = _capture_script(pending, seconds, "tcpdump")

            def run_toolbox() -> List[Neighbor]:
                out = _toolbox_run(ssh, script, seconds)
                captured["ran"] = bool(out.strip())
                return _parse_capture_output(out, name_map, "toolbox-tcpdump", say)
            step("toolbox-tcpdump", run_toolbox, only_for=pending)

    pending = missing()
    if pending and captured["ran"]:
        say("  not running the raw sniffer: a full %ds capture already came back empty on %s"
            % (seconds, ", ".join(pending)))
    elif pending:
        python = "python3" if ssh.have("python3") else ("python" if ssh.have("python") else "")
        if python:
            say("  falling back to the raw-socket sniffer (%s) for %ds" % (python, seconds))

            def sniff() -> List[Neighbor]:
                args = "%s %d" % (shlex.quote(",".join(pending)), seconds)
                _rc, text, _err = push_and_run_python(ssh, SNIFFER_SRC, args,
                                                      timeout=seconds + 45, python=python)
                found = []
                for line in text.splitlines():
                    if not line.startswith("FRAME "):
                        continue
                    _tag, name, blob = line.split(" ", 2)
                    nb = decode_frame(base64.b64decode(blob))
                    if nb:
                        nb.local_iface = name
                        nb.source = "raw-sniffer"
                        found.append(nb)
                return found
            step("raw-sniffer", sniff, only_for=pending)

    result.neighbors = prefer_lldp(result.neighbors)
    still = missing()
    heard = {n.local_iface for n in result.neighbors}
    for name in chosen:
        if name in heard:
            continue
        itf = ifaces.get(name)
        blank = Neighbor(host=target_ip, local_iface=name, local_role=role_of(name),
                         confidence="none", note="probed, no LLDP/CDP frame received")
        if itf:
            blank.local_mac, blank.local_speed, blank.local_parent = itf.mac, itf.speed, itf.master
        result.neighbors.append(blank)
    result.neighbors.sort(key=lambda n: (0 if n.local_role.startswith("mgmt") else 1,
                                         0 if n.resolved else 1, n.local_iface))
    if still and not result.error:
        result.error = ("no LLDP/CDP received on: %s (switch port may have discovery "
                        "disabled - try the switch side)" % ", ".join(still))
    return result


def run_linux_target(host: str, creds: Creds, **kw) -> TargetResult:
    result = TargetResult(target=host, kind="linux")
    log = kw.pop("log", None)
    try:
        with Ssh(host, creds) as ssh:
            platform = ssh.platform()
            result.kind = platform
            if platform == "esxi":
                from .probe_esxi import probe_esxi
                return probe_esxi(ssh, result, log=log)
            if platform == "windows":
                from .probe_windows import probe_windows_over_ssh
                return probe_windows_over_ssh(ssh, result, log=log)
            if platform == "unknown":
                from .probe_switchcli import probe_switch_as_target
                if log:
                    log("  not a Linux/ESXi/Windows host - treating it as a network device")
                return probe_switch_as_target(host, creds, result, log=log, ssh=ssh)
            return probe_linux(ssh, result, host, platform=platform, log=log, **kw)
    except SshError as exc:
        result.error = str(exc)
        return result

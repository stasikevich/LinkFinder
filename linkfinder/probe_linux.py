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
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import handsets, hardware, serialports
from .decode import decode_frame, iter_capture, neighbors_from_capture
from .model import Neighbor, ProbeAttempt, TargetResult
from .netns import explain_dark
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


# A capture that never started and a capture that heard nothing look identical
# in the output, so the script says which one it was. The shell echoes the
# script back, so the script must never contain the marker as a literal or the
# echo alone would answer the question - it is printed in two pieces instead.
NO_TOOL_MARK = "LF-NOTOOL"

# How long to keep listening after every port being waited for has spoken.
# It was 8 seconds, which is shorter than the interval between one device's
# advertisements - so a wire carrying two of them gave up the first and
# missed the second. Measured: a management port that faces both a switch
# and a virtual machine reported only the switch. LLDP goes out every 30
# seconds by default (IEEE 802.1AB msgTxInterval), so a full cycle plus a
# little is what it takes to hear everyone on the wire rather than
# whoever happened to speak first.
GRACE = 34

# When the host's own LLDP agent is up and has heard neighbours, the ports it
# did not name are quiet for real. They get this short top-up capture instead of
# the full window - one LLDP tick (30s) is plenty of safety, and the raw-socket
# fallback is skipped. Without a working agent the full window still applies.
AGENT_TOPUP = 30

# A total budget for reading radios' own serials over NETCONF. The radios are
# already named and placed from their MAC/logs; NETCONF only adds the serial,
# best-effort, and a radio whose M-plane is slow or down must not let that step
# run away. Once this much has been spent, the rest are left with what the logs
# gave. Per-radio the pty script bounds itself too.
NETCONF_BUDGET = 30


def _capture_script(ifaces: List[str], seconds: int, tool: str,
                    pcap_filter: str = "", count: int = 5,
                    wait_it_out: bool = False,
                    expect: Optional[int] = None) -> str:
    """One tcpdump/tshark per interface in parallel, results returned base64.

    The script always waits the whole window, so taking several frames instead of
    one costs nothing and gives both protocols a chance to show up.
    """
    pcap_filter = pcap_filter or GROUP_MAC_FILTER
    lines = ["command -v %s >/dev/null 2>&1 || printf '%s%%s\n' '%s'"
             % (tool, NO_TOOL_MARK[:5], NO_TOOL_MARK[5:]),
             "d=$(mktemp -d /tmp/lf.XXXXXX)"]
    for name in ifaces:
        safe = name.replace("/", "_")
        if tool == "tcpdump":
            cap = ("tcpdump -i %s -s 0 -c %d -p -U -w $d/%s.pcap %s >/dev/null 2>&1 &"
                   % (shlex.quote(name), count, safe, shlex.quote(pcap_filter)))
        else:
            cap = ("tshark -i %s -c %d -w $d/%s.pcap -f %s >/dev/null 2>&1 &"
                   % (shlex.quote(name), count, safe, shlex.quote(pcap_filter)))
        lines.append(cap)
    # Waiting the whole window when the answer already arrived is where
    # most of a scan's time went. One advertisement names the switch and the
    # port; the grace period after the last interface lands is there so a
    # second protocol on the same wire is not cut off.
    if wait_it_out:
        # Somebody asked for the whole window: a wire can carry two discovery
        # protocols at different intervals, and only waiting catches both.
        lines.append("sleep %d" % seconds)
        lines.append("for p in $d/*.pcap; do [ -s \"$p\" ] || continue; "
                     "n=${p##*/}; echo \"PCAP ${n%.pcap}\"; "
                     "base64 \"$p\" | tr -d '" + chr(92) + chr(92) + "n'; echo; done")
        lines.append("kill $(jobs -p) 2>/dev/null; rm -rf $d")
        return chr(10).join(lines)
    # How many of these ports have to produce a frame before there is
    # nothing left to wait for. Every port is still listened to; the ones
    # that have never spoken simply do not hold the rest back, because a
    # port with no neighbour never answers and waiting for it is waiting
    # for the window to run out.
    lines.append("want=%d" % (len(ifaces) if expect is None else max(0, expect)))
    lines.append("left=%d" % seconds)
    lines.append("grace=0")
    lines.append("while [ $left -gt 0 ]; do")
    lines.append("  have=0")
    # A pcap is 24 bytes of header the moment tcpdump starts, so "not empty"
    # was true immediately and the early exit fired after the grace period
    # every time, losing neighbours that had not spoken yet. A record header
    # is another 16 bytes, so anything past 40 means a real frame landed.
    lines.append("  for p in $d/*.pcap; do [ -f \"$p\" ] || continue; "
                 "[ \"$(wc -c < \"$p\")\" -gt 40 ] && have=$((have+1)); done")
    lines.append("  if [ $have -ge $want ]; then")
    lines.append("    grace=$((grace+2))")
    lines.append("    [ $grace -ge %d ] && break" % GRACE)
    lines.append("  fi")
    lines.append("  sleep 2")
    lines.append("  left=$((left-2))")
    lines.append("done")
    lines.append("for p in $d/*.pcap; do [ -s \"$p\" ] || continue; "
                 "n=${p##*/}; echo \"PCAP ${n%.pcap}\"; base64 \"$p\" | tr -d '\\n'; echo; done")
    lines.append("kill $(jobs -p) 2>/dev/null; rm -rf $d")
    return "\n".join(lines)


def capture_landed(text: str) -> bool:
    """Whether a capture came back, as opposed to merely having been started.

    A shell transcript always holds a prompt and the echoed script, so any
    output at all used to count as proof that the capture had happened, and
    that quietly suppressed the raw-socket sniffer - the one method left on a
    host where tcpdump cannot run. Only a pcap coming back is proof.
    """
    return "PCAP " in (text or "")


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


NOT_A_NAME = ("n/a", "na", "unknown", "none", "-", "")

CUT_MARKS = ("…", "...")


def uncut(value: str):
    """networkctl pads its output into columns and cuts what does not fit.

    A port called "10GigabitEthern..." is not a port, it is a column that ran
    out of room. Returning it as a name invites somebody to search a switch
    for it. The text is kept because the start of it is still useful, and the
    fact that it was cut is kept with it.
    """
    text = (value or "").strip()
    for mark in CUT_MARKS:
        if text.endswith(mark):
            return text[:-len(mark)].rstrip(), True
    return text, False


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
        # The table is one block. After it come a blank line, a capability
        # legend and a count, and the legend is wide enough to slice into
        # plausible-looking columns - so stop at the blank rather than try
        # to name every thing that is not a neighbour.
        if not line.strip():
            break
        if line.startswith(("Capabilities:", "Capability", " ")) or "listed" in line:
            continue
        cell = {name: line[start:end].strip() for name, start, end in spans}
        link = cell.get("LINK", "")
        if not link:
            continue
        port, port_cut = uncut(cell.get("PORT ID", ""))
        name, name_cut = uncut(cell.get("SYSTEM NAME", ""))
        desc, desc_cut = uncut(cell.get("PORT DESCRIPTION", ""))
        name = "" if name.lower() in NOT_A_NAME else name
        desc = "" if desc.lower() in NOT_A_NAME else desc
        nb = Neighbor(local_iface=link, source="networkctl", proto="LLDP",
                      chassis_id=cell.get("CHASSIS ID", ""), switch=name,
                      port=port, port_desc=desc,
                      capabilities=",".join(NETWORKCTL_CAPS[c] for c in cell.get("CAPS", "")
                                            if c in NETWORKCTL_CAPS))
        if port_cut or name_cut or desc_cut:
            nb.extra["networkctl_truncated"] = True
            nb.note = ("networkctl cut this to fit its column - the far end is "
                       "longer than shown; read it from the switch to be sure")
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


def _optic_label(record):
    from .optics import label

    return label(record) or "module present"


PTP_CAPTURE_SECONDS = 4
PTP_CAPTURE_FRAMES = 25


def _probe_ptp(ssh: Ssh, result: TargetResult, ifaces: Dict[str, Iface],
               chosen: List[str], unresolved: List[str],
               say: Callable[[str], None]) -> None:
    """Timing role per port, and the far end of any cable LLDP could not name.

    Reading the daemon state is two cheap commands and happens for every port.
    The extra capture is only run on ports LLDP left unresolved, because that is
    where it pays: a fronthaul cable with no switch in it carries PTP and
    nothing else we can read.
    """
    from .ptp import (PTP_FILTER, PtpPort, collect_ptp, peers_from_capture,
                      query_segment)

    def run(command: str) -> str:
        # pmc answers in under a second when the PTP daemon is up; when a pod's
        # socket is wedged the read must not sit for half a minute.
        return ssh.run(command, timeout=12)[1]

    try:
        ports = collect_ptp(run, chosen, say)
    except Exception as exc:
        say("  ptp: %s: %s" % (type(exc).__name__, exc))
        return

    want = [name for name in unresolved if ifaces.get(name)
            and ifaces[name].carrier == "1"]
    if want:
        tool = "tcpdump" if ssh.have("tcpdump") else ("tshark" if ssh.have("tshark") else "")
        if tool:
            say("  listening %ds for PTP on %s" % (PTP_CAPTURE_SECONDS, ", ".join(want)))
            script = _capture_script(want, PTP_CAPTURE_SECONDS, tool,
                                     pcap_filter=PTP_FILTER, count=PTP_CAPTURE_FRAMES)
            name_map = {n.replace("/", "_"): n for n in want}
            try:
                _rc, out, _err = ssh.sudo("sh -c %s" % shlex.quote(script),
                                          timeout=PTP_CAPTURE_SECONDS + 15)
            except Exception as exc:
                say("  ptp capture failed: %s" % exc)
                out = ""
            our_macs = [i.mac for i in ifaces.values() if i.mac]
            lines = out.splitlines()
            for i, line in enumerate(lines):
                if not line.startswith("PCAP ") or i + 1 >= len(lines):
                    continue
                name = name_map.get(line[5:].strip(), line[5:].strip())
                try:
                    blob = base64.b64decode(lines[i + 1].strip())
                except Exception:
                    continue
                peers = peers_from_capture(blob, our_macs)
                if not peers:
                    continue
                port = ports.setdefault(name, PtpPort(iface=name))
                port.peers = peers
                say("    %s: %s" % (name, ", ".join(
                    "%s (%s)" % (p.label, "/".join(p.messages) or "ptp") for p in peers)))

    for name, port in ports.items():
        if not port.peers:
            continue
        domain = port.domain if port.domain >= 0 else port.peers[0].domain
        found = query_segment(run, name, domain=max(domain, 0), log=say)
        port.responders = found
        if found:
            say("    %s: %d clock(s) answered the segment query" % (name, len(found)))

    active = {name: port for name, port in ports.items() if port.active}
    if not active:
        return
    result.extra.setdefault("ptp", {})
    for name, port in active.items():
        result.extra["ptp"][name] = port.to_dict()


VIRT_NAMES = {
    "kvm": "a KVM guest", "qemu": "a QEMU guest", "vmware": "a VMware guest",
    "microsoft": "a Hyper-V guest", "xen": "a Xen guest",
    "oracle": "a VirtualBox guest", "bochs": "a Bochs guest",
    "lxc": "a container", "docker": "a container", "podman": "a container",
    "systemd-nspawn": "a container", "openvz": "a container",
}


def bond_of(ifaces, name: str) -> str:
    """The bond this port belongs to, and nothing else.

    A master is not a bond. A bridge member has a master too, and reading it
    as an aggregation makes the LACP check report "this end is bundled, the
    switch port is not" on every port of an ordinary hypervisor - a warning
    on a healthy host, which is how a column stops being read.
    """
    itf = (ifaces or {}).get(name)
    master = getattr(itf, "master", "") if itf is not None else ""
    if not master or master == name:
        return ""
    owner = (ifaces or {}).get(master)
    if owner is not None and getattr(owner, "kind", "") not in ("bond", "team"):
        return ""
    return master


FDB_SCRIPT = ("command -v bridge >/dev/null 2>&1 && "
              "bridge fdb show 2>/dev/null | head -400 || true")


def parse_fdb(text: str) -> dict:
    """`bridge fdb show` as address -> the bridge port it was seen on.

    Only what the bridge learned is useful here. A `permanent` row is the
    port's own address, put there by the kernel rather than seen coming
    in, and taking it for a neighbour attaches a host to itself.
    """
    out: dict = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or "permanent" in parts or "self" in parts:
            continue
        mac = parts[0].lower()
        if mac.count(":") != 5 or mac.startswith(("01:00:5e", "33:33")):
            continue
        held = {"dev": "", "master": ""}
        for name in ("dev", "master"):
            if name in parts:
                where = parts.index(name) + 1
                if where < len(parts):
                    held[name] = parts[where]
        if held["dev"]:
            out.setdefault(mac, held)
    return out


def fdb_of(ssh) -> dict:
    rc, out, _err = ssh.run("sh -c %s" % shlex.quote(FDB_SCRIPT), timeout=20)
    return parse_fdb(out if rc == 0 else "")


def read_virtualisation(text: str) -> dict:
    """Whether this is a machine or something running on one.

    It changes the answer rather than decorating it. A guest has no switch
    port of its own - its cable is the hypervisor's - so telling somebody to
    "try the switch side" sends them hunting for a port that cannot exist.
    """
    found = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if parts[0] == "DMI" and len(parts) >= 3:
            found[parts[1]] = parts[2]
        elif parts[0] == "VIRT" and len(parts) >= 2 and parts[1] not in ("", "none"):
            found["virt"] = parts[1]
    kind = found.get("virt", "")
    if kind:
        product = found.get("product_name", "")
        if "openstack" in product.lower():
            found["what"] = "an OpenStack instance"
        else:
            found["what"] = VIRT_NAMES.get(kind.lower(), "a %s guest" % kind)
    return found


def _probe_neighbor6(ssh, result: TargetResult, ifaces, chosen, say) -> None:
    """Ask the ports a cable reaches but no protocol does.

    Only ports still without a neighbour after every passive method, with a
    carrier and no IPv4 of their own. On a live network that is usually none
    of them; on fronthaul wired host to host it is the only thing that works.
    """
    from .neighbor6 import discover

    have = {n.local_iface for n in result.neighbors if n.resolved}
    dark = []
    for name in chosen:
        if name in have:
            continue
        itf = ifaces.get(name)
        if itf is None or itf.carrier != "1":
            continue
        if any("." in addr for addr in (itf.addrs or [])):
            continue
        dark.append(name)
    if not dark:
        return

    def run(command: str) -> str:
        return ssh.run(command, timeout=25)[1]

    try:
        found = discover(run, dark, log=say)
    except Exception as exc:
        say("  neighbour probe skipped: %s" % exc)
        return
    if found:
        result.extra.setdefault("neigh6", {}).update(found)


def _probe_namespaces(ssh, result: TargetResult, say,
                      identify: bool = True) -> Dict[str, Any]:
    """Look for ports the host is holding inside a container or a pod.

    Done for every Linux host, not only the ones that look odd: there is no
    cheap signal that says "this machine is hiding a port", and the cost is
    two reads on a host that has nothing to hide.
    """
    from .netns import discover

    # Reading another namespace needs root. On a host logged into as root this
    # is a plain run; on a host reached as a non-root account that must su, it
    # is the su escalation, so the radios on the fronthaul are discovered the
    # same either way instead of being hidden behind "not root". ssh.sudo picks
    # whichever applies.
    def run(command: str) -> str:
        # crictl / nsenter answer quickly on a healthy host; a wedged pod socket
        # must not hold the whole scan for a minute and a half.
        return ssh.sudo(command, timeout=25)[1]

    try:
        found = discover(run, say)
    except Exception as exc:
        say("  namespaces: %s: %s" % (type(exc).__name__, exc))
        return {}
    if found.get("refused"):
        result.extra["netns_hidden"] = found["refused"]
    spaces = found.get("spaces") or {}
    if spaces:
        result.extra["netns"] = {ino: sp.to_dict()
                                 for ino, sp in spaces.items()}
        if identify:
            _identify_hidden(ssh, result, spaces, say)
        # Which physical port owns each virtual function. Without it a port
        # and its own virtual function look like two ports with a cable
        # between them.
        if found.get("vfs"):
            result.extra["netns_vfs"] = dict(found["vfs"])
    return found


def _identify_hidden(ssh, result: TargetResult, spaces, say,
                     limit: int = 16) -> None:
    """Ask the devices behind a container's ports what they are.

    They are the ones with no name and no credentials anybody has, so the
    little a device says before it asks who you are is the only thing that
    will ever name them. The probe runs inside the namespace, because that
    is the only place their addresses exist.
    """
    from .identify import collect

    # nsenter into a pod's namespace needs root, so this escalates the same
    # way the namespace listing did - otherwise a host reached as a non-root
    # account sees the ports but never gets to ask the radios what they are.
    def run(command: str) -> str:
        # The deployed NETCONF/identify probes bound their own time (the pty
        # script caps itself, the port scan uses short per-port timeouts), so
        # this ceiling is only a backstop - it does not need to be minutes.
        return ssh.sudo(command, timeout=40)[1]

    # The radio's own serial, over NETCONF, when its M-plane is up and the
    # build carries a login for it. Standard ietf-hardware, read-only, best
    # effort: a down M-plane or no login simply adds nothing.
    from .baked import radio_creds
    from . import netconf

    rcreds = radio_creds()
    netconf_ready = bool(rcreds) and netconf.deploy(run)
    nc_deadline = time.time() + NETCONF_BUDGET
    nc_said = {"budget": False}

    # The radios also sit in this host's own DHCP leases, which do not age out
    # of a cache the way the namespace neighbour table does - so they are a far
    # steadier source of a radio's address than whatever the ND cache happens
    # to hold the moment the scan runs. Probed inside the namespace all the
    # same; one that is not reachable there just answers nothing.
    leased = result.extra.get("dhcp") or {}

    known = {}
    for space in spaces.values():
        ours = {i.name for i in space.hardware()}
        wanted, by_ip = [], {}
        for entry in space.neighbours:
            if entry.get("iface") in ours and entry.get("ip"):
                wanted.append(entry["ip"])
                by_ip[entry["ip"]] = entry.get("mac", "")
        for mac, rec in leased.items():
            addr = str((rec or {}).get("address") or "")
            if addr and addr not in by_ip:
                wanted.append(addr)
                by_ip[addr] = mac
        if not wanted:
            continue
        try:
            found = collect(run, wanted[:limit], pid=space.pid, log=say)
        except Exception as exc:
            say("  identify: %s: %s" % (type(exc).__name__, exc))
            continue
        for ip, what in found.items():
            over_budget = time.time() >= nc_deadline
            if netconf_ready and over_budget and not nc_said["budget"]:
                say("  netconf: radio serial budget spent - leaving the rest to "
                    "what the logs gave")
                nc_said["budget"] = True
            if netconf_ready and not over_budget and what.get("kind") == "radio":
                hw = netconf.read_hardware(run, space.pid, ip, rcreds, log=say)
                if hw:
                    facts = list(what.get("facts") or [])
                    tag = ", ".join(b for b in (hw.get("model"),
                        "serial %s" % hw["serial"] if hw.get("serial") else "") if b)
                    if tag:
                        facts.append("from the radio over NETCONF: " + tag)
                    what = dict(what, facts=facts, **hw)
            mac = by_ip.get(ip)
            if mac:
                known[mac] = dict(what, address=ip, through=space.label)
    if netconf_ready:
        netconf.cleanup(run)
    if known:
        result.extra["identified"] = known


def _our_other_half(result: TargetResult, port: str, mac: str) -> bool:
    """Is this answer the same physical port, rather than a neighbour?

    Probing a port leaves our own address in the cache at the other end of
    the card, so a container's port routinely lists the physical function as
    a neighbour. Counted as one, a cable with a single device on it looks
    like a shared segment and the device never reaches the map.
    """
    from .netns import one_port
    from .ptp import normalise_mac

    want = normalise_mac(mac)
    extra = result.extra or {}
    for name, ours in (extra.get("macs") or {}).items():
        if normalise_mac(ours) == want and one_port(extra, port, name):
            return True
    return False


def _rows_for_namespaces(result: TargetResult, target_ip: str,
                         found: Dict[str, Any]) -> None:
    """Give every hidden port a row of its own, named so it cannot be confused.

    The same interface name means a different port in each namespace, so the
    row is called "<port> in <pod>" and that is also the key used for the MAC
    and neighbour tables. Keying on the bare name would quietly merge a pod's
    virtual function with the host's physical one.

    What the namespace's own neighbour cache holds is handed to the usual
    pairing code as cache evidence, because that is exactly what it is - and
    it was free, where the root namespace could only have got it by asking.
    """
    from .neighbor6 import EVIDENCE_LIVE

    spaces = (found or {}).get("spaces") or {}
    if not spaces:
        return
    macs = result.extra.setdefault("macs", {})
    cache = result.extra.setdefault("neigh6", {})
    for space in sorted(spaces.values(), key=lambda s: s.label):
        for itf in space.hardware():
            where = "%s in %s" % (itf.name, space.label)
            if itf.mac:
                macs[where] = itf.mac
            peers = [{"mac": n["mac"], "ip": n["ip"]}
                     for n in space.neighbours if n["iface"] == itf.name
                     and not _our_other_half(result, where, n["mac"])]
            if peers:
                cache[where] = {"peers": peers, "evidence": EVIDENCE_LIVE}
            note = ("this port is not in the host's own network - it is held "
                    "by %s" % space.label)
            if not peers:
                note += ", and nothing is in its neighbour cache"
            row = Neighbor(host=target_ip, local_iface=where,
                           local_netns=space.label, local_mac=itf.mac,
                           local_mtu=itf.mtu, local_role="physical",
                           confidence="none", source="namespace", note=note)
            result.neighbors.append(row)


def _explain_dark_ports(result: TargetResult, found: Dict[str, Any]) -> None:
    """Add the namespace explanation last, so nothing can overwrite it.

    PTP and the neighbour pairing both write a sentence onto an unresolved
    row, and whichever ran last used to win. This is the one fact that
    changes what an engineer does next - a port is not unplugged, its
    traffic is somewhere else - so it is added to the sentence rather than
    competing with it.
    """
    if not (found or {}).get("spaces"):
        return
    for nb in result.neighbors:
        if nb.resolved or nb.local_netns:
            continue
        said = explain_dark(nb.local_iface, found)
        if not said or said in (nb.note or ""):
            continue
        nb.extra["netns_explains"] = said
        nb.note = "%s. %s" % (nb.note.rstrip("."), said) if nb.note else said


def attach_neighbor6(result: TargetResult) -> None:
    """Put each answer on its own row, once the blank rows exist."""
    found = result.extra.get("neigh6") or {}
    if not found:
        return
    for nb in result.neighbors:
        port = found.get(nb.local_iface)
        if not port:
            continue
        macs = [p["mac"] for p in port.get("peers") or []]
        if not macs:
            continue
        # The sentence is left to name_peers(), which runs once every
        # target is in and can turn a MAC into a device. Writing one
        # here too would mean two descriptions of the same port.
        nb.extra["neigh6"] = port


def attach_ptp(result: TargetResult) -> None:
    """Hang the PTP findings on the rows, once every row exists.

    This runs after the blank rows for unresolved ports are made, because those
    are exactly the rows PTP has something to say about.
    """
    found = result.extra.get("ptp") or {}
    if not found:
        return
    for nb in result.neighbors:
        port = found.get(nb.local_iface)
        if not port:
            continue
        nb.extra["ptp"] = port
        if nb.resolved:
            continue
        peers = port.get("peers") or []
        bits = []
        if port.get("state") or port.get("role"):
            bits.append("PTP %s" % (port.get("state") or port.get("role")))
        if peers:
            bits.append("hears %s" % ", ".join(
                p.get("clock_identity") or p.get("mac") or "?" for p in peers))
        if bits:
            nb.note = "no LLDP/CDP here, but " + "; ".join(bits)


def probe_linux(ssh: Ssh, result: TargetResult, target_ip: str,
                want_ifaces: Optional[List[str]] = None, mgmt_only: bool = False,
                seconds: int = 65, platform: str = "linux", ptp: bool = True,
                neighbours: bool = True,
                identify: bool = True,
                adb: bool = False,
                read_serial: bool = False, serial_baud: int = 115200,
                resolve_switch: Optional[Callable] = None,
                log: Optional[Callable[[str], None]] = None,
                cancel=None) -> TargetResult:
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
        if not (itf and itf.master):
            return "physical"
        # Enslaved is not the same as bonded. A bridge member was reported as
        # "bond-slave", which reads as redundancy that is not there.
        kind = getattr(ifaces.get(itf.master), "kind", "")
        return "bridge-member" if kind in ("bridge", "team") else "bond-slave"

    def attach(nbs: List[Neighbor]) -> int:
        added = 0
        for nb in nbs:
            itf = ifaces.get(nb.local_iface)
            if itf:
                nb.local_mac, nb.local_speed = itf.mac, itf.speed
                nb.local_parent = bond_of(ifaces, itf.name)
                nb.local_mtu = itf.mtu
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

    # The switch, first. Where one is known it already holds, in its MAC
    # table, which port each of these interfaces is on - answered in a tenth
    # of a second. Those ports then drop out of `missing()` and are never
    # listened to, which is the difference between a scan of seconds and one
    # of minutes. Ports the switch cannot explain still fall through below.
    if resolve_switch is not None:
        mac_by_iface = {name: ifaces[name].mac for name in chosen
                        if ifaces.get(name) and ifaces[name].mac
                        and ifaces[name].physical}
        try:
            known = resolve_switch(target_ip, mac_by_iface) or []
        except Exception as exc:
            known = []
            say("  switch-first lookup skipped: %s" % exc)
        if known:
            attach(known)
            result.attempts.append(ProbeAttempt(
                "switch-first", True, "%d port(s) the switch already knew"
                % len(known), 0.0))
            say("  the switch already knows %d of these port(s) - not listening"
                " on them: %s" % (len(known),
                                  ", ".join(sorted(n.local_iface for n in known))))

    # Whether the host's own LLDP agent answered with a neighbour on any port.
    # If it did, the agent is up and listening, so a port it did NOT name is
    # genuinely quiet - we trust that and give the quiet ports only a short
    # top-up capture, no full window and no raw-socket fallback.
    agent_heard = False
    if ssh.have("lldpctl"):
        agent_heard = step("lldpctl", lambda: parse_lldpctl(ssh.run("lldpctl -f keyvalue", timeout=25)[1])) or agent_heard
    elif ssh.have("lldpcli"):
        agent_heard = step("lldpcli", lambda: parse_lldpctl(ssh.run("lldpcli -f keyvalue show neighbors", timeout=25)[1])) or agent_heard
    if missing() and ssh.have("networkctl"):
        agent_heard = step("networkctl-lldp", lambda: parse_networkctl(ssh.run("networkctl lldp --no-pager", timeout=20)[1])) or agent_heard
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
        agent_heard = step("lldptool", lldptool_all, only_for=pending) or agent_heard

    pending = missing()
    # The neighbour cache, before the capture. A pending port that already
    # holds a reachable peer is a direct cable the capture would only sit
    # silent on - LLDP was already tried above and found nothing, so there is
    # none to miss. Reading the cache costs one command and no waiting, and
    # those ports drop out of what must be listened to. may_ask=False is the
    # safety: this only reads what the kernel already learned, it never sends
    # a packet, so it cannot change what is on the wire or hide a second
    # talker the way a shortened capture once did.
    if pending:
        try:
            from .neighbor6 import discover as _nd_cache
            cached = _nd_cache(lambda c: ssh.run(c, timeout=20)[1], pending,
                               may_ask=False)
        except Exception as exc:
            cached = {}
            say("  neighbour-cache read skipped: %s" % exc)
        cached = {k: v for k, v in (cached or {}).items() if (v or {}).get("peers")}
        if cached:
            result.extra.setdefault("neigh6", {}).update(cached)
            pending = [p for p in pending if p not in cached]
            say("  %d port(s) already have a neighbour in the host's cache - "
                "not listening on them: %s"
                % (len(cached), ", ".join(sorted(cached))))

    captured = {"ran": False}
    if pending:
        has_tcpdump = ssh.have("tcpdump")
        has_tshark = ssh.have("tshark")
        name_map = {n.replace("/", "_"): n for n in pending}
        if has_tcpdump or has_tshark:
            tool = "tcpdump" if has_tcpdump else "tshark"
            # A port that has never announced anything held every scan open
            # for the whole window. What it did announce before is what is
            # waited for now; the rest are still listened to.
            from . import quiet
            listen_for, expect, full = quiet.window(target_ip, pending, seconds)
            if agent_heard:
                # The host's LLDP agent is up and heard neighbours elsewhere, so
                # these ports are quiet for real - a short top-up, not the window.
                listen_for = min(listen_for, AGENT_TOPUP)
                full = False
                say("  the host's LLDP agent is up - a short %ds top-up on the "
                    "quiet port(s), not the full window: %s"
                    % (listen_for, ", ".join(pending)))
            elif full:
                say("  capturing with %s for %ds on %s"
                    % (tool, listen_for, ", ".join(pending)))
            else:
                say("  capturing with %s for up to %ds on %s - %d of them "
                    "have announced themselves before, the rest are listened "
                    "to but not waited for"
                    % (tool, listen_for, ", ".join(pending), expect or 0))
            script = _capture_script(pending, listen_for, tool, expect=expect)

            def run_capture() -> List[Neighbor]:
                # The script waits `listen_for` itself then prints and exits;
                # the margin is only for the base64 of the pcaps. A big margin
                # just let a capture that hung under su sit until it timed out.
                rc, out, _err = ssh.sudo("sh -c %s" % shlex.quote(script),
                                         timeout=listen_for + 20)
                captured["ran"] = rc == 0 and capture_landed(out)
                if rc == 0 and not captured["ran"]:
                    say("    %s wrote no capture file at all" % tool)
                heard = _parse_capture_output(out, name_map, tool, say)
                # Only a full-length listen proves a port is silent; a
                # shortened one proves nothing about what it did not hear,
                # so it may add a talker but never take one away.
                try:
                    quiet.remember(target_ip, {n.local_iface for n in heard},
                                   pending, full=full)
                except Exception as problem:
                    say("    could not remember which ports spoke: %s" % problem)
                return heard
            step("%s-capture" % tool, run_capture, only_for=pending)
        elif platform == "suse" and ssh.have("toolbox"):
            say("  no tcpdump on the host; entering toolbox for %ds" % seconds)
            script = _capture_script(pending, seconds, "tcpdump")

            def run_toolbox() -> List[Neighbor]:
                out = _toolbox_run(ssh, script, seconds)
                captured["ran"] = capture_landed(out)
                if NO_TOOL_MARK in out:
                    say("    there is no tcpdump inside the toolbox either")
                elif out.strip() and not captured["ran"]:
                    say("    the toolbox returned no capture file at all,"
                        " so nothing was listened to")
                return _parse_capture_output(out, name_map, "toolbox-tcpdump", say)
            step("toolbox-tcpdump", run_toolbox, only_for=pending)

    pending = missing()
    if pending and agent_heard:
        say("  trusting the host's own LLDP agent for the quiet port(s): %s"
            % ", ".join(pending))
    elif pending and captured["ran"]:
        say("  not repeating it with the raw sniffer: the %ds capture ran and"
            " heard nothing on %s" % (seconds, ", ".join(pending)))
    elif pending:
        python = "python3" if ssh.have("python3") else ("python" if ssh.have("python") else "")
        if python:
            # The raw sniffer is the last resort, reached only when tcpdump
            # could not land a capture at all. A host should never pay the full
            # window twice, so this leg is capped - it still catches an LLDP
            # tick (30s) and gets one shot at a CDP one.
            fb_seconds = min(int(seconds), 45)
            say("  falling back to the raw-socket sniffer (%s) for %ds" % (python, fb_seconds))

            def sniff() -> List[Neighbor]:
                args = "%s %d" % (shlex.quote(",".join(pending)), fb_seconds)
                _rc, text, _err = push_and_run_python(ssh, SNIFFER_SRC, args,
                                                      timeout=fb_seconds + 45, python=python)
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

    # Which MAC belongs to which port of this device. The neighbour pairing
    # needs it to turn a heard MAC back into a host and a port, and the rows
    # for ports with no protocol neighbour do not survive into the map.
    result.extra["macs"] = {name: itf.mac for name, itf in ifaces.items()
                            if itf.mac and itf.physical and not itf.virtual_prefix}

    # The address a bond leg announces is not the address it answers on. A
    # switch names the far end by what it HEARD, so without the burned-in one
    # a four-leg bond arrives as four nameless boxes beside the real server.
    perm = {name: itf.perm_mac for name, itf in ifaces.items()
            if getattr(itf, "perm_mac", "") and itf.perm_mac != itf.mac}
    if perm:
        result.extra["perm_macs"] = perm

    # The port carrying the address you typed is the one answer nobody should
    # have to work out twice, and it vanished from the map because only ports
    # with a protocol neighbour survive the crawl.
    #
    # "Ports with no address are already drawn as links" was the old rule here
    # and it is false: a port is drawn as a link only if something was HEARD
    # on it. A lit cable nobody talks over was in no link and in no table, so
    # it vanished - which is exactly the port worth looking at. Measured on a
    # live host: three 10G ports up with carrier and good light, in no bond
    # and in no map.
    carrying = {}
    for name, itf in sorted(ifaces.items()):
        role = role_of(name)
        held = [a for a in (itf.addrs or []) if a]
        lit = itf.physical and str(itf.carrier or "").strip() == "1"
        if not held and not lit and not role.startswith("mgmt"):
            continue
        if itf.virtual_prefix and not role.startswith("mgmt"):
            continue
        carrying[name] = {"role": role, "mac": itf.mac or "",
                          "state": itf.operstate or "", "speed": itf.speed or "",
                          "mtu": itf.mtu or "", "addresses": held,
                          "carrier": "up" if lit else ""}
    if carrying:
        result.extra["ports"] = carrying

    # A host with a bridge really does pass frames between its ports, so a
    # ring through it is a ring. Without this the loop check assumes every
    # host is an endpoint.
    bridges = sorted(name for name, itf in ifaces.items()
                     if getattr(itf, "kind", "") in ("bridge", "team"))
    if bridges:
        result.extra["bridges"] = bridges
        # Which hardware addresses sit behind which bridge port. This is
        # how a virtual machine is tied to the host running it, whatever
        # the hypervisor calls its tap and whatever range the guest's
        # address came from: Proxmox hands out bc:24:11 and libvirt
        # 52:54:00, so matching on a range finds one and misses the other.
        # The table names them both, and names nothing it has not seen.
        try:
            table = fdb_of(ssh)
            if table:
                result.extra["fdb"] = table
        except Exception as problem:          # a probe is never fatal
            say("  bridge table not read: %s" % problem)

    virtual = read_virtualisation(out)
    if virtual:
        result.extra["virtual"] = virtual

    from .lag import bonds_from_ifaces
    from .linkcheck import tagged_vlans
    found_tags = tagged_vlans(ifaces)
    if found_tags:
        result.extra["tagged_vlans"] = found_tags
    from .sshx import parse_bonds
    detail = parse_bonds(out)
    found_bonds = bonds_from_ifaces(ifaces, known=set(detail))
    if found_bonds:
        result.extra["bonds"] = found_bonds
        if detail:
            result.extra["bond_detail"] = detail
    from .sensors import collect as collect_sensors
    cards = collect_sensors(lambda cmd: ssh.run(cmd, timeout=40)[1], say)
    if cards:
        result.extra["sensors"] = {slot: card.to_dict()
                                   for slot, card in cards.items()}
    from .dhcp_leases import collect as collect_leases
    leased = collect_leases(lambda cmd: ssh.run(cmd, timeout=30)[1], say)
    if leased:
        result.extra["dhcp"] = leased

    from .duplicates import collect as collect_neighbours
    learned = collect_neighbours(lambda cmd: ssh.run(cmd, timeout=30)[1], say)
    if learned:
        result.extra["neighbours"] = learned
    # The address a server answers on when it will not boot, read from
    # inside it. Also the one NetBox most often holds wrongly, because
    # nobody can see it from outside to check.
    from .ipmi import collect as collect_bmc
    bmc = collect_bmc(lambda cmd: ssh.run(cmd, timeout=40)[1], say, ssh=ssh)
    if bmc:
        result.extra["ipmi"] = bmc

    if ssh.have("ethtool"):
        from .optics import collect as collect_optics
        modules = collect_optics(lambda cmd: ssh.run(cmd, timeout=60)[1], say)
        if modules:
            result.extra["optics"] = modules
            say("  optics: %s"
                % ", ".join("%s %s" % (name, _optic_label(rec))
                            for name, rec in sorted(modules.items())))

    hardware.attach(result, hardware.read_linux(
        lambda cmd: ssh.run(cmd, timeout=30)[1]), say)

    # Say what we decided this host is, so the scan it gets is legible: a rack
    # server has no radio console cable (its COM ports are left shut), a bench
    # NUC/desktop does and is asked. The class comes from the SMBIOS chassis.
    _hw = result.extra.get("hardware") or {}
    _model = (hardware.describe(_hw) or "").strip()
    say("  this host looks like a %s%s"
        % ("rack server" if serialports.is_server(_hw) else "bench NUC/desktop/workstation",
           " (%s)" % _model if _model else ""))

    attached = handsets.collect(lambda cmd: ssh.run(cmd, timeout=60)[1], say,
                                windows=False, adb=adb)
    if attached:
        result.extra["usb"] = attached

    serial_run = lambda cmd: ssh.run(cmd, timeout=30)[1]
    found_serial = serialports.read_linux(serial_run)
    if read_serial and found_serial:
        from .baked import radio_creds
        say("  asking the radio console port(s) who they are at %d baud" % serial_baud)
        serialports.capture(lambda cmd: ssh.run(cmd, timeout=40)[1], found_serial,
                            platform="linux", baud=serial_baud, seconds=4,
                            creds=radio_creds(), say=say,
                            hardware=result.extra.get("hardware"), cancel=cancel)
    serialports.attach(result, found_serial, say)

    if ptp:
        _probe_ptp(ssh, result, ifaces, chosen, missing(), say)

    result.neighbors = prefer_lldp(result.neighbors)
    still = missing()

    # Radio serials this host records in its own files - a fallback for a radio
    # whose M-plane is down. Read before the namespace probe, on a clean
    # session, rather than after the NETCONF pty work that follows it. Only
    # runs when the build carries the (site-specific) rules; otherwise there is
    # nothing to look for and nothing happens.
    from .baked import radio_file_rules
    from . import radiolog

    rules = radio_file_rules()
    if rules:
        srun = lambda c: ssh.sudo(c, timeout=15)[1]
        serials = radiolog.read(srun, rules, log=say)
        if serials:
            result.extra["radios_in_logs"] = serials
        # The richer read: the device-info the host logged for each radio -
        # model, revisions - and the MAC it answers on, so a radio shows the
        # same facts a NETCONF get would give, with no login to the radio.
        detail = radiolog.details(srun, rules, log=say)
        if detail:
            result.extra["radios_detail"] = detail
            by_mac = {}
            for serial, rec in detail.items():
                clean = {k: v for k, v in rec.items() if k != "macs"}
                for mac in rec.get("macs") or []:
                    by_mac[mac] = clean
            if by_mac:
                result.extra["radio_detail_by_mac"] = by_mac

    hidden = _probe_namespaces(ssh, result, say, identify)
    heard = {n.local_iface for n in result.neighbors}
    for name in chosen:
        if name in heard:
            continue
        itf = ifaces.get(name)
        guest = (result.extra.get("virtual") or {}).get("what", "")
        why = ("probed, no LLDP/CDP frame received - this is %s, so its switch "
               "port belongs to the hypervisor, not to it" % guest if guest
               else "probed, no LLDP/CDP frame received")
        blank = Neighbor(host=target_ip, local_iface=name, local_role=role_of(name),
                         confidence="none", note=why)
        if itf:
            blank.local_mac, blank.local_speed = itf.mac, itf.speed
            blank.local_parent = bond_of(ifaces, name)
            blank.local_mtu = itf.mtu
        result.neighbors.append(blank)
    _rows_for_namespaces(result, target_ip, hidden)
    if neighbours:
        _probe_neighbor6(ssh, result, ifaces, chosen, say)
    attach_neighbor6(result)
    attach_ptp(result)
    _explain_dark_ports(result, hidden)
    result.neighbors.sort(key=lambda n: (0 if n.local_role.startswith("mgmt") else 1,
                                         0 if n.resolved else 1, n.local_iface))
    if still and not result.error:
        guest = (result.extra.get("virtual") or {}).get("what", "")
        if guest:
            result.error = ("no LLDP/CDP received on: %s - this is %s, and a guest "
                            "has no switch port of its own. The cable belongs to the "
                            "hypervisor, so scan that host instead"
                            % (", ".join(still), guest))
        else:
            result.error = ("no LLDP/CDP received on: %s (switch port may have "
                            "discovery disabled - try the switch side)"
                            % ", ".join(still))
    return result


def run_linux_target(host: str, creds: Creds, **kw) -> TargetResult:
    from . import cancel as _cancel
    result = TargetResult(target=host, kind="linux")
    log = kw.pop("log", None)
    cancel = kw.get("cancel")
    try:
        with Ssh(host, creds) as ssh:
            # Stop closes this SSH at once, so a read stuck on a capture window
            # unblocks instead of holding the crawl up until it times out.
            stop_watch = _cancel.close_on_cancel(cancel, ssh)
            try:
                platform = ssh.platform()
                result.kind = platform
                if platform == "esxi":
                    from .probe_esxi import probe_esxi
                    return probe_esxi(ssh, result, log=log)
                if platform == "windows":
                    from .probe_windows import probe_windows_over_ssh
                    return probe_windows_over_ssh(ssh, result, log=log,
                                                  adb=bool(kw.get("adb")),
                                                  read_serial=bool(kw.get("read_serial")),
                                                  serial_baud=int(kw.get("serial_baud")
                                                                  or 115200),
                                                  cancel=cancel)
                if platform == "unknown":
                    from .probe_switchcli import probe_switch_as_target
                    if log:
                        log("  not a Linux/ESXi/Windows host - treating it as a network device")
                    return probe_switch_as_target(host, creds, result, log=log, ssh=ssh)
                return probe_linux(ssh, result, host, platform=platform, log=log, **kw)
            finally:
                stop_watch()
    except SshError as exc:
        result.error = str(exc)
        return result

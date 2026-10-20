"""ESXi: ask the hypervisor what it already knows before capturing anything.

`vim-cmd hostsvc/net/query_networkhint` carries both the CDP and the LLDP view
per vmnic, so a healthy host answers instantly; pktcap-uw is only the fallback.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import re
import shlex
import time
from typing import Callable, Dict, List, Optional

from .decode import neighbors_from_capture
from .model import Neighbor, ProbeAttempt, TargetResult
from .sshx import Ssh

FIELD_RE = re.compile(r'^\s*([A-Za-z][\w]*)\s*=\s*"?(.*?)"?,?\s*$')
KEY_RE = re.compile(r'key\s*=\s*"([^"]+)"')
VAL_RE = re.compile(r'value\s*=\s*"?([^",]+)"?')

PARAM_MAP = {
    "system name": "switch",
    "system description": "sys_desc",
    "port description": "port_desc",
    "management address": "mgmt_ip",
    "port vlan": "vlan",
    "port vlan id": "vlan",
    "vlan id": "vlan",
}


def parse_network_hint(text: str) -> List[Neighbor]:
    """Brace depth is unreliable here; the `device =` and section markers are not."""
    records: Dict[tuple, Neighbor] = {}
    device = ""
    section = ""
    pending_key = ""

    def record(proto: str) -> Neighbor:
        key = (device, proto)
        if key not in records:
            records[key] = Neighbor(proto=proto, source="esxi:networkhint", local_iface=device)
        return records[key]

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("device =") or stripped.startswith("device="):
            match = FIELD_RE.match(line)
            if match and match.group(2).strip():
                device = match.group(2).strip()
                section = ""
            continue
        if stripped.startswith("connectedSwitchPort"):
            section = "" if "<unset>" in stripped else "cdp"
            continue
        if stripped.startswith("lldpInfo"):
            section = "" if "<unset>" in stripped else "lldp"
            continue
        if not section or not device:
            continue

        match = FIELD_RE.match(line)
        if match:
            name, value = match.group(1), match.group(2).strip()
            if value and value != "<unset>" and name not in ("key", "value"):
                nb = record("CDP" if section == "cdp" else "LLDP")
                if section == "cdp":
                    if name == "devId":
                        nb.switch = nb.chassis_id = value
                    elif name == "portId":
                        nb.port = value
                    elif name == "address":
                        nb.mgmt_ip = value
                    elif name in ("devicePlatform", "hardwarePlatform"):
                        nb.sys_desc = value
                    elif name == "vlan":
                        nb.vlan = value
                else:
                    if name == "chassisId":
                        nb.chassis_id = value
                    elif name == "portId":
                        nb.port = value

        if section == "lldp":
            key_hit = KEY_RE.search(line)
            if key_hit:
                pending_key = key_hit.group(1).strip().lower()
                continue
            val_hit = VAL_RE.search(line)
            if val_hit and pending_key:
                attr = PARAM_MAP.get(pending_key)
                if attr:
                    setattr(record("LLDP"), attr, val_hit.group(1).strip())
                pending_key = ""
    return [nb for nb in records.values() if nb.switch or nb.chassis_id]


def parse_esxcli_nics(text: str) -> Dict[str, Dict[str, str]]:
    nics: Dict[str, Dict[str, str]] = {}
    current: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current.get("Name"):
                nics[current["Name"]] = current
            current = {}
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current[key.strip().split(".")[-1]] = value.strip()
    if current.get("Name"):
        nics[current["Name"]] = current
    return nics


def _pktcap_script(nics: List[str], seconds: int) -> str:
    lines = ["d=/tmp/lf.$$", "mkdir -p $d"]
    for nic in nics:
        lines.append("pktcap-uw --uplink %s --capture UplinkRcv -o $d/%s.pcap -c 40 "
                     ">/dev/null 2>&1 &" % (shlex.quote(nic), nic))
    lines.append("sleep %d" % seconds)
    lines.append("kill $(jobs -p) 2>/dev/null; sleep 1")
    lines.append("for p in $d/*.pcap; do [ -s \"$p\" ] || continue; n=${p##*/}; "
                 "echo \"PCAP ${n%.pcap}\"; /bin/base64 \"$p\" 2>/dev/null | tr -d '\\n'; echo; done")
    lines.append("rm -rf $d")
    return "\n".join(lines)


def probe_esxi(ssh: Ssh, result: TargetResult, seconds: int = 65,
               log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)
    result.kind = "esxi"
    started = time.time()

    from . import hardware
    hardware.attach(result, hardware.read_esxi(
        lambda cmd: ssh.run(cmd, timeout=30)[1]), say)

    _rc, nic_text, _err = ssh.run("esxcli --formatter=keyvalue network nic list", timeout=30)
    nics = parse_esxcli_nics(nic_text)
    up = [n for n, info in nics.items() if info.get("LinkStatus", "").lower() == "up"] or list(nics)
    say("  vmnics: %s | link up: %s" % (", ".join(sorted(nics)) or "none", ", ".join(up) or "none"))

    _rc, hint, _err = ssh.run("vim-cmd hostsvc/net/query_networkhint", timeout=60)
    found = parse_network_hint(hint)
    for nb in found:
        info = nics.get(nb.local_iface, {})
        nb.local_mac = info.get("MACAddress", "")
        nb.local_speed = (info.get("Speed", "") + "Mb/s") if info.get("Speed") else ""
        nb.local_role = "physical"
        result.add(nb)
    result.attempts.append(ProbeAttempt("esxi-networkhint", bool(found),
                                        "%d neighbour(s)" % len(found), round(time.time() - started, 1)))
    say("  %-22s %s" % ("vim-cmd networkhint", "%d neighbour(s)" % len(found) if found else "nothing"))

    have = {n.local_iface for n in result.neighbors if n.resolved}
    missing = [n for n in up if n not in have]
    if missing and ssh.have("pktcap-uw"):
        say("  capturing with pktcap-uw for %ds on %s" % (seconds, ", ".join(missing)))
        begin = time.time()
        _rc, out, _err = ssh.run("sh -c %s" % shlex.quote(_pktcap_script(missing, seconds)),
                                 timeout=seconds + 60)
        added = 0
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("PCAP ") or i + 1 >= len(lines):
                continue
            nic = line[5:].strip()
            try:
                blob = base64.b64decode(lines[i + 1].strip())
            except Exception:
                continue
            for nb in neighbors_from_capture(blob):
                nb.local_iface = nic
                nb.source = "pktcap-uw"
                result.add(nb)
                added += 1
        result.attempts.append(ProbeAttempt("esxi-pktcap", bool(added), "%d neighbour(s)" % added,
                                            round(time.time() - begin, 1)))

    if not result.resolved and not result.error:
        result.error = ("ESXi has no CDP/LLDP for these vmnics - the vSwitch discovery "
                        "policy may be set to Listen=none, or the switch is not advertising")
    return result

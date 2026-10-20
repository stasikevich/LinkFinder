"""Switches over SSH, for the two jobs SNMP cannot always do.

1. Ask a switch which port a MAC is on, when SNMP is disabled.
2. Point the tool AT a switch and get its whole neighbour table - what is
   plugged into every port.

Output shapes differ per NOS but fall into three families: blocks of
`Key: value`, whitespace columns under a header, and RouterOS `key=value`.
One parser per family plus a per-vendor command table covers the lot.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from .model import Neighbor, ProbeAttempt, TargetResult
from .sshx import Creds, Ssh, SshError

NOISE = {"dynamic", "static", "secure", "learned", "self", "forward", "yes", "no",
         "cpu", "active", "management", "l", "d", "s", "*", "+", "-", "mac", "address"}


class Dialect:
    def __init__(self, name, detect, version_cmd, neighbour_cmds, mac_cmd, style, fields):
        self.name = name
        self.detect = detect
        self.version_cmd = version_cmd
        self.neighbour_cmds = neighbour_cmds
        self.mac_cmd = mac_cmd
        self.style = style
        self.fields = fields


BLOCK_FIELDS = {
    "local": ("local intf", "local interface", "local port", "localport", "interface"),
    "name": ("system name", "device id", "sysname", "neighbor system name", "chassis name"),
    "port": ("port id", "port-id", "portid", "port", "remote port", "neighbor port-id"),
    "desc": ("port description", "port descr", "portdescr"),
    "sysdesc": ("system description", "platform", "system descr"),
    "mgmt": ("management address", "mgmt address", "ip address", "management ip",
             "mgmt ip", "ip"),
    "vlan": ("vlan id", "pvid", "port vlan id", "native vlan"),
    "chassis": ("chassis id", "chassisid", "chassis"),
    "caps": ("enabled capabilities", "system capabilities", "capabilities"),
}

COLUMN_FIELDS = {
    "local": ("local interface", "local intf", "local port", "localport", "local",
              "interface", "port"),
    "name": ("system name", "neighbor device", "neighbour device", "device id",
             "neighbor name", "chassis name", "sysname", "neighbor", "device", "system"),
    "port": ("port info", "neighbor interface", "neighbour interface", "neighbor port",
             "remote port", "remote interface", "port id", "port-id", "portid"),
    "desc": ("port description", "port descr"),
    "mgmt": ("management address", "ip address", "mgmt address", "address"),
    "chassis": ("chassis id", "chassisid"),
    "caps": ("capability", "capabilities", "caps"),
}

MIKROTIK_FIELDS = {
    "local": ("interface",),
    "name": ("identity", "system-description"),
    "port": ("interface-name", "port-id"),
    "desc": ("port-description",),
    "sysdesc": ("system-description", "board", "platform"),
    "mgmt": ("address4", "address", "ipv4-address"),
    "chassis": ("mac-address",),
    "caps": ("system-caps-enabled", "system-caps"),
}

DIALECTS = [
    Dialect("mikrotik", ("routeros", "mikrotik"), "/system/resource/print",
            ["/ip/neighbor/print detail without-paging",
             "/ip neighbor print detail without-paging"],
            "/interface/bridge/host/print detail where mac-address={mac_upper}",
            "mikrotik", MIKROTIK_FIELDS),
    Dialect("nxos", ("nx-os", "nexus"), "show version",
            ["show lldp neighbors detail", "show cdp neighbors detail"],
            "show mac address-table address {mac_dot}", "block", BLOCK_FIELDS),
    Dialect("arista", ("arista",), "show version",
            ["show lldp neighbors detail"],
            "show mac address-table address {mac_colon}", "block", BLOCK_FIELDS),
    Dialect("juniper", ("junos", "juniper"), "show version",
            ["show lldp neighbors"],
            "show ethernet-switching table | match {mac_colon}", "columns", COLUMN_FIELDS),
    Dialect("aruba-cx", ("arubaos-cx", "aruba"), "show version",
            ["show lldp neighbor-info detail", "show lldp neighbor-info"],
            "show mac-address-table address {mac_colon}", "block", BLOCK_FIELDS),
    Dialect("procurve", ("procurve", "hp j", "hewlett"), "show version",
            ["show lldp info remote-device detail", "show lldp info remote-device"],
            "show mac-address {mac_dash}", "block", BLOCK_FIELDS),
    Dialect("dell", ("os10", "dell"), "show version",
            ["show lldp neighbors detail"],
            "show mac address-table address {mac_colon}", "block", BLOCK_FIELDS),
    Dialect("extreme", ("extremexos", "exos"), "show version",
            ["show lldp neighbors detail"],
            "show fdb {mac_colon}", "block", BLOCK_FIELDS),
    Dialect("huawei", ("huawei", "vrp", "h3c", "comware"), "display version",
            ["display lldp neighbor brief", "display lldp neighbor-information verbose"],
            "display mac-address {mac_dot}", "columns", COLUMN_FIELDS),
    Dialect("cisco", ("cisco", "ios"), "show version",
            ["show lldp neighbors detail", "show cdp neighbors detail"],
            "show mac address-table address {mac_dot}", "block", BLOCK_FIELDS),
]
GENERIC = Dialect("unknown", (), "show version",
                  ["show lldp neighbors detail", "show lldp neighbors",
                   "show lldp neighbor-info detail", "display lldp neighbor brief",
                   "/ip/neighbor/print detail without-paging"],
                  "show mac address-table address {mac_dot}", "block", BLOCK_FIELDS)


def detect_family(version_text: str) -> str:
    low = version_text.lower()
    for dialect in DIALECTS:
        if any(needle in low for needle in dialect.detect):
            return dialect.name
    return "unknown"


def dialect_for(name: str) -> Dialect:
    for dialect in DIALECTS:
        if dialect.name == name:
            return dialect
    return GENERIC


def mac_variants(mac: str) -> Dict[str, str]:
    plain = mac.replace(":", "").replace("-", "").replace(".", "").lower()
    return {
        "mac": ".".join(plain[i:i + 4] for i in range(0, 12, 4)),
        "mac_dot": ".".join(plain[i:i + 4] for i in range(0, 12, 4)),
        "mac_colon": ":".join(plain[i:i + 2] for i in range(0, 12, 2)),
        "mac_dash": "-".join(plain[i:i + 2] for i in range(0, 12, 2)),
        "mac_upper": ":".join(plain[i:i + 2] for i in range(0, 12, 2)).upper(),
        "mac_plain": plain,
    }


def _pick(record: Dict[str, str], names) -> str:
    for key in names:
        if record.get(key):
            return record[key]
    return ""


IPV4 = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _pick_ip(record: Dict[str, str], names) -> str:
    """A device often lists several addresses; take the first real IPv4."""
    for key in names:
        value = (record.get(key) or "").strip().split(",")[0].strip()
        if IPV4.match(value):
            return value
    return ""


def _is_error(text: str) -> bool:
    low = text.strip().lower()
    return (not low or low.startswith("%") or "invalid input" in low
            or "unknown command" in low or "syntax error" in low
            or "bad command" in low or "expected end of command" in low
            or "no such command" in low)


# --------------------------------------------------------------- parsers

def parse_block_records(text: str) -> List[Dict[str, str]]:
    """Blocks of `Key: value`, a new record whenever a key repeats."""
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    last_key = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or set(line.strip()) <= {"-", "=", "*", "_"}:
            continue
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9 ._/-]{1,40}?)\s*:\s*(.*)$", line)
        if not match:
            if last_key == "mgmt" and current.get("mgmt") == "":
                current["mgmt"] = line.strip()
            continue
        key, value = match.group(1).strip().lower(), match.group(2).strip()
        if key in current:
            records.append(current)
            current = {}
        current[key] = value
        last_key = "mgmt" if key.startswith("management address") else ""
    if current:
        records.append(current)
    return records


def parse_column_records(text: str) -> List[Dict[str, str]]:
    """Whitespace columns under a header line, sliced by header position."""
    lines = [l for l in text.splitlines() if l.strip()]
    header_at = None
    for i, line in enumerate(lines):
        low = line.lower()
        if ("local" in low and ("chassis" in low or "port" in low or "neighbor" in low)) \
                or ("interface" in low and "neighbor" in low):
            header_at = i
            break
    if header_at is None:
        return []
    header = lines[header_at]
    labels = [(m.start(), m.group(0).strip().lower())
              for m in re.finditer(r"\S+(?: \S+)*?(?=\s{2,}|$)", header)]
    records = []
    for line in lines[header_at + 1:]:
        if set(line.strip()) <= {"-", "="} or not line.strip():
            continue
        record = {}
        for pos, (start, label) in enumerate(labels):
            end = labels[pos + 1][0] if pos + 1 < len(labels) else None
            record[label] = line[start:end].strip()
        if any(record.values()):
            records.append(record)
    return records


def parse_mikrotik_records(text: str) -> List[Dict[str, str]]:
    """RouterOS prints key=value, a record per leading index number."""
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("Flags:", "Columns:")):
            continue
        if re.match(r"^\d+\s", line) or re.match(r"^\d+$", line):
            if current:
                records.append(current)
            current = {}
            line = re.sub(r"^\d+\s*", "", line)
        for key, value in re.findall(r'([a-z0-9-]+)=("(?:[^"]*)"|\S+)', line):
            current[key] = value.strip('"')
    if current:
        records.append(current)
    return records


def records_to_neighbours(records: List[Dict[str, str]], fields: Dict[str, tuple],
                          switch_host: str, source: str, proto: str = "LLDP") -> List[Neighbor]:
    out = []
    for record in records:
        local = _pick(record, fields.get("local", ())).split(",")[0].strip()
        name = _pick(record, fields.get("name", ()))
        port = _pick(record, fields.get("port", ()))
        if not (name or port):
            continue
        nb = Neighbor(
            host=switch_host, local_iface=local or "?", local_role="switch-port",
            switch=name or "?", port=port, proto=proto, source=source,
            port_desc=_pick(record, fields.get("desc", ())),
            sys_desc=" ".join(_pick(record, fields.get("sysdesc", ())).split()),
            mgmt_ip=_pick_ip(record, fields.get("mgmt", ())),
            vlan=_pick(record, fields.get("vlan", ())),
            chassis_id=_pick(record, fields.get("chassis", ())),
            capabilities=_pick(record, fields.get("caps", ())),
            note="seen by this switch on its own port %s" % (local or "?"))
        out.append(nb)
    return out


def parse_neighbours(text: str, dialect: Dialect, switch_host: str,
                     source: str) -> List[Neighbor]:
    proto = "CDP" if "cdp" in source else ("MNDP/LLDP" if dialect.name == "mikrotik" else "LLDP")
    if dialect.style == "mikrotik":
        records = parse_mikrotik_records(text)
    elif dialect.style == "columns":
        records = parse_column_records(text)
    else:
        records = parse_block_records(text)
        if not records:
            records = parse_column_records(text)
    return records_to_neighbours(records, dialect.fields, switch_host, source, proto)


# --------------------------------------------------------------- MAC table

def line_has_mac(line: str, variants: Dict[str, str]) -> bool:
    low = line.lower()
    return any(v in low for v in variants.values())


def port_from_line(line: str) -> Tuple[str, str]:
    tokens = [t for t in re.split(r"[\s,]+", line.strip()) if t]
    vlan = ""
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= 4094:
            vlan = token
            break
    for token in reversed(tokens):
        low = token.lower().strip("()[]")
        if not low or low in NOISE or low.isdigit():
            continue
        if re.search(r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}", low):
            continue
        if re.search(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", low):
            continue
        return token, vlan
    return "", vlan


# --------------------------------------------------------------- entry points

def _login(host: str, creds: Creds) -> Tuple[Optional[Ssh], str]:
    try:
        ssh = Ssh(host, creds)
        ssh.connect()
        return ssh, ""
    except SshError as exc:
        return None, str(exc)


def identify(ssh: Ssh) -> Tuple[Dialect, str, str]:
    """Try each family's version command until one answers. Returns dialect, name, raw."""
    for command in ("show version", "/system/resource/print", "display version",
                    "show system information"):
        _rc, out, _err = ssh.run(command, timeout=30)
        if _is_error(out):
            continue
        family = detect_family(out)
        name = ""
        if family == "mikrotik":
            _rc, ident, _err = ssh.run("/system/identity/print", timeout=20)
            found = re.search(r'name\s*[:=]\s*"?([^"\r\n]+)', ident)
            if found:
                name = found.group(1).strip()
        for line in ([] if name else out.splitlines()):
            found = re.search(r"^\s*(?:hostname|System Name|board-name)\s*[: ]\s*(\S+)", line, re.I)
            if found:
                name = found.group(1)
                break
        if family != "unknown" or out.strip():
            return dialect_for(family), name, out
    return GENERIC, "", ""


def _read_routeros_hardware(ssh: Ssh, result: TargetResult,
                            say: Callable[[str], None]) -> None:
    """Transceivers, temperatures, fans and power supplies off a RouterOS box.

    Never fatal: a neighbour table is the job, and losing a map because a
    health reading would not parse would be a poor trade.
    """
    from . import hardware, routeros

    try:
        found = routeros.collect(lambda cmd: ssh.run(cmd, timeout=90)[1], say)
    except Exception as problem:
        say("  switch hardware not read: %s" % problem)
        return
    for key in ("optics", "ports", "health"):
        if found.get(key):
            result.extra[key] = found[key]
    if found.get("hardware"):
        hardware.attach(result, found["hardware"], say)


def probe_switch_as_target(host: str, creds: Creds, result: TargetResult,
                           log: Optional[Callable[[str], None]] = None,
                           ssh: Optional[Ssh] = None) -> TargetResult:
    """Point at a switch: list everything it sees on its own ports."""
    say = log or (lambda _m: None)
    started = time.time()
    borrowed = ssh is not None
    if ssh is None:
        ssh, error = _login(host, creds)
        if ssh is None:
            result.error = "switch CLI: %s" % error
            result.attempts.append(ProbeAttempt("switch-cli", False, error,
                                                round(time.time() - started, 1)))
            return result
    try:
        dialect, sysname, raw = identify(ssh)
        result.kind = "switch:%s" % dialect.name
        result.device_name = sysname
        result.device_descr = " ".join(raw.split())[:160]
        say("  %s looks like %s%s" % (host, dialect.name,
                                      " (%s)" % sysname if sysname else ""))
        found: List[Neighbor] = []
        for command in dialect.neighbour_cmds:
            _rc, out, _err = ssh.run(command, timeout=60)
            if _is_error(out):
                continue
            rows = parse_neighbours(out, dialect, host, "switch-cli:%s" % command.split()[0])
            say("  %-44s %d neighbour(s)" % (command, len(rows)))
            found.extend(rows)
        if not found and dialect.name == "unknown":
            for command in GENERIC.neighbour_cmds:
                _rc, out, _err = ssh.run(command, timeout=60)
                if _is_error(out):
                    continue
                found.extend(parse_neighbours(out, GENERIC, host, "switch-cli"))
        seen = set()
        for nb in found:
            key = (nb.local_iface, nb.switch, nb.port)
            if key in seen:
                continue
            seen.add(key)
            result.add(nb)
        result.attempts.append(ProbeAttempt("switch-cli-neighbours", bool(found),
                                            "%d neighbour(s)" % len(result.neighbors),
                                            round(time.time() - started, 1)))
        # A switch knows its own transceivers, temperatures and power supplies,
        # and was never asked. Only RouterOS can be asked this way so far; the
        # other dialects keep the behaviour they had.
        if dialect.name == "mikrotik":
            _read_routeros_hardware(ssh, result, say)
        if not result.neighbors and not result.error:
            result.error = ("logged into %s (%s) but could not read a neighbour table - "
                            "LLDP/CDP may be off on it" % (host, dialect.name))
        return result
    finally:
        if not borrowed:
            ssh.close()


def probe_switch_cli(host: str, creds: Creds, mac: str, result: TargetResult,
                     target_ip: str = "", log: Optional[Callable[[str], None]] = None) -> TargetResult:
    """Ask a switch over SSH which of its ports a MAC sits on."""
    say = log or (lambda _m: None)
    started = time.time()
    ssh, error = _login(host, creds)
    if ssh is None:
        result.error = "switch CLI: %s" % error
        result.attempts.append(ProbeAttempt("switch-cli", False, error,
                                            round(time.time() - started, 1)))
        return result
    try:
        dialect, sysname, _raw = identify(ssh)
        say("  switch %s looks like %s" % (host, dialect.name))
        variants = mac_variants(mac)
        hits = 0
        commands = [dialect.mac_cmd] + [d.mac_cmd for d in DIALECTS if d is not dialect]
        for template in commands:
            try:
                command = template.format(**variants)
            except KeyError:
                continue
            _rc, out, _err = ssh.run(command, timeout=40)
            if _is_error(out):
                continue
            for line in out.splitlines():
                if not line_has_mac(line, variants):
                    continue
                port, vlan = port_from_line(line)
                if not port:
                    continue
                result.add(Neighbor(
                    host=target_ip or mac, local_mac=mac, switch=sysname or host, port=port,
                    vlan=vlan, mgmt_ip=host, proto="FDB", source="switch-cli:%s" % host,
                    note="from `%s`" % command))
                hits += 1
            if hits:
                break
        result.attempts.append(ProbeAttempt("switch-cli", bool(hits),
                                            "%d line(s) matched %s" % (hits, mac),
                                            round(time.time() - started, 1)))
        if not hits and not result.error:
            result.error = "MAC %s not found in the forwarding table of %s" % (mac, host)
        return result
    finally:
        ssh.close()

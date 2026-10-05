"""Switch-side discovery over SNMP.

This is the path that works for gear you cannot log into - RUs, PDUs, UPSs,
appliances, anything. Two questions are asked of the switch: "who are your LLDP
neighbours" and "which port is this MAC on", with uplink ports filtered out so
the answer is the access port the device is actually plugged into.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import socket
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from .model import Neighbor, ProbeAttempt, TargetResult
from .proc import run_quiet
from .snmp import SnmpClient, SnmpError, as_mac, as_text

OID_SYSNAME = "1.3.6.1.2.1.1.5.0"
OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_IFNAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IFALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_IFDESCR = "1.3.6.1.2.1.2.2.1.2"
OID_BASEPORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
OID_DOT1D_FDB = "1.3.6.1.2.1.17.4.3.1.2"
OID_DOT1Q_FDB = "1.3.6.1.2.1.17.7.1.2.2.1.2"
OID_ARP = "1.3.6.1.2.1.4.22.1.2"
OID_ARP_NEW = "1.3.6.1.2.1.4.35.1.4"
OID_LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
OID_LLDP_LOC_CHASSIS = "1.0.8802.1.1.2.1.3.2.0"
OID_LLDP_REM_MANADDR = "1.0.8802.1.1.2.1.4.2.1.3"
OID_LLDP_LOC_PORTID = "1.0.8802.1.1.2.1.3.7.1.3"
OID_LLDP_LOC_PORTDESC = "1.0.8802.1.1.2.1.3.7.1.4"
OID_CDP_DEVICE = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
OID_CDP_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
OID_CDP_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"
OID_VTP_VLAN_STATE = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"

UPLINK_MAC_THRESHOLD = 4


def mac_to_oid(mac: str) -> str:
    parts = mac.split(":")
    return ".".join(str(int(p, 16)) for p in parts)


def oid_to_mac(tail: str) -> str:
    arcs = tail.split(".")
    if len(arcs) < 6:
        return ""
    return ":".join("%02x" % int(a) for a in arcs[-6:])


SUBTYPE_MAC_CHASSIS = 4
SUBTYPE_MAC_PORT = 3


def _id_text(value, subtype=None, mac_subtype=None) -> str:
    """A six-byte id is only a MAC when the subtype says so: an interface name
    like "1/1/28" is also six bytes and would otherwise print as a MAC."""
    if not isinstance(value, bytes):
        return as_text(value)
    if subtype is not None and mac_subtype is not None:
        try:
            return as_mac(value) if int(subtype) == mac_subtype else as_text(value)
        except (TypeError, ValueError):
            pass
    printable = value and all(32 <= b < 127 for b in value)
    if len(value) == 6 and not printable:
        return as_mac(value)
    return as_text(value)


LLDP_CAP_BITS = [(0x80, "Other"), (0x40, "Repeater"), (0x20, "Bridge"), (0x10, "WLAN-AP"),
                 (0x08, "Router"), (0x04, "Telephone"), (0x02, "DOCSIS"), (0x01, "Station")]


def _cap_text(value) -> str:
    if not isinstance(value, bytes) or not value:
        return ""
    return ",".join(name for bit, name in LLDP_CAP_BITS if value[0] & bit)


class Switch:
    """Cached SNMP view of one switch."""

    def __init__(self, host: str, community: str = "public", timeout: float = 2.0):
        self.host = host
        self.community = community
        self.client = SnmpClient(host, community, timeout=timeout)
        self._if_name: Optional[Dict[int, str]] = None
        self._bridge_map: Optional[Dict[int, int]] = None
        self._fdb_cache: Optional[Dict[str, Tuple[int, str]]] = None
        self.name = ""
        self.descr = ""

    def identify(self) -> bool:
        try:
            self.name = as_text(self.client.get_one(OID_SYSNAME)) or self.host
            self.descr = " ".join(as_text(self.client.get_one(OID_SYSDESCR)).split())
            return True
        except SnmpError:
            return False

    def local_chassis_id(self) -> str:
        """lldpLocChassisId - the switch's own identity, best key for the graph."""
        try:
            return _id_text(self.client.get_one(OID_LLDP_LOC_CHASSIS))
        except SnmpError:
            return ""

    def if_name(self) -> Dict[int, str]:
        if self._if_name is None:
            names: Dict[int, str] = {}
            for oid, value in self.client.walk(OID_IFNAME):
                names[int(oid.rsplit(".", 1)[-1])] = as_text(value)
            if not names:
                for oid, value in self.client.walk(OID_IFDESCR):
                    names[int(oid.rsplit(".", 1)[-1])] = as_text(value)
            self._if_name = names
        return self._if_name

    def bridge_to_ifindex(self) -> Dict[int, int]:
        if self._bridge_map is None:
            out: Dict[int, int] = {}
            try:
                for oid, value in self.client.walk(OID_BASEPORT_IFINDEX):
                    out[int(oid.rsplit(".", 1)[-1])] = int(value)
            except SnmpError:
                pass
            self._bridge_map = out
        return self._bridge_map

    def port_label(self, bridge_port: int) -> str:
        ifindex = self.bridge_to_ifindex().get(bridge_port, bridge_port)
        return self.if_name().get(ifindex, "bridge-port-%d" % bridge_port)

    def peer_mgmt_addresses(self) -> Dict[str, str]:
        """lldpRemManAddrTable keys the address into the OID index itself:
        timeMark.localPort.remIndex.addrSubtype.addrLen.<address bytes>."""
        out: Dict[str, str] = {}
        try:
            rows = list(self.client.walk(OID_LLDP_REM_MANADDR))
        except SnmpError:
            return out
        for oid, _value in rows:
            arcs = oid[len(OID_LLDP_REM_MANADDR) + 1:].split(".")
            if len(arcs) < 9:
                continue
            subtype, length = arcs[3], arcs[4]
            if subtype != "1" or length != "4":
                continue
            out[".".join(arcs[0:3])] = ".".join(arcs[5:9])
        return out

    def lldp_neighbours(self) -> List[Neighbor]:
        """Everything this switch sees, keyed by its own local port."""
        rows: Dict[str, Dict[int, object]] = {}
        try:
            for oid, value in self.client.walk(OID_LLDP_REM):
                tail = oid[len(OID_LLDP_REM) + 1:]
                arcs = tail.split(".")
                if len(arcs) < 4:
                    continue
                column = int(arcs[0])
                index = ".".join(arcs[1:])
                rows.setdefault(index, {})[column] = value
        except SnmpError:
            return []
        loc_port: Dict[str, str] = {}
        for oid_root in (OID_LLDP_LOC_PORTID, OID_LLDP_LOC_PORTDESC):
            try:
                for oid, value in self.client.walk(oid_root):
                    loc_port.setdefault(oid.rsplit(".", 1)[-1], as_text(value))
            except SnmpError:
                pass
        names = self.if_name()
        man_addr = self.peer_mgmt_addresses()
        out = []
        for index, cols in rows.items():
            arcs = index.split(".")
            local_num = arcs[1] if len(arcs) > 1 else ""
            local = loc_port.get(local_num) or names.get(int(local_num) if local_num.isdigit() else -1, "")
            nb = Neighbor(
                host=self.host, local_iface=local or ("port-%s" % local_num),
                proto="LLDP", source="snmp:%s" % self.host,
                chassis_id=_id_text(cols.get(5), cols.get(4), SUBTYPE_MAC_CHASSIS),
                port=_id_text(cols.get(7), cols.get(6), SUBTYPE_MAC_PORT),
                port_desc=as_text(cols.get(8)),
                switch=as_text(cols.get(9)),
                sys_desc=" ".join(as_text(cols.get(10)).split()),
                capabilities=_cap_text(cols.get(12)),
                mgmt_ip=man_addr.get(index, ""))
            if nb.chassis_id or nb.switch:
                out.append(nb)
        return out

    def cdp_neighbours(self) -> List[Neighbor]:
        names = self.if_name()
        device: Dict[str, str] = {}
        port: Dict[str, str] = {}
        platform: Dict[str, str] = {}
        for root, sink in ((OID_CDP_DEVICE, device), (OID_CDP_PORT, port),
                           (OID_CDP_PLATFORM, platform)):
            try:
                for oid, value in self.client.walk(root):
                    sink[oid[len(root) + 1:]] = as_text(value)
            except SnmpError:
                return []
        out = []
        for index, dev in device.items():
            ifindex = index.split(".")[0]
            local = names.get(int(ifindex)) if ifindex.isdigit() else ""
            out.append(Neighbor(
                host=self.host, local_iface=local or ("ifindex-%s" % ifindex), proto="CDP",
                source="snmp:%s" % self.host, switch=dev, chassis_id=dev,
                port=port.get(index, ""), sys_desc=platform.get(index, "")))
        return out

    def fdb(self) -> Dict[str, Tuple[int, str]]:
        """mac -> (bridge port, vlan). dot1q first, then the classic dot1d table."""
        if self._fdb_cache is not None:
            return self._fdb_cache
        table: Dict[str, Tuple[int, str]] = {}
        try:
            for oid, value in self.client.walk(OID_DOT1Q_FDB):
                tail = oid[len(OID_DOT1Q_FDB) + 1:]
                arcs = tail.split(".")
                if len(arcs) < 7:
                    continue
                table[oid_to_mac(tail)] = (int(value), arcs[0])
        except SnmpError:
            pass
        if not table:
            try:
                for oid, value in self.client.walk(OID_DOT1D_FDB):
                    table[oid_to_mac(oid[len(OID_DOT1D_FDB) + 1:])] = (int(value), "")
            except SnmpError:
                pass
        self._fdb_cache = table
        return table

    def fdb_per_vlan(self, max_vlans: int = 64) -> Dict[str, Tuple[int, str]]:
        """Cisco keeps the bridge MIB behind community@vlan indexing."""
        table: Dict[str, Tuple[int, str]] = {}
        vlans: List[str] = []
        try:
            for oid, _value in self.client.walk(OID_VTP_VLAN_STATE):
                vlans.append(oid.rsplit(".", 1)[-1])
        except SnmpError:
            return table
        for vlan in vlans[:max_vlans]:
            sub = SnmpClient(self.host, "%s@%s" % (self.community, vlan),
                             timeout=self.client.timeout, retries=0)
            try:
                for oid, value in sub.walk(OID_DOT1D_FDB, limit=2000):
                    table[oid_to_mac(oid[len(OID_DOT1D_FDB) + 1:])] = (int(value), vlan)
            except SnmpError:
                continue
        return table

    def arp(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for root in (OID_ARP, OID_ARP_NEW):
            try:
                for oid, value in self.client.walk(root):
                    arcs = oid[len(root) + 1:].split(".")
                    ip = ".".join(arcs[-4:])
                    mac = as_mac(value)
                    if ip.count(".") == 3 and len(mac) == 17:
                        out[ip] = mac
            except SnmpError:
                continue
            if out:
                break
        return out


def local_arp_mac(ip: str) -> str:
    """Last resort when no switch holds the ARP entry: ping, then read our own cache."""
    ping = ["ping", "-n", "1", "-w", "1000", ip] if sys.platform.startswith("win") \
        else ["ping", "-c", "1", "-W", "1", ip]
    run_quiet(ping, timeout=6)
    _rc, out = run_quiet(["arp", "-a", ip], timeout=6)
    for line in out.splitlines():
        if ip in line:
            found = re.search(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", line)
            if found:
                return found.group(0).lower().replace("-", ":")
    return ""


def resolve_mac(ip: str, switches: List[Switch], say: Callable[[str], None]) -> str:
    for sw in switches:
        try:
            entry = sw.arp().get(ip)
        except SnmpError:
            continue
        if entry:
            say("  MAC %s for %s came from %s ARP" % (entry, ip, sw.name or sw.host))
            return entry
    mac = local_arp_mac(ip)
    if mac:
        say("  MAC %s for %s came from the local ARP cache" % (mac, ip))
    return mac


def locate_mac(sw: Switch, mac: str) -> List[Tuple[str, str, int]]:
    """Return (port label, vlan, macs seen on that port) for every hit."""
    table = sw.fdb()
    if mac not in table:
        table.update(sw.fdb_per_vlan())
        sw._fdb_cache = table
    if mac not in table:
        return []
    per_port: Dict[int, int] = {}
    for _m, (port, _v) in table.items():
        per_port[port] = per_port.get(port, 0) + 1
    port, vlan = table[mac]
    return [(sw.port_label(port), vlan, per_port.get(port, 1))]


def probe_switch_side(target_ip: str, switches: List[Switch], result: TargetResult,
                      mac: str = "", macs: Optional[List[str]] = None,
                      learned: Optional[List[Tuple[str, str, str, Optional[bool]]]] = None,
                      log: Optional[Callable[[str], None]] = None) -> TargetResult:
    say = log or (lambda _m: None)
    started = time.time()
    live = []
    for sw in switches:
        if sw.identify():
            live.append(sw)
            say("  switch %s (%s) answered SNMP" % (sw.name or sw.host, sw.host))
            if learned is not None:
                learned.append((sw.host, sw.name, sw.descr, True))
        else:
            say("  switch %s did not answer SNMP" % sw.host)
            if learned is not None:
                learned.append((sw.host, "", "", False))
    if not live:
        result.error = "no switch answered SNMP (check --community and reachability)"
        result.attempts.append(ProbeAttempt("snmp", False, result.error, round(time.time() - started, 1)))
        return result

    wanted = [m for m in ([mac] if mac else list(macs or [])) if m]
    if not wanted:
        found_mac = resolve_mac(target_ip, live, say)
        wanted = [found_mac] if found_mac else []
    if not wanted:
        result.error = (
            "could not resolve a MAC for %s. The switches asked (%s) hold no ARP entry for "
            "it, and a routed address never appears in our own ARP cache. Either pass "
            "--mac, give SSH credentials so the MAC can be read off the target, or add the "
            "router that owns %s.0/24 with --switch so its ARP table can be read."
            % (target_ip, ", ".join(sw.host for sw in live),
               ".".join(target_ip.split(".")[:3])))
        result.attempts.append(ProbeAttempt("snmp-arp", False, result.error,
                                            round(time.time() - started, 1)))
        return result
    if len(wanted) > 1:
        say("  looking for %d MAC(s) of %s" % (len(wanted), target_ip))

    uplinks: Dict[str, set] = {}
    for sw in live:
        peers = sw.lldp_neighbours() + sw.cdp_neighbours()
        if learned is not None:
            for peer in peers:
                if peer.mgmt_ip and re.search(r"Bridge|Router", peer.capabilities or "") :
                    learned.append((peer.mgmt_ip, peer.switch, peer.sys_desc, None))
        uplinks[sw.host] = {p.local_iface for p in peers
                            if "Bridge" in (p.capabilities or "") or "Router" in (p.capabilities or "")
                            or re.search(r"switch|ios|nx-os|arista|aruba|cumulus|junos|mikrotik",
                                         p.sys_desc or "", re.I)}
        for peer in peers:
            if peer.chassis_id.lower() in wanted:
                nb = Neighbor(host=target_ip, local_iface="", switch=sw.name or sw.host,
                              port=peer.local_iface, port_desc=peer.port_desc,
                              chassis_id=peer.chassis_id, proto=peer.proto,
                              source="snmp-lldp:%s" % sw.host, mgmt_ip=sw.host,
                              sys_desc=sw.descr, confidence="high",
                              note="the device itself speaks LLDP to this switch")
                result.add(nb)

    found_any = result.resolved
    for sw in live:
      for mac in wanted:
        for label, vlan, macs_on_port in locate_mac(sw, mac):
            is_uplink = label in uplinks.get(sw.host, set()) or macs_on_port > UPLINK_MAC_THRESHOLD
            nb = Neighbor(
                host=target_ip, local_mac=mac, switch=sw.name or sw.host, port=label,
                vlan=vlan, mgmt_ip=sw.host, sys_desc=sw.descr, proto="FDB",
                source="snmp-fdb:%s" % sw.host,
                confidence="low" if is_uplink else "high",
                note=("learned via an uplink/trunk (%d MACs on this port) - the device is "
                      "further downstream" % macs_on_port) if is_uplink
                else "%d MAC(s) on this port" % macs_on_port)
            result.add(nb)
            found_any = True
            say("  %s -> %s port %s (%s)" % (mac, sw.name or sw.host, label, nb.confidence))

    result.attempts.append(ProbeAttempt("snmp-switch-side", found_any,
                                        "%d switch(es) queried" % len(live),
                                        round(time.time() - started, 1)))
    if not found_any and not result.error:
        result.error = ("none of these MACs is in the forwarding table of any switch given: %s"
                        % ", ".join(wanted))
    return result


def build_switches(specs: List[str], community: str, timeout: float = 2.0) -> List[Switch]:
    out = []
    for spec in specs:
        host, _sep, own_community = spec.partition("@")
        out.append(Switch(host.strip(), (own_community or community).strip(), timeout=timeout))
    return out

"""Binary decoders: LLDP and CDP frames, plus pcap / pcapng containers.

Every capture path (tcpdump -w, the pure-python sniffer, pktmon, pktcap-uw)
funnels raw ethernet frames in here, so there is exactly one parser to trust
instead of a pile of regexes over tcpdump text output.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Optional, Tuple

from .model import Neighbor
from .netaddr import compress_ipv6

ETH_P_LLDP = 0x88CC
ETH_P_VLAN = (0x8100, 0x88A8, 0x9100)
CDP_PROTO_ID = 0x2000

LLC_SNAP_CISCO = b"\xaa\xaa\x03\x00\x00\x0c"

CHASSIS_SUBTYPE = {
    1: "chassis-component", 2: "interface-alias", 3: "port-component",
    4: "mac", 5: "network-address", 6: "interface-name", 7: "local",
}
PORT_SUBTYPE = {
    1: "interface-alias", 2: "port-component", 3: "mac", 4: "network-address",
    5: "interface-name", 6: "agent-circuit-id", 7: "local",
}
CAP_BITS = [
    (0x01, "Other"), (0x02, "Repeater"), (0x04, "Bridge"), (0x08, "WLAN-AP"),
    (0x10, "Router"), (0x20, "Telephone"), (0x40, "DOCSIS"), (0x80, "Station"),
]

OUI_8021 = b"\x00\x80\xc2"
OUI_8023 = b"\x00\x12\x0f"

TLV_END, TLV_CHASSIS, TLV_PORT, TLV_TTL = 0, 1, 2, 3
TLV_PORT_DESC, TLV_SYS_NAME, TLV_SYS_DESC, TLV_CAPS, TLV_MGMT = 4, 5, 6, 7, 8
TLV_ORG = 127

CDP_DEVICE_ID, CDP_ADDRESSES, CDP_PORT_ID = 0x0001, 0x0002, 0x0003
CDP_VERSION, CDP_PLATFORM, CDP_NATIVE_VLAN = 0x0005, 0x0006, 0x000A


def mac_str(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw)


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip("\x00").strip()


def reachable_address(text: str) -> str:
    """A management address somebody else can use, or nothing.

    Devices announce whatever is configured, and plenty announce the
    loopback. Taken at face value that became a switch to interrogate, so
    the tool went and read SNMP from its own machine and reported "did not
    answer" about a switch that was never there.
    """
    said = str(text or "").strip()
    if not said or said in ("0.0.0.0", "::", "::1"):
        return ""
    if said.startswith("127.") or said.lower().startswith("fe80:"):
        return ""
    return said


def _ipv4(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def _ipv6(raw: bytes) -> str:
    full = ":".join("%02x%02x" % (raw[i], raw[i + 1]) for i in range(0, 16, 2))
    return compress_ipv6(full)


def _id_value(subtype: int, raw: bytes, table: Dict[int, str]) -> Tuple[str, str]:
    """Render a chassis/port id according to its subtype."""
    name = table.get(subtype, "subtype-%d" % subtype)
    if name == "mac" and len(raw) == 6:
        return mac_str(raw), name
    if name == "network-address" and raw:
        fam, addr = raw[0], raw[1:]
        if fam == 1 and len(addr) == 4:
            return _ipv4(addr), name
        if fam == 2 and len(addr) == 16:
            return _ipv6(addr), name
        return raw.hex(), name
    txt = _text(raw)
    if not txt and raw:
        return raw.hex(), name
    return txt, name


def strip_vlans(frame: bytes) -> Tuple[int, bytes, Optional[int], bytes, bytes]:
    """Return (ethertype, payload, outer_vlan, dst_mac, src_mac) after peeling VLAN tags."""
    if len(frame) < 14:
        raise ValueError("frame too short")
    dst, src = frame[0:6], frame[6:12]
    off = 12
    vlan = None
    etype = struct.unpack("!H", frame[off:off + 2])[0]
    while etype in ETH_P_VLAN and len(frame) >= off + 8:
        tci = struct.unpack("!H", frame[off + 2:off + 4])[0]
        if vlan is None:
            vlan = tci & 0x0FFF
        off += 4
        etype = struct.unpack("!H", frame[off:off + 2])[0]
    return etype, frame[off + 2:], vlan, dst, src


def is_discovery_frame(frame: bytes) -> bool:
    """Cheap pre-filter for the sniffers: LLDP, or CDP under an 802.3 length field."""
    try:
        etype, payload, _vlan, _dst, _src = strip_vlans(frame)
    except (ValueError, struct.error):
        return False
    if etype == ETH_P_LLDP:
        return True
    if etype <= 1500:
        return payload[:6] == LLC_SNAP_CISCO
    return False


def _decode_org_tlv(nb: Neighbor, val: bytes) -> None:
    oui, sub, data = val[0:3], val[3], val[4:]
    if oui == OUI_8021:
        if sub == 1 and len(data) >= 2:
            pvid = struct.unpack("!H", data[:2])[0]
            if pvid:
                nb.vlan = str(pvid)
        elif sub == 3 and len(data) >= 3:
            vid = struct.unpack("!H", data[:2])[0]
            nlen = data[2]
            nb.extra.setdefault("vlan_names", []).append("%d:%s" % (vid, _text(data[3:3 + nlen])))
    elif oui == OUI_8023:
        if sub == 4 and len(data) >= 2:
            nb.extra["max_frame"] = struct.unpack("!H", data[:2])[0]
        elif sub == 2:
            nb.extra["poe"] = data.hex()
        elif sub == 3 and len(data) >= 5:
            nb.extra["link_agg"] = struct.unpack("!I", data[1:5])[0]


def decode_lldp(payload: bytes, vlan: Optional[int] = None) -> Optional[Neighbor]:
    nb = Neighbor(proto="LLDP")
    off = 0
    seen = False
    while off + 2 <= len(payload):
        hdr = struct.unpack("!H", payload[off:off + 2])[0]
        ttype, tlen = hdr >> 9, hdr & 0x01FF
        off += 2
        val = payload[off:off + tlen]
        off += tlen
        if ttype == TLV_END or len(val) < tlen:
            break
        if ttype == TLV_CHASSIS and val:
            nb.chassis_id, nb.chassis_id_subtype = _id_value(val[0], val[1:], CHASSIS_SUBTYPE)
            seen = True
        elif ttype == TLV_PORT and val:
            nb.port, nb.port_id_subtype = _id_value(val[0], val[1:], PORT_SUBTYPE)
            seen = True
        elif ttype == TLV_PORT_DESC:
            nb.port_desc = _text(val)
        elif ttype == TLV_SYS_NAME:
            nb.switch = _text(val)
        elif ttype == TLV_SYS_DESC:
            nb.sys_desc = " ".join(_text(val).split())
        elif ttype == TLV_CAPS and len(val) >= 4:
            enabled = struct.unpack("!H", val[2:4])[0]
            nb.capabilities = ",".join(n for bit, n in CAP_BITS if enabled & bit)
        elif ttype == TLV_MGMT and val:
            alen = val[0]
            if alen and len(val) >= 1 + alen:
                sub, addr = val[1], val[2:1 + alen]
                if sub == 1 and len(addr) == 4:
                    nb.mgmt_ip = reachable_address(_ipv4(addr)) or nb.mgmt_ip
                elif sub == 2 and len(addr) == 16 and not nb.mgmt_ip:
                    nb.mgmt_ip = reachable_address(_ipv6(addr)) or nb.mgmt_ip
        elif ttype == TLV_ORG and len(val) >= 4:
            _decode_org_tlv(nb, val)
    if not seen:
        return None
    if not nb.vlan and vlan is not None:
        nb.vlan = str(vlan)
    return nb


def _cdp_addresses(val: bytes) -> List[str]:
    cnt = struct.unpack("!I", val[:4])[0]
    pos, addrs = 4, []
    for _ in range(min(cnt, 8)):
        if pos + 5 > len(val):
            break
        plen = val[pos + 1]
        pos += 2 + plen
        if pos + 2 > len(val):
            break
        alen = struct.unpack("!H", val[pos:pos + 2])[0]
        addr = val[pos + 2:pos + 2 + alen]
        pos += 2 + alen
        if alen == 4:
            addrs.append(_ipv4(addr))
    return addrs


def decode_cdp(payload: bytes) -> Optional[Neighbor]:
    """payload starts at the LLC/SNAP header."""
    if payload[:6] != LLC_SNAP_CISCO or len(payload) < 12:
        return None
    if struct.unpack("!H", payload[6:8])[0] != CDP_PROTO_ID:
        return None
    body = payload[8:]
    nb = Neighbor(proto="CDP")
    nb.extra["cdp_version"] = body[0]
    off = 4
    seen = False
    while off + 4 <= len(body):
        ttype, tlen = struct.unpack("!HH", body[off:off + 4])
        if tlen < 4:
            break
        val = body[off + 4:off + tlen]
        off += tlen
        if ttype == CDP_DEVICE_ID:
            nb.switch = nb.chassis_id = _text(val)
            seen = True
        elif ttype == CDP_PORT_ID:
            nb.port = _text(val)
            seen = True
        elif ttype == CDP_VERSION:
            nb.sys_desc = " ".join(_text(val).split())
        elif ttype == CDP_PLATFORM:
            nb.extra["platform"] = _text(val)
        elif ttype == CDP_NATIVE_VLAN and len(val) >= 2:
            nb.vlan = str(struct.unpack("!H", val[:2])[0])
        elif ttype == CDP_ADDRESSES and len(val) >= 4:
            addrs = [a for a in (_cdp_addresses(val) or [])
                     if reachable_address(a)]
            if addrs:
                nb.mgmt_ip = addrs[0]
    if not seen:
        return None
    if "platform" in nb.extra:
        nb.sys_desc = ("%s / %s" % (nb.extra["platform"], nb.sys_desc)).strip(" /")
    return nb


def decode_frame(frame: bytes) -> Optional[Neighbor]:
    try:
        etype, payload, vlan, _dst, _src = strip_vlans(frame)
    except (ValueError, struct.error):
        return None
    try:
        if etype == ETH_P_LLDP:
            return decode_lldp(payload, vlan)
        if etype <= 1500:
            nb = decode_cdp(payload)
            if nb and vlan is not None and not nb.vlan:
                nb.vlan = str(vlan)
            return nb
    except (struct.error, IndexError):
        return None
    return None


PCAP_MAGIC_LE = (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
PCAP_MAGIC_BE = (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d")
PCAPNG_SHB = b"\x0a\x0d\x0d\x0a"
PCAPNG_LE_BOM = b"\x4d\x3c\x2b\x1a"
PCAPNG_EPB, PCAPNG_SPB = 0x00000006, 0x00000003


def iter_pcap(data: bytes) -> Iterator[bytes]:
    if len(data) < 24:
        return
    magic = data[:4]
    if magic in PCAP_MAGIC_LE:
        endian = "<"
    elif magic in PCAP_MAGIC_BE:
        endian = ">"
    else:
        return
    off = 24
    while off + 16 <= len(data):
        _ts, _us, incl, _orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        if incl > len(data) - off:
            break
        yield data[off:off + incl]
        off += incl


def iter_pcapng(data: bytes) -> Iterator[bytes]:
    off = 0
    endian = "<"
    while off + 12 <= len(data):
        if data[off:off + 4] == PCAPNG_SHB:
            endian = "<" if data[off + 8:off + 12] == PCAPNG_LE_BOM else ">"
        btype = struct.unpack(endian + "I", data[off:off + 4])[0]
        blen = struct.unpack(endian + "I", data[off + 4:off + 8])[0]
        if blen < 12 or off + blen > len(data):
            break
        if btype == PCAPNG_EPB and blen >= 32:
            caplen = struct.unpack(endian + "I", data[off + 20:off + 24])[0]
            yield data[off + 28:off + 28 + caplen]
        elif btype == PCAPNG_SPB and blen >= 16:
            yield data[off + 12:off + blen - 4]
        off += blen


def iter_capture(data: bytes) -> Iterator[bytes]:
    """Accept either container: tcpdump writes pcap, pktmon writes pcapng."""
    if data[:4] == PCAPNG_SHB:
        for frame in iter_pcapng(data):
            yield frame
    else:
        for frame in iter_pcap(data):
            yield frame


def neighbors_from_capture(data: bytes) -> List[Neighbor]:
    out, seen = [], set()
    for frame in iter_capture(data):
        nb = decode_frame(frame)
        if not nb:
            continue
        key = (nb.proto, nb.chassis_id, nb.port)
        if key in seen:
            continue
        seen.add(key)
        out.append(nb)
    return out

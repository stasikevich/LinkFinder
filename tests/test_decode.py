"""Offline checks for the frame/container decoders. Run: python tests/test_decode.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.decode import decode_frame, is_discovery_frame, neighbors_from_capture

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-28s %-34r %s" % (label, got, "ok" if got == want else "FAIL"))


def lldp_tlv(ttype, value):
    return struct.pack("!H", (ttype << 9) | len(value)) + value


def build_lldp():
    p = lldp_tlv(1, b"\x04" + bytes.fromhex("001122334455"))
    p += lldp_tlv(2, b"\x05" + b"1/1/5")
    p += lldp_tlv(3, struct.pack("!H", 120))
    p += lldp_tlv(4, b"GigabitEthernet1/1/5")
    p += lldp_tlv(5, b"sw-access-1")
    p += lldp_tlv(6, b"Aruba JL675A 6300M  FL.10.09")
    p += lldp_tlv(7, struct.pack("!HH", 0x0014, 0x0014))
    p += lldp_tlv(8, bytes([5, 1]) + bytes([10, 1, 2, 3]) + bytes([2]) + struct.pack("!I", 7) + bytes([0]))
    p += lldp_tlv(127, b"\x00\x80\xc2\x01" + struct.pack("!H", 100))
    p += lldp_tlv(127, b"\x00\x12\x0f\x04" + struct.pack("!H", 9216))
    p += lldp_tlv(0, b"")
    return bytes.fromhex("0180c200000e") + bytes.fromhex("aabbccddeeff") + struct.pack("!H", 0x88CC) + p


def cdp_tlv(ttype, value):
    return struct.pack("!HH", ttype, len(value) + 4) + value


def build_cdp():
    body = bytes([2, 180, 0, 0])
    body += cdp_tlv(0x0001, b"sw-core-2.lab.local")
    body += cdp_tlv(0x0003, b"TenGigabitEthernet1/0/7")
    body += cdp_tlv(0x0005, b"Cisco IOS-XE 17.9.4")
    body += cdp_tlv(0x0006, b"cisco C9300-48P")
    body += cdp_tlv(0x000A, struct.pack("!H", 401))
    snap = b"\xaa\xaa\x03\x00\x00\x0c" + struct.pack("!H", 0x2000) + body
    return bytes.fromhex("01000ccccccc") + bytes.fromhex("aabbccddeeff") + struct.pack("!H", len(snap)) + snap


def tag(frame, *vids):
    out = frame[:12]
    for vid in vids:
        out += struct.pack("!HH", 0x8100, vid)
    return out + frame[12:]


lldp = build_lldp()
nb = decode_frame(lldp)
check("lldp.switch", nb.switch, "sw-access-1")
check("lldp.port", nb.port, "1/1/5")
check("lldp.port_subtype", nb.port_id_subtype, "interface-name")
check("lldp.port_desc", nb.port_desc, "GigabitEthernet1/1/5")
check("lldp.chassis", nb.chassis_id, "00:11:22:33:44:55")
check("lldp.sys_desc", nb.sys_desc, "Aruba JL675A 6300M FL.10.09")
check("lldp.mgmt_ip", nb.mgmt_ip, "10.1.2.3")

v6_first = lldp_tlv(1, b"" + bytes.fromhex("001122334455")) + lldp_tlv(2, b"" + b"p1")
v6_first += lldp_tlv(8, bytes([17, 2]) + bytes(range(16)) + bytes([2]) + struct.pack("!I", 1) + bytes([0]))
v6_first += lldp_tlv(8, bytes([5, 1]) + bytes([10, 9, 9, 9]) + bytes([2]) + struct.pack("!I", 1) + bytes([0]))
v6_first += lldp_tlv(0, b"")
check("lldp.ipv4 beats ipv6",
      decode_frame(bytes.fromhex("0180c200000e") + bytes.fromhex("aabbccddeeff")
                   + struct.pack("!H", 0x88CC) + v6_first).mgmt_ip, "10.9.9.9")
check("lldp.vlan", nb.vlan, "100")
check("lldp.caps", nb.capabilities, "Bridge,Router")
check("lldp.max_frame", nb.extra.get("max_frame"), 9216)

q = decode_frame(tag(lldp, 3674))
check("lldp.tagged.port", q.port, "1/1/5")
check("lldp.tagged.vlan", q.vlan, "100")

qinq = decode_frame(tag(lldp, 3674, 20))
check("lldp.qinq.switch", qinq.switch, "sw-access-1")

cdp = build_cdp()
c = decode_frame(cdp)
check("cdp.switch", c.switch, "sw-core-2.lab.local")
check("cdp.port", c.port, "TenGigabitEthernet1/0/7")
check("cdp.vlan", c.vlan, "401")
check("cdp.sys_desc", c.sys_desc, "cisco C9300-48P / Cisco IOS-XE 17.9.4")
check("cdp.tagged.port", decode_frame(tag(cdp, 55)).port, "TenGigabitEthernet1/0/7")

check("prefilter.lldp", is_discovery_frame(lldp), True)
check("prefilter.cdp", is_discovery_frame(cdp), True)
check("prefilter.arp", is_discovery_frame(b"\xff" * 12 + struct.pack("!H", 0x0806) + b"x" * 28), False)
check("prefilter.short", is_discovery_frame(b"\x00" * 6), False)

pcap = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
for f in (lldp, cdp, lldp):
    pcap += struct.pack("<IIII", 0, 0, len(f), len(f)) + f
check("pcap.dedup", [(n.proto, n.port) for n in neighbors_from_capture(pcap)],
      [("LLDP", "1/1/5"), ("CDP", "TenGigabitEthernet1/0/7")])

shb = struct.pack("<II", 0x0A0D0D0A, 28) + b"\x4d\x3c\x2b\x1a" + struct.pack("<HHq", 1, 0, -1) + struct.pack("<I", 28)
idb = struct.pack("<IIHHI", 0x00000001, 20, 1, 0, 65535) + struct.pack("<I", 20)
pad = (4 - len(lldp) % 4) % 4
epb = struct.pack("<IIIIIII", 0x00000006, 32 + len(lldp) + pad, 0, 0, 0, len(lldp), len(lldp))
epb += lldp + b"\x00" * pad + struct.pack("<I", 32 + len(lldp) + pad)
check("pcapng.epb", [n.switch for n in neighbors_from_capture(shb + idb + epb)], ["sw-access-1"])

check("garbage", neighbors_from_capture(b"not a capture at all"), [])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all decode checks passed")

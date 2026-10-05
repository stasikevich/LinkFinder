"""Switch CLI parsing against real-world output from each NOS.
Run: python tests/test_switchcli.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.probe_switchcli import (detect_family, dialect_for, parse_neighbours,
                                        port_from_line, line_has_mac, mac_variants, _is_error)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-46s %s" % (label, "ok" if got == want else "FAIL"))


def rows(text, family):
    return parse_neighbours(text, dialect_for(family), "10.0.0.1", "switch-cli")


CISCO_LLDP = """Capability codes:
    (R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device
------------------------------------------------
Local Intf: Gi1/0/7
Chassis id: 0011.2233.4455
Port id: Gi0/1
Port Description: to-server-12
System Name: sw-access-1

System Description:
Cisco IOS Software, C3750E Software

Time remaining: 97 seconds
System Capabilities: B,R
Enabled Capabilities: B
Management Addresses:
    IP: 10.1.2.3
Vlan ID: 100

------------------------------------------------
Local Intf: Gi1/0/8
Chassis id: 0011.2233.4466
Port id: Gi0/2
Port Description: to-server-13
System Name: sw-access-2
Management Addresses:
    IP: 10.1.2.4
Vlan ID: 101
"""
check("cisco: family", detect_family("Cisco IOS Software, Version 15.2"), "cisco")
cisco = rows(CISCO_LLDP, "cisco")
check("cisco: two neighbours", len(cisco), 2)
check("cisco: local port", cisco[0].local_iface, "Gi1/0/7")
check("cisco: neighbour name", cisco[0].switch, "sw-access-1")
check("cisco: neighbour port", cisco[0].port, "Gi0/1")
check("cisco: port description", cisco[0].port_desc, "to-server-12")
check("cisco: management ip", cisco[0].mgmt_ip, "10.1.2.3")
check("cisco: vlan", cisco[0].vlan, "100")
check("cisco: chassis id captured", cisco[0].chassis_id, "0011.2233.4455")
check("cisco: second row", (cisco[1].local_iface, cisco[1].switch), ("Gi1/0/8", "sw-access-2"))

NXOS = """Chassis id: 00de.fb12.3456
Port id: Ethernet1/14
Local Port id: Eth1/14
Port Description: uplink-to-core
System Name: sw-core-1
System Description: Cisco Nexus Operating System (NX-OS) Software 9.3(7)
Vlan ID: 401
"""
check("nxos: family", detect_family("Cisco Nexus Operating System (NX-OS) Software"), "nxos")
nx = rows(NXOS, "nxos")
check("nxos: neighbour", (nx[0].switch, nx[0].port), ("sw-core-1", "Ethernet1/14"))

JUNIPER = """Local Interface    Parent Interface    Chassis Id          Port info          System Name
ge-0/0/1           -                   00:11:22:33:44:55   ge-0/0/9           sw-core-1
ge-0/0/2           ae0                 00:11:22:33:44:66   xe-1/0/3           sw-core-2
"""
check("juniper: family", detect_family("JUNOS Software Release [21.4R3]"), "juniper")
jun = rows(JUNIPER, "juniper")
check("juniper: two rows", len(jun), 2)
check("juniper: local port", jun[0].local_iface, "ge-0/0/1")
check("juniper: neighbour", (jun[0].switch, jun[0].port), ("sw-core-1", "ge-0/0/9"))
check("juniper: second row port", jun[1].port, "xe-1/0/3")

MIKROTIK = """Flags: X - disabled, I - invalid
 0   interface=sfp-sfpplus3,bridge1 address=10.70.129.27 address4=10.70.129.27
     mac-address=aa:bb:cc:dd:ee:11 identity="server-a" platform="Linux"
     version="6.1.0" interface-name="ens3f3np3" system-description="SUSE Linux"

 1   interface=vlan94 address=fe80::1 address4=10.78.94.20
     mac-address=AA:BB:CC:DD:EE:01 identity="sw-edge" platform="MikroTik"
     interface-name="ether5" system-caps-enabled=bridge,router
"""
check("mikrotik: family", detect_family("RouterOS 7.12 on CRS518"), "mikrotik")
mik = rows(MIKROTIK, "mikrotik")
check("mikrotik: two neighbours", len(mik), 2)
check("mikrotik: own port, list trimmed", mik[0].local_iface, "sfp-sfpplus3")
check("mikrotik: neighbour identity", mik[0].switch, "server-a")
check("mikrotik: neighbour port", mik[0].port, "ens3f3np3")
check("mikrotik: neighbour ip", mik[0].mgmt_ip, "10.70.129.27")
check("mikrotik: chassis mac captured", mik[0].chassis_id, "aa:bb:cc:dd:ee:11")
check("mikrotik: ipv6 skipped for ipv4", mik[1].mgmt_ip, "10.78.94.20")
check("mikrotik: capabilities read", mik[1].capabilities, "bridge,router")

ARUBA_CX = """Local Port  : 1/1/5
Neighbor Entries : 1
Chassis Id       : 00:11:22:33:44:55
Port Id          : 1/1/30
Port Description : to-server-oam
System Name      : sw-access-3
System Description : ArubaOS-CX GL.10.09
Management Address : 10.70.9.1
"""
check("aruba: family", detect_family("ArubaOS-CX Version GL.10.09"), "aruba-cx")
ar = rows(ARUBA_CX, "aruba-cx")
check("aruba: local port", ar[0].local_iface, "1/1/5")
check("aruba: neighbour", (ar[0].switch, ar[0].port), ("sw-access-3", "1/1/30"))
check("aruba: management ip", ar[0].mgmt_ip, "10.70.9.1")

PROCURVE = """Local Port   : 7
ChassisType  : mac-address
ChassisId    : 00 11 22 33 44 55
PortType     : local
PortId       : 12
SysName      : hp-core
System Descr : ProCurve J9728A
"""
check("procurve: family", detect_family("ProCurve J9728A Switch 2920"), "procurve")
pc = rows(PROCURVE, "procurve")
check("procurve: local port", pc[0].local_iface, "7")
check("procurve: neighbour", (pc[0].switch, pc[0].port), ("hp-core", "12"))

HUAWEI = """Local Interface        Neighbor Device        Neighbor Interface
GigabitEthernet0/0/1   sw-access-2            GigabitEthernet0/0/24
GigabitEthernet0/0/2   server-a               eth0
"""
check("huawei: family", detect_family("Huawei Versatile Routing Platform VRP (R) software"), "huawei")
hw = rows(HUAWEI, "huawei")
check("huawei: two rows", len(hw), 2)
check("huawei: neighbour", (hw[0].local_iface, hw[0].switch, hw[0].port),
      ("GigabitEthernet0/0/1", "sw-access-2", "GigabitEthernet0/0/24"))

check("unknown family", detect_family("Some Other Box 1.0"), "unknown")
check("error output detected", _is_error("% Invalid input detected at '^' marker."), True)
check("empty output is an error", _is_error("   "), True)
check("real output is not an error", _is_error("Local Intf: Gi1/0/7"), False)

variants = mac_variants("aa:bb:cc:dd:ee:01")
check("cisco mac line", port_from_line("  10    aabb.ccdd.ee01    DYNAMIC     Gi1/0/12"),
      ("Gi1/0/12", "10"))
check("aruba mac line", port_from_line("aabb-ccdd-ee01  3674     dynamic    1/1/5"),
      ("1/1/5", "3674"))
check("mac match", line_has_mac("  10 aabb.ccdd.ee01 DYNAMIC Gi1/0/12", variants), True)
check("no mac match", line_has_mac("  10 1111.2222.3333 DYNAMIC Gi1/0/12", variants), False)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all switch-cli checks passed")

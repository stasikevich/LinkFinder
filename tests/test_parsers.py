"""Offline checks for every text/JSON source we parse. Run: python tests/test_parsers.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.probe_esxi import parse_esxcli_nics, parse_network_hint
from linkfinder.probe_linux import (parse_lldpctl, parse_lldptool, parse_networkctl,
                                    prefer_lldp, _capture_script)
from linkfinder.probe_redfish import decode_hex_port, detect_vendor, hunt_lldp, neighbor_from_lldp_block
from linkfinder.probe_switchcli import detect_family, mac_variants, port_from_line, line_has_mac
from linkfinder.probe_snmp import oid_to_mac, mac_to_oid, _cap_text
from linkfinder.sshx import parse_ifaces, physical_leaves, iface_for_ip, normalise_mac

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s:\n    got  %r\n    want %r" % (label, got, want))
    print("%-34s %s" % (label, "ok" if got == want else "FAIL"))


LLDPCTL = """lldp.eno1.via=LLDP
lldp.eno1.rid=1
lldp.eno1.age=0 day, 00:03:11
lldp.eno1.chassis.mac=00:11:22:33:44:55
lldp.eno1.chassis.name=sw-access-1
lldp.eno1.chassis.descr=Aruba JL675A 6300M 24SFP+
lldp.eno1.chassis.mgmt-ip=10.70.9.1
lldp.eno1.chassis.Bridge.enabled=on
lldp.eno1.chassis.Router.enabled=on
lldp.eno1.chassis.Wlan.enabled=off
lldp.eno1.port.ifname=1/1/5
lldp.eno1.port.descr=to-bbu-01
lldp.eno1.vlan.vlan-id=3674
lldp.bond0.3674.via=LLDP
lldp.bond0.3674.chassis.mac=00:11:22:33:44:66
lldp.bond0.3674.chassis.name=sw-access-2
lldp.bond0.3674.port.local=Eth1/14
lldp.bond0.3674.port.descr=uplink
"""
neighbours = {n.local_iface: n for n in parse_lldpctl(LLDPCTL)}
check("lldpctl ifaces", sorted(neighbours), ["bond0.3674", "eno1"])
check("lldpctl switch", neighbours["eno1"].switch, "sw-access-1")
check("lldpctl port", neighbours["eno1"].port, "1/1/5")
check("lldpctl port subtype", neighbours["eno1"].port_id_subtype, "ifname")
check("lldpctl port desc", neighbours["eno1"].port_desc, "to-bbu-01")
check("lldpctl vlan", neighbours["eno1"].vlan, "3674")
check("lldpctl mgmt", neighbours["eno1"].mgmt_ip, "10.70.9.1")
check("lldpctl caps", neighbours["eno1"].capabilities, "Bridge,Router")
check("lldpctl dotted iface", neighbours["bond0.3674"].port, "Eth1/14")

LLDPTOOL = """Chassis ID TLV
\tMAC: 00:11:22:33:44:55
Port ID TLV
\tIfname: 1/1/7
Time to Live TLV
\t120
Port Description TLV
\tto-server-12
System Name TLV
\tsw-access-1
System Description TLV
\tAruba JL675A 6300M
Management Address TLV
\tIPv4: 10.70.9.1
Port VLAN ID TLV
\tPVID: 3674
"""
nb = parse_lldptool(LLDPTOOL, "eno2")
check("lldptool switch", nb.switch, "sw-access-1")
check("lldptool port", nb.port, "1/1/7")
check("lldptool desc", nb.port_desc, "to-server-12")
check("lldptool vlan", nb.vlan, "3674")
check("lldptool mgmt", nb.mgmt_ip, "10.70.9.1")
check("lldptool empty", parse_lldptool("", "eno2"), None)

NETWORKCTL = """LINK CHASSIS ID         SYSTEM NAME CAPS      PORT ID PORT DESCRIPTION
oam  00:0b:86:aa:bb:cc  sw-access-3 ..b.r.... 1/1/30  to-server-oam
eno2 00:0b:86:aa:bb:dd  sw-access-4 ..b...... Eth1/14 uplink to core

Capabilities: o - Other; p - Repeater; b - Bridge; w - WLAN Access Point; r - Router;
              t - Telephone; d - DOCSIS cable device; a - Station; c - Customer VLAN

2 neighbors listed.
"""
rows = {n.local_iface: n for n in parse_networkctl(NETWORKCTL)}
check("networkctl count", sorted(rows), ["eno2", "oam"])
check("networkctl switch", rows["oam"].switch, "sw-access-3")
check("networkctl port", rows["oam"].port, "1/1/30")
check("networkctl desc", rows["oam"].port_desc, "to-server-oam")
check("networkctl caps", rows["oam"].capabilities, "Bridge,Router")
check("networkctl chassis", rows["oam"].chassis_id, "00:0b:86:aa:bb:cc")
check("networkctl desc w/ spaces", rows["eno2"].port_desc, "uplink to core")
check("networkctl no header", parse_networkctl("nothing here"), [])

SYSFS = """IF|eno1|aa:bb:cc:dd:ee:01|up|10000|1||1|
IF|eno2|aa:bb:cc:dd:ee:02|up|10000|1|bond0|1|
IF|eno3|aa:bb:cc:dd:ee:03|up|10000|1|bond0|1|
IF|bond0|aa:bb:cc:dd:ee:02|up|20000|0||1|eno2,eno3
IF|bond0.3674|aa:bb:cc:dd:ee:02|up|-1|0||1|bond0
IF|docker0|02:42:aa:bb:cc:dd|down|-1|0||0|
ADDR|eno1|10.70.9.93/24
ADDR|bond0.3674|172.16.3.10/24
"""
ifs = parse_ifaces(SYSFS)
check("sysfs count", len(ifs), 6)
check("sysfs speed", ifs["eno1"].speed, "10000Mb/s")
check("sysfs vlan speed blank", ifs["bond0.3674"].speed, "")
check("vlan -> physical legs", physical_leaves(ifs, "bond0.3674"), ["eno2", "eno3"])
check("bond -> legs", physical_leaves(ifs, "bond0"), ["eno2", "eno3"])
check("physical stays itself", physical_leaves(ifs, "eno1"), ["eno1"])
check("iface for ip", iface_for_ip(ifs, "10.70.9.93"), "eno1")
check("iface for vlan ip", iface_for_ip(ifs, "172.16.3.10"), "bond0.3674")
check("docker is virtual", ifs["docker0"].virtual_prefix, True)

script = _capture_script(["eno1", "bond0.3674"], 65, "tcpdump")
check("capture: one per iface", script.count("tcpdump -i"), 2)
check("capture: several frames, not one", "-c 5" in script, True)
check("capture: no bare customer-bridge match", "or (ether dst 01:80:c2:00:00:00)" in script, False)
check("capture: bounded", "sleep 65" in script, True)
check("capture: cleans up", "rm -rf $d" in script, True)

HINT = """(vim.host.PhysicalNic.NetworkHint) [
   (vim.host.PhysicalNic.NetworkHint) {
      device = "vmnic0",
      connectedSwitchPort = (vim.host.PhysicalNic.CdpInfo) {
         cdpVersion = 2,
         devId = "sw-core-2.lab.local",
         address = "10.70.9.2",
         portId = "GigabitEthernet1/0/7",
         hardwarePlatform = "cisco WS-C3850-48P",
         vlan = 401,
      },
      lldpInfo = <unset>,
   }
   (vim.host.PhysicalNic.NetworkHint) {
      device = "vmnic1",
      connectedSwitchPort = <unset>,
      lldpInfo = (vim.host.PhysicalNic.LldpInfo) {
         chassisId = "00:11:22:33:44:55",
         portId = "1/1/9",
         parameter = (vim.KeyAnyValue) [
            (vim.KeyAnyValue) {
               key = "System Name",
               value = "sw-access-1",
            },
            (vim.KeyAnyValue) {
               key = "Port Description",
               value = "to-esxi-02",
            },
         ],
      },
   }
]
"""
hints = {n.local_iface: n for n in parse_network_hint(HINT)}
check("esxi vmnics", sorted(hints), ["vmnic0", "vmnic1"])
check("esxi cdp switch", hints["vmnic0"].switch, "sw-core-2.lab.local")
check("esxi cdp port", hints["vmnic0"].port, "GigabitEthernet1/0/7")
check("esxi cdp vlan", hints["vmnic0"].vlan, "401")
check("esxi lldp port", hints["vmnic1"].port, "1/1/9")
check("esxi lldp sysname", hints["vmnic1"].switch, "sw-access-1")
check("esxi lldp portdesc", hints["vmnic1"].port_desc, "to-esxi-02")

ESXCLI = """Name = vmnic0
LinkStatus = Up
MACAddress = aa:bb:cc:dd:ee:01
Speed = 10000

Name = vmnic1
LinkStatus = Down
MACAddress = aa:bb:cc:dd:ee:02
Speed = 0
"""
nics = parse_esxcli_nics(ESXCLI)
check("esxcli nics", sorted(nics), ["vmnic0", "vmnic1"])
check("esxcli link", nics["vmnic0"]["LinkStatus"], "Up")

check("hex port decode", decode_hex_port("31:2f:31:2f:35"), "1/1/5")
check("hex port passthrough", decode_hex_port("Gi1/0/7"), "Gi1/0/7")
check("hex port empty", decode_hex_port(None), "")

check("vendor supermicro", detect_vendor({"Oem": {"Supermicro": {}}}), "supermicro")
check("vendor hpe", detect_vendor({"Oem": {"Hpe": {}}}), "hpe")
check("vendor dell", detect_vendor({"Oem": {"Dell": {}}}), "dell")
check("vendor from text", detect_vendor({"Vendor": "Lenovo"}), "lenovo")
check("vendor unknown", detect_vendor({"Oem": {"Weird": {}}}), "unknown")

SUPERMICRO = {"LLDPEnabled": True,
              "LLDPReceive": {"ChassisId": "sw-access-1", "PortId": "31:2f:31:2f:35"}}
blocks = hunt_lldp(SUPERMICRO)
nb = neighbor_from_lldp_block(blocks[0])
check("supermicro switch", nb.switch, "sw-access-1")
check("supermicro port", nb.port, "1/1/5")

DELL = {"Id": "NIC.Slot.1-1", "Oem": {"Dell": {"DellSwitchConnection": {
    "SwitchConnectionID": "00:11:22:33:44:55", "SwitchPortConnectionID": "Gi1/0/12"}}}}
nb = neighbor_from_lldp_block(hunt_lldp(DELL)[0])
check("dell port", nb.port, "Gi1/0/12")
check("dell chassis", nb.chassis_id, "00:11:22:33:44:55")

STANDARD = {"Id": "1", "Ethernet": {"LLDPReceive": {
    "ChassisId": "00:aa:bb:cc:dd:ee", "PortId": "Ethernet1/14",
    "SystemName": "sw-core-1", "ManagementAddressIPv4": "10.70.9.3",
    "ManagementVlanId": 100}}}
nb = neighbor_from_lldp_block(hunt_lldp(STANDARD)[0])
check("redfish std switch", nb.switch, "sw-core-1")
check("redfish std port", nb.port, "Ethernet1/14")
check("redfish std vlan", nb.vlan, "100")

EMPTY = {"Ethernet": {"LLDPReceive": {"ChassisId": "Not Available", "PortId": None}}}
check("redfish empty link", neighbor_from_lldp_block(hunt_lldp(EMPTY)[0]), None)

PLAIN_PORT = {"Id": "1", "Name": "Ethernet Port", "PortId": "1", "PortNumber": 1,
              "LinkStatus": "LinkUp"}
HPE_PORT = {"Id": "1", "Name": "Ethernet Port 1", "LinkStatus": "NoLink", "Ethernet": {
    "AssociatedMACAddresses": ["aa:bb:cc:dd:ee:31"], "LLDPEnabled": True,
    "LLDPReceive": {"ChassisId": "", "ChassisIdSubtype": "NotTransmitted", "PortId": "",
                    "PortIdSubtype": "NotTransmitted", "ManagementVlanId": 4095},
    "LLDPTransmit": {"ChassisId": "aa:bb:cc:dd:ee:31", "ChassisIdSubtype": "MacAddr",
                     "PortId": "AA:BB:CC:DD:EE:31", "ManagementVlanId": 4095}}}
SMC_OEM = {"ChassisId": "aa:bb:cc:dd:ee:21", "PortId": "1/1/13",
           "ManagementAddressMAC": "AA:BB:CC:DD:EE:22", "ManagementAddressIPv4": "",
           "ManagementVlanId": 242}
_smc = neighbor_from_lldp_block(SMC_OEM)
check("supermicro port read", _smc.port, "1/1/13")
check("a MAC never becomes the switch IP", _smc.mgmt_ip, "")
check("and it is explained in the note", "only a MAC" in _smc.note, True)
check("the MAC is still kept", _smc.extra.get("mgmt_mac"), "AA:BB:CC:DD:EE:22")
SMC_OEM_IP = dict(SMC_OEM, ManagementAddressIPv4="10.71.243.1")
check("a real IPv4 is taken", neighbor_from_lldp_block(SMC_OEM_IP).mgmt_ip, "10.71.243.1")

check("redfish ignores our own LLDPTransmit",
      [neighbor_from_lldp_block(b) for b in hunt_lldp(HPE_PORT)], [None])

check("redfish plain port ignored",
      [neighbor_from_lldp_block(b) for b in hunt_lldp(PLAIN_PORT)], [None])

check("family nxos", detect_family("Cisco Nexus Operating System (NX-OS) Software"), "nxos")
check("family arista", detect_family("Arista DCS-7050SX-64"), "arista")
check("family junos", detect_family("JUNOS Software Release [21.4R3]"), "juniper")
check("family mikrotik", detect_family("RouterOS 7.12 on CRS518"), "mikrotik")
check("family unknown", detect_family("Some Other Box 1.0"), "unknown")

variants = mac_variants("aa:bb:cc:dd:ee:01")
check("mac dotted", variants["mac"], "aabb.ccdd.ee01")
check("mac colon", variants["mac_colon"], "aa:bb:cc:dd:ee:01")

CISCO = "  10    aabb.ccdd.ee01    DYNAMIC     Gi1/0/12"
check("cisco line match", line_has_mac(CISCO, variants), True)
check("cisco port", port_from_line(CISCO), ("Gi1/0/12", "10"))
ARUBA = "aabb-ccdd-ee01  3674     dynamic    1/1/5"
check("aruba port", port_from_line(ARUBA), ("1/1/5", "3674"))
NXOS = "* 401     aabb.ccdd.ee01   dynamic  0         F    F  Eth1/14"
check("nxos port", port_from_line(NXOS), ("Eth1/14", "401"))

check("mac to oid", mac_to_oid("aa:bb:cc:dd:ee:01"), "170.187.204.221.238.1")
check("oid to mac", oid_to_mac("401.170.187.204.221.238.1"), "aa:bb:cc:dd:ee:01")
check("snmp caps bridge", _cap_text(bytes([0x28])), "Bridge,Router")
check("normalise mac dash", normalise_mac("AA-BB-CC-DD-EE-01"), "aa:bb:cc:dd:ee:01")
check("normalise mac dotted", normalise_mac("aabb.ccdd.ee01"), "aa:bb:cc:dd:ee:01")

from linkfinder.model import Neighbor

check("best_port prefers a name over an index",
      Neighbor(port="1", port_desc="10GigabitEthernet 1/1").best_port, "10GigabitEthernet 1/1")
check("best_port keeps a real port id",
      Neighbor(port="1/1/30", port_desc="1/1/30").best_port, "1/1/30")
check("best_port keeps a numeric id when the desc is numeric too",
      Neighbor(port="1", port_desc="1").best_port, "1")
check("best_port falls back to the desc", Neighbor(port="", port_desc="Eth1/14").best_port, "Eth1/14")

_lldp = Neighbor(local_iface="oam", proto="LLDP", switch="MikroTik", port="sfp-sfpplus3",
                 chassis_id="aa", source="tcpdump")
_cdp = Neighbor(local_iface="oam", proto="CDP", switch="MikroTik", port="vlan3729",
                chassis_id="aa", vlan="3729", source="tcpdump")
_cdp2 = Neighbor(local_iface="oam", proto="CDP", switch="MikroTik", port="vlan94",
                 chassis_id="aa", mgmt_ip="10.78.94.1", source="tcpdump")
_lldp.mgmt_ip = "fe80:0000:0000:0000:06f4:1cff:feeb:065d"
_merged = prefer_lldp([_cdp, _lldp, _cdp2])
check("prefer_lldp keeps one row", len(_merged), 1)
check("prefer_lldp keeps the LLDP port", _merged[0].port, "sfp-sfpplus3")
check("prefer_lldp takes the CDP vlan", _merged[0].vlan, "3729")
check("prefer_lldp records every CDP port",
      "vlan3729" in _merged[0].note and "vlan94" in _merged[0].note, True)
check("prefer_lldp takes an IPv4 over link-local", _merged[0].mgmt_ip, "10.78.94.1")
_only_cdp = prefer_lldp([Neighbor(local_iface="e0", proto="CDP", switch="s", port="p")])
check("prefer_lldp keeps a lone CDP row", _only_cdp[0].port, "p")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all parser checks passed")

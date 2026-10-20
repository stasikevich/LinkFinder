"""Checks for what is plugged into a host, and for not confusing it with a cable.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import handsets, watch

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def raises(name, call, wanted):
    try:
        call()
    except Exception as problem:
        check(name, wanted in str(problem), True)
        return
    check(name, "no error raised", "an error")


# A real dump: a root hub, a hub, a phone on a SuperSpeed port, and a console
# cable that declares USB 2.00 but is a UART and must not be read as a slow phone.
SYSFS = """DEV usb1
idVendor=1d6b
idProduct=0002
manufacturer=Linux 5.15.0 xhci-hcd
product=xHCI Host Controller
speed=480
version= 2.00
bDeviceClass=09
DEV 1-3
idVendor=05e3
idProduct=0610
manufacturer=Genesys Logic, Inc.
product=USB2.1 Hub
speed=480
version= 2.10
bDeviceClass=09
DEV 1-3.2
idVendor=2a70
idProduct=4ee7
manufacturer=OnePlus
product=CPH2583
serial=a1b2c3d4
speed=5000
version= 3.20
bDeviceClass=00
DEV 1-4
idVendor=0403
idProduct=6001
manufacturer=FTDI
product=FT232R USB UART
serial=AB0K1XYZ
speed=12
version= 2.00
bDeviceClass=00
"""

print("-- what the bus says --")
found = handsets.parse_usb(SYSFS)
check("hubs are wiring, not things plugged in", sorted(found), ["1-3.2", "1-4"])
phone = found["1-3.2"]
check("a handset is recognised by its maker", phone["kind"], "phone")
check("its negotiated rate is read", phone["speed_mbps"], 5000.0)
check("so is the version it declares", phone["version"], "3.20")
check("and its serial, which is also its adb serial", phone["serial"], "a1b2c3d4")
check("the port it sits on is kept", phone["port"], "1-3.2")
check("a UART bridge is not a handset", found["1-4"]["kind"], "serial")

print()
print("-- what Windows says, which is never a speed --")
PNP = r"""DEV USB\VID_2A70&PID_4EE7\a1b2c3d4
product=OnePlus CPH2583
maker=OnePlus
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2);ACPI(_SB_)#ACPI(PC00)#ACPI(SS02)
DEV USB\VID_0403&PID_6001\AB0K1XYZ
product=USB Serial Converter
maker=FTDI
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(5);ACPI(_SB_)#ACPI(PC00)#ACPI(HS05)
DEV USB\ROOT_HUB30\4&1a2b3c&0
product=USB Root Hub
maker=(Standard USB Host Controller)
location=PCIROOT(0)#PCI(1400)
"""
win = handsets.parse_usb_windows(PNP)
check("the root hub is left out", len(win), 2)
check("a SuperSpeed port means a USB 3 connection",
      win[r"USB\VID_2A70&PID_4EE7\a1b2c3d4"]["version"], "3.00")
check("a high-speed port means USB 2",
      win[r"USB\VID_0403&PID_6001\AB0K1XYZ"]["version"], "2.00")
check("the serial still comes out of the instance id",
      win[r"USB\VID_2A70&PID_4EE7\a1b2c3d4"]["serial"], "a1b2c3d4")
check("no speed is invented where Windows publishes none",
      win[r"USB\VID_2A70&PID_4EE7\a1b2c3d4"]["speed_mbps"], None)

print()
print("-- what adb says --")
LISTED = """List of devices attached
* daemon started successfully *
a1b2c3d4       device usb:1-3.2 product:OnePlus model:CPH2583 device:OP594DL1
9f8e7d6c       unauthorized usb:1-5
10.0.2.15:5555 offline
"""
devices = handsets.parse_adb_devices(LISTED)
check("the chatter is not a device", len(devices), 3)
check("the model is picked up", devices[0]["model"], "CPH2583")
check("an unauthorised phone is still a phone we can see",
      devices[1]["state"], "unauthorized")
check("a network transport id is a serial too", devices[2]["serial"], "10.0.2.15:5555")

print()
print("-- a serial is rejected, never repaired --")
check("an ordinary serial passes", handsets.valid_serial("a1b2c3d4"), "a1b2c3d4")
check("so does ip:port", handsets.valid_serial("10.0.2.15:5555"), "10.0.2.15:5555")
for bad in ("a1b2; rm -rf /", "a1 b2", "a1$(id)", "a1`id`", "a1\nb2", 'a1"b2'):
    raises("%r is refused outright" % bad,
           lambda b=bad: handsets.valid_serial(b), "invalid adb serial")

print()
print("-- a command must survive the host's own shell --")
check("a POSIX host gets single quotes",
      handsets.adb_shell("adb", "a1b2", "getprop"),
      "adb -s 'a1b2' shell 'getprop'")
check("a Windows host gets double quotes, because cmd.exe eats single ones",
      handsets.adb_shell("adb", "a1b2", "getprop", windows=True),
      'adb -s "a1b2" shell "getprop"')
check("an adb path with spaces is quoted",
      handsets.adb_shell(r"C:\Program Files\adb.exe", "a1b2", "getprop", windows=True),
      '"C:\\Program Files\\adb.exe" -s "a1b2" shell "getprop"')
raises("a double-quote cannot be smuggled through cmd.exe",
       lambda: handsets.adb_shell("adb", "a1b2", 'getprop "x"', windows=True),
       "double-quote")

print()
print("-- the bus and adb, joined on the serial --")
merged = handsets.merge(handsets.parse_usb(SYSFS), devices)
check("the phone on the bus got its adb state",
      merged["1-3.2"]["adb"], "device")
check("a phone adb sees but the bus does not is still reported",
      merged["adb:10.0.2.15:5555"]["not_on_bus"], True)
check("the UART was not turned into a handset by the join",
      merged["1-4"]["kind"], "serial")

print()
print("-- collecting, without being asked to touch adb --")
asked = []


def fake_run(command):
    asked.append(command)
    if "adb" in command and "devices" in command:
        return LISTED
    if "getprop" in command:
        return "[ro.product.model]: [CPH2583]\n[ro.build.version.release]: [14]\n"
    return SYSFS


quiet = []
only_usb = handsets.collect(fake_run, quiet.append)
check("reading the bus alone never mentions adb",
      any("adb" in c for c in asked), False)
check("and it still found the devices", len(only_usb), 2)

asked[:] = []
full = handsets.collect(fake_run, quiet.append, adb=True)
check("asked, it reads the model off the phone", full["1-3.2"]["model"], "CPH2583")
check("and the Android version", full["1-3.2"]["android"], "14")
check("an unauthorised phone is not interrogated further",
      any("9f8e7d6c" in c and "getprop" in c for c in asked), False)

asked[:] = []


def broken_run(command):
    raise OSError("connection closed")


check("a host that will not answer does not kill the scan",
      handsets.collect(broken_run, quiet.append), {})

print()
print("-- a lost NUC is not a pile of unplugged phones --")
NUC = {"nodes": [
    {"id": "ip:198.51.100.10", "name": "nuc1", "kind": "host", "state": "scanned",
     "mgmt_ips": ["198.51.100.10"], "extra": {"ports": {"eno1": {"state": "up"}}}},
    {"id": "chassis:usb:a1b2c3d4", "name": "CPH2583", "kind": "phone",
     "state": "scanned", "extra": {"usb": {"adb": "device"}}},
    {"id": "chassis:usb:9f8e7d6c", "name": "SM-G991B", "kind": "phone",
     "state": "scanned", "extra": {"usb": {"adb": "device"}}}],
    "edges": [
        {"id": "e1", "a": "ip:198.51.100.10", "a_iface": "1-3.2",
         "b": "chassis:usb:a1b2c3d4", "b_iface": "usb", "protos": ["usb"]},
        {"id": "e2", "a": "ip:198.51.100.10", "a_iface": "1-5",
         "b": "chassis:usb:9f8e7d6c", "b_iface": "usb", "protos": ["usb"]}]}

seen = watch.read(NUC)
owned = [s for s in seen.values() if s["kind"] == "handset"]
check("each handset is watched", len(owned), 2)
check("but counted onto the NUC it is plugged into",
      sorted({s["device"] for s in owned}), ["ip:198.51.100.10"])

LOST = {"nodes": [dict(NUC["nodes"][0], state="unreachable", error="no answer on 22")],
        "edges": []}
state = {"signals": {}, "events": []}
state, _ = watch.update(state, watch.read(NUC), now="a")
state, _ = watch.update(state, watch.read(LOST), now="b")
state, events = watch.update(state, watch.read(LOST), now="c")
check("losing the NUC is one line, not three", len(events), 1)
check("and the phones, their links and its port went with it",
      "5 thing(s) on it went out of sight" in events[0]["detail"], True)
held = {s["label"]: s["state"] for s in state["signals"].values()}
check("a phone we could not look at is unknown, not unplugged",
      held["CPH2583"], "UNKNOWN")

print()
print("-- a phone that really was unplugged, while the NUC answered --")
ONE_GONE = {"nodes": [NUC["nodes"][0], NUC["nodes"][1]],
            "edges": [NUC["edges"][0]]}
state = {"signals": {}, "events": []}
state, _ = watch.update(state, watch.read(NUC), now="a")
state, _ = watch.update(state, watch.read(ONE_GONE), now="b")
state, events = watch.update(state, watch.read(ONE_GONE), now="c")
gone = [e for e in events if e["kind"] == "handset"]
check("the one that left is reported", [e["label"] for e in gone], ["SM-G991B"])
check("as unplugged, not as unknown", gone[0]["to"], "DOWN")
check("and it says so in words", gone[0]["detail"], "it is no longer plugged in")
check("the one still there says nothing",
      any(e["label"] == "CPH2583" for e in events), False)

print()
print("-- adb losing the phone it can see --")
SULKING = {"nodes": [NUC["nodes"][0],
                     {"id": "chassis:usb:a1b2c3d4", "name": "CPH2583",
                      "kind": "phone", "state": "scanned",
                      "extra": {"usb": {"adb": "unauthorized"}}}],
           "edges": [NUC["edges"][0]]}
state = {"signals": {}, "events": []}
state, _ = watch.update(state, watch.read(ONE_GONE), now="a")
state, _ = watch.update(state, watch.read(SULKING), now="b")
state, events = watch.update(state, watch.read(SULKING), now="c")
check("a phone on the bus that adb may not talk to is degraded, not gone",
      [(e["label"], e["to"]) for e in events], [("CPH2583", "DEGRADED")])
check("and the reason is the one adb gave", events[0]["detail"], "adb says unauthorized")

print()
print("-- what a real Windows controller actually publishes --")
# Shape taken from a live lab NUC. A composite phone also appears as its own
# interfaces, Windows writes the serial in capitals while adb uses lower case,
# and the friendly name of a phone can be "USB Composite Device".
REAL_PNP = r"""DEV USB\ROOT_HUB30\4&1BF2B177&0&0
product=USB Root Hub (USB 3.0)
maker=(Standard USB HUBs)
location=PCIROOT(0)#PCI(1400)#USBROOT(0);ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPI(RHUB)
DEV USB\VID_18D1&PID_4EE9\8D8C7469
product=USB Composite Device
maker=(Standard USB Host Controller)
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(6);ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPI(RHUB)#ACPI(HS06)
DEV USB\VID_18D1&PID_4EE9&MI_00\6&1D971C48&0&0000
product=MIDI function
maker=Microsoft
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(6)#USBMI(0);ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPI(RHUB)#ACPI(HS06)#USBMI(0)
DEV USB\VID_18D1&PID_4EE9&MI_02\6&1D971C48&0&0002
product=ADB Interface
maker=WinUsb Device
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(6)#USBMI(2);ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPI(RHUB)#ACPI(HS06)#USBMI(2)
DEV USB\VID_8087&PID_0A2A\5&1D94A37C&0&3
product=Intel(R) Wireless Bluetooth(R)
maker=Intel Corporation
location=PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3);ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPI(RHUB)#ACPI(HS03)
"""
real = handsets.parse_usb_windows(REAL_PNP)
check("one phone is one device, not one per interface",
      sorted(real), [r"USB\VID_18D1&PID_4EE9\8D8C7469",
                     r"USB\VID_8087&PID_0A2A\5&1D94A37C&0&3"])
check("the phone sits on a high-speed port",
      real[r"USB\VID_18D1&PID_4EE9\8D8C7469"]["version"], "2.00")

print()
print("-- adb and Windows do not spell the serial the same way --")
PAIRED = handsets.merge(handsets.parse_usb_windows(REAL_PNP),
                        handsets.parse_adb_devices(
                            "8d8c7469\tdevice product:OnePlus model:ONEPLUS_A5000\n"))
on_bus = PAIRED[r"USB\VID_18D1&PID_4EE9\8D8C7469"]
check("a capitalised serial still matches its lower-case adb twin",
      on_bus.get("adb"), "device")
check("nothing was invented off the bus to stand beside it",
      any(k.startswith("adb:") for k in PAIRED), False)
check("and the serial adb answers to is the one kept for talking to it",
      on_bus["adb_serial"], "8d8c7469")


def win_run(command):
    if "Get-PnpDevice" in command:
        return REAL_PNP
    if "devices" in command:
        return "8d8c7469\tdevice product:OnePlus model:ONEPLUS_A5000\n"
    if "8d8c7469" in command:
        return "[ro.product.model]: [ONEPLUS A5000]\n"
    return ""


named = handsets.collect(win_run, quiet.append, windows=True, adb=True)
check("so the phone gets its real name, not 'USB Composite Device'",
      handsets.describe(named[r"USB\VID_18D1&PID_4EE9\8D8C7469"]), "ONEPLUS A5000")

print()
print("-- a maker named by the product rather than the manufacturer --")
check("Windows calls a OnePlus 'WinUsb Device' and puts the name in product",
      handsets.classify({"maker": "WinUsb Device", "product": "OnePlus"}), "phone")

print()
print("-- four identical handsets on one NUC --")
from linkfinder.crawl import Crawler, Limits
from linkfinder.neighbors import DeviceView


class Opts(object):
    workers = 1


crawler = Crawler(Opts(), Limits(max_depth=0, max_devices=5))
host = crawler.topology.add_node(mgmt_ip="198.51.100.10", name="nuc1", kind="host",
                                 state="scanned")
view = DeviceView(target="198.51.100.10")
view.extra = {"usb": {
    "1-1": {"port": "1-1", "serial": "aaa1", "model": "ONEPLUS A5000", "kind": "phone"},
    "1-2": {"port": "1-2", "serial": "bbb2", "model": "ONEPLUS A5000", "kind": "phone"},
    "1-3": {"port": "1-3", "serial": "ccc3", "model": "ONEPLUS A5010", "kind": "phone"}}}
crawler._record_peripherals(host, view)
labels = sorted(n.name for n in crawler.topology.nodes.values() if n.kind == "phone")
check("the repeated model carries its serial",
      labels, ["ONEPLUS A5000 (aaa1)", "ONEPLUS A5000 (bbb2)", "ONEPLUS A5010"])
check("each handset is its own node", len(labels), 3)
usb_edges = [e for e in crawler.topology.edges.values() if "usb" in e.protos]
check("and each hangs off the NUC on its own port", len(usb_edges), 3)
check("the link is never mistaken for a cable",
      all(e.speed == "" for e in usb_edges), True)

print()
print("-- an unplugged phone is one line, not a phone and a cable --")
state = {"signals": {}, "events": []}
state, _ = watch.update(state, watch.read(NUC), now="a")
state, _ = watch.update(state, watch.read(ONE_GONE), now="b")
state, events = watch.update(state, watch.read(ONE_GONE), now="c")
check("only the handset is announced", len(events), 1)
check("the cable to it is not a second piece of news",
      any(e["kind"] == "link" for e in events), False)
check("and the line reads as a person would say it",
      watch.describe(events), ["c  SM-G991B went down - it is no longer plugged in"])

print()
print("-- a port path is not an identity --")
# A BMC's virtual keyboard and network gadget carry no serial and sit on the
# same USB path on every board of a model. Keying on the path folded three
# servers' gadgets into one node wired to all three hosts.
two = Crawler(Opts(), Limits(max_depth=0, max_devices=5))
# Handsets that publish no serial. The rule being protected is the one that
# once folded three servers' devices into a single node: without a real
# serial a thing is only identifiable WITHIN its host. It is checked on
# phones now because only phones are drawn - a mouse is recorded, not mapped.
gadgets = {"5-1.1": {"port": "5-1.1", "serial": "", "product": "ONEPLUS A5000",
                     "maker": "OnePlus", "kind": "phone"},
           "5-1.2": {"port": "5-1.2", "serial": "", "product": "Pixel 6",
                     "maker": "Google", "kind": "phone"}}
for address, name in (("198.51.100.21", "host-a"), ("198.51.100.22", "host-b")):
    host = two.topology.add_node(mgmt_ip=address, name=name, kind="host",
                                 state="scanned")
    view = DeviceView(target=address)
    view.extra = {"usb": {k: dict(v) for k, v in gadgets.items()}}
    two._record_peripherals(host, view)

usb = [n for n in two.topology.nodes.values() if n.kind == "phone"]
check("each host keeps its own gadgets", len(usb), 4)
owners = {}
for edge in two.topology.edges.values():
    for end, other in ((edge.a, edge.b), (edge.b, edge.a)):
        if two.topology.nodes.get(end) and two.topology.nodes[end].kind == "phone":
            owners.setdefault(end, set()).add(other)
check("and none of them is wired to two hosts",
      sorted({len(v) for v in owners.values()}), [1])
check("a thing with no serial is not given a fake one",
      sorted({n.serial for n in usb}), [""])
check("its identity is scoped to the host it is plugged into",
      all("198.51.100.2" in n.chassis_id for n in usb), True)

real = Crawler(Opts(), Limits(max_depth=0, max_devices=5))
host = real.topology.add_node(mgmt_ip="198.51.100.30", name="host-c", kind="host",
                              state="scanned")
view = DeviceView(target="198.51.100.30")
view.extra = {"usb": {"1-2": {"port": "1-2", "serial": "aaa1", "model": "a phone",
                              "kind": "phone", "adb_serial": "aaa1"}}}
real._record_peripherals(host, view)
phone = [n for n in real.topology.nodes.values() if n.kind == "phone"][0]
check("while a thing that HAS a serial is still keyed by it",
      phone.chassis_id, "usb:aaa1")
check("and keeps it, so an inventory can be asked about it", phone.serial, "aaa1")


print()
print("-- the picture is the bench, not the bus --")
mixed = Crawler(Opts(), Limits(max_depth=0, max_devices=5))
_host = mixed.topology.add_node(mgmt_ip="198.51.100.30", name="nuc",
                                kind="host", state="scanned")
_view = DeviceView(target="198.51.100.30")
_view.extra = {"usb": {
    "1-1": {"port": "1-1", "serial": "", "product": "SMCI HID KM",
            "kind": "peripheral"},
    "1-2": {"port": "1-2", "serial": "", "product": "RNDIS/Ethernet Gadget",
            "kind": "peripheral"},
    "1-3": {"port": "1-3", "serial": "", "product": "FT230X Basic UART",
            "kind": "serial"},
    "1-4": {"port": "1-4", "serial": "RF8M12", "product": "SM-G998B",
            "maker": "Samsung", "kind": "phone"}}}
mixed._record_peripherals(_host, _view)
drawn = [n.kind for n in mixed.topology.nodes.values() if n.kind != "host"]
check("a keyboard, a gadget and a console cable are not drawn",
      sorted(drawn), ["phone"])
check("but the host still carries every one of them",
      len(_view.extra["usb"]), 4)

print()
print("-- what a device offers beats who made it --")
# From a live bench: a motherboard's RGB controller was drawn on the map as a
# handset, because the firm that builds it also builds phones. Only the ADB
# interface says handset; a thing offering nothing but human input is not one.
from linkfinder.handsets import classify

check("an RGB controller is a peripheral, not a phone",
      classify({"maker": "AsusTek Computer Inc.",
                "product": "AURA LED Controller",
                "interfaces": "030000"}), "peripheral")
check("a handset offering adb is a phone whoever made it",
      classify({"maker": "Nobody", "product": "thing",
                "interfaces": "ff4201,060101"}), "phone")
check("a phone maker with nothing to go on is still a phone",
      classify({"maker": "OnePlus", "product": "A5000",
                "interfaces": ""}), "phone")
check("a mouse stays a peripheral",
      classify({"maker": "Logitech", "product": "Mouse",
                "interfaces": "030102"}), "peripheral")
check("and the probe actually asks",
      "bInterfaceClass" in handsets.USB_SCRIPT, True)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all handset checks passed")

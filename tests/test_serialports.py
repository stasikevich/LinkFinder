"""Serial-port discovery: which COM / tty a host has, and which is a radio.

The point of the feature is one answer - "the radio console is on this port" -
so the tests pin down the classification (a USB-serial bridge chip is a cable
somebody plugged in; a motherboard UART and a Bluetooth link are not) and the
two parsers that feed it, on sample output shaped exactly like the hosts give.

Run: python tests/test_serialports.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import serialports as sp
from linkfinder.render_topology import device_facts

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def _attach_extra(ports):
    class _R:
        def __init__(self):
            self.extra = {}
    r = _R()
    sp.attach(r, ports, say=None)
    return r.extra


# Output shaped exactly like linkfinder/serialports.LINUX_SCRIPT produces it:
# a by-id symlink, a USB-serial bridge with its udev identity, and two of the
# motherboard's own UARTs (the ones whose /sys type is not 0).
LINUX = """BYID|usb-FTDI_FT230X_Basic_UART_SERIAL01-if00-port0|/dev/ttyUSB0
USB|/dev/ttyUSB0|0403|6015|FTDI|FT230X_Basic_UART|SERIAL01|ftdi_sio
USB|/dev/ttyACM0|10c4|ea60|Silicon_Labs|CP2102_UART_Bridge|SERIAL02|cp210x
UART|/dev/ttyS0|4
UART|/dev/ttyS4|4"""

print("-- Linux: a USB-serial cable is the radio, the motherboard UARTs are not --")
lp = sp.read_linux(lambda _cmd: LINUX)
check("every port is found", sorted(lp), ["/dev/ttyACM0", "/dev/ttyS0",
                                          "/dev/ttyS4", "/dev/ttyUSB0"])
check("the FTDI bridge is flagged as a likely radio console",
      lp["/dev/ttyUSB0"]["likely_radio"], True)
check("so is the CP210x bridge", lp["/dev/ttyACM0"]["likely_radio"], True)
check("the motherboard UART is NOT", lp["/dev/ttyS0"].get("likely_radio"), False)
check("the cable's own serial is read", lp["/dev/ttyUSB0"].get("serial"), "SERIAL01")
check("the bridge chip is named", lp["/dev/ttyUSB0"].get("bridge"), "FTDI")
check("the stable by-id name is kept",
      lp["/dev/ttyUSB0"].get("by_id"),
      "usb-FTDI_FT230X_Basic_UART_SERIAL01-if00-port0")
check("one line reads for a person",
      sp.one_line(lp["/dev/ttyUSB0"]),
      "/dev/ttyUSB0 - FTDI FT230X Basic UART serial SERIAL01 - looks like a "
      "radio console cable")
check("the summary counts them and names a radio port",
      "4 serial port(s)" in sp.summary(lp) and "/dev/ttyUSB0" in sp.summary(lp),
      True)


print()
print("-- Windows: VID/PID in the PnP DeviceID decides, Bluetooth is not a radio --")
WINDOWS = (
    r"PORT|COM3|Standard Serial over Bluetooth link (COM3)|Microsoft|"
    r"BTHENUM\{0000}_LOCALMFG&0000\7&ABCD&0&000000000000_00000000" "\n"
    r"PORT|COM7|USB Serial Port (COM7)|FTDI|FTDIBUS\VID_0403+PID_6015+SERIAL01A\0000" "\n"
    r"PORT|COM9|Silicon Labs CP210x USB to UART Bridge (COM9)|Silicon Laboratories|"
    r"USB\VID_10C4&PID_EA60\SERIAL02")
wp = sp.read_windows(lambda _cmd: WINDOWS)
check("every COM port is found", sorted(wp), ["COM3", "COM7", "COM9"])
check("the FTDI cable is a likely radio console", wp["COM7"]["likely_radio"], True)
check("the CP210x cable is too", wp["COM9"]["likely_radio"], True)
check("the Bluetooth link is NOT", wp["COM3"].get("likely_radio"), False)
check("it is marked as bluetooth, not usb-serial", wp["COM3"]["kind"], "bluetooth")
check("the VID is pulled out of the DeviceID", wp["COM7"].get("vid"), "0403")
check("the cable serial is pulled out of an FTDIBUS id",
      wp["COM7"].get("serial"), "SERIAL01A")
check("a USB\\VID id yields its serial too", wp["COM9"].get("serial"), "SERIAL02")


print()
print("-- empty and junk input is nothing, not a crash --")
check("no serial hardware -> no section", sp.read_linux(lambda _c: ""), {})
check("junk lines are ignored", sp.read_windows(lambda _c: "garbage\n\n"), {})
check("attach files nothing when there is nothing",
      ("serial" in _attach_extra({})), False)
check("attach files the ports when there are some",
      _attach_extra(lp).get("serial") is lp, True)


print()
print("-- the panel shows only the real connections: UART and radio cables --")
# A mix: two radio cables, two motherboard UARTs, and one Bluetooth COM port
# that is pure noise. The panel must keep the first four and drop the last.
mixed = dict(lp)
mixed["COM99"] = {"port": "COM99", "kind": "bluetooth", "vendor": "Microsoft",
                  "model": "Standard Serial over Bluetooth link",
                  "likely_radio": False}
node = {"name": "bench-nuc", "extra": {"serial": mixed}}
sections = {s["title"]: s["lines"] for s in device_facts(node)}
check("there is a Serial ports section", "Serial ports" in sections, True)
lines = sections.get("Serial ports", [])
check("the likely radio cable is listed first",
      lines[0].startswith("/dev/ttyUSB0") or lines[0].startswith("/dev/ttyACM0"),
      True)
check("the on-board UARTs are shown",
      sum("on-board serial port" in ln for ln in lines), 2)
check("both radio cables are shown",
      sum("looks like a radio console cable" in ln for ln in lines), 2)
check("the Bluetooth COM port is cut from the panel",
      any("Bluetooth" in ln or "COM99" in ln for ln in lines), False)
check("so the panel shows four ports, not five", len(lines), 4)


print()
print("-- active probe: drive the console, log in if asked, read its name --")
# The Linux probe must set the line raw, open it read-write (an idle radio
# prints nothing until poked), send the identity question, and read with a
# timeout. With no login given it must not type a user/password.
cmd = sp.active_linux(lambda c: c, "/dev/ttyUSB0", 115200, "", "", seconds=3)
check("the probe sets the line raw at the baud", "raw" in cmd and "115200" in cmd, True)
check("it opens the port read-write (exec 3<>)", "exec 3<>" in cmd, True)
check("it sends the identity question down the port",
      sp.PROBE_BEGIN in cmd and "hostname" in cmd and ">&3" in cmd, True)
check("it reads back with a timeout, stopping at the END marker",
      "timeout 3 awk" in cmd and sp.PROBE_END in cmd and "<&3" in cmd, True)
check("with no login it types no credentials",
      "sleep 0.7" not in cmd, True)
cmd_login = sp.active_linux(lambda c: c, "/dev/ttyUSB0", 115200,
                            "oranuser", "secret", seconds=3)
check("with a login it types the user then the password",
      "oranuser" in cmd_login and "secret" in cmd_login and "sleep 0.7" in cmd_login,
      True)

# The Windows probe must hold DTR/RTS down so opening the port does not reset a
# device wired to them, and must write the identity question.
wcmd = sp.active_windows(lambda c: c, "COM7", 115200, "", "", seconds=3)
check("the Windows probe holds DTR down", "$p.DtrEnable=$false" in wcmd, True)
check("and RTS down too", "$p.RtsEnable=$false" in wcmd, True)
check("and writes the identity question", sp.PROBE_BEGIN in wcmd, True)

# read_identity lifts the name from between the fence and drops the echoed
# command line (which carries the markers mid-line) and the shell prompt.
answer = ("root@radio:~# echo %s; hostname; echo %s\n"
          "%s\n"
          "Integrity-ME-0-B1B3B8B28n41-02-1\n"
          "%s\n"
          "root@radio:~# " % (sp.PROBE_BEGIN, sp.PROBE_END,
                              sp.PROBE_BEGIN, sp.PROBE_END))
check("the fenced name is read, not the echoed command",
      sp.read_identity(answer), "Integrity-ME-0-B1B3B8B28n41-02-1")
check("no fence, no crash - falls back to the shell prompt name",
      sp.read_identity("someradio login: \nroot@Integrity-MM-9:~# "),
      "Integrity-MM-9")
check("pure noise yields nothing", sp.read_identity("garbage \x00\x01"), "")

# capture() drives only the likely-radio ports, tries as-is then each login,
# and records the name a port answers with.
ports = {
    "/dev/ttyUSB0": {"port": "/dev/ttyUSB0", "kind": "usb-serial", "likely_radio": True},
    "/dev/ttyS0": {"port": "/dev/ttyS0", "kind": "uart", "likely_radio": False},
}
driven = []
def _probe(cmd):
    driven.append(cmd)
    return ("%s\nRadioUnit-7\n%s" % (sp.PROBE_BEGIN, sp.PROBE_END))
sp.capture(_probe, ports, platform="linux", baud=115200, seconds=3)
check("only the radio cable was driven, not the UART", len(driven), 1)
check("the radio's name is filed on the port",
      ports["/dev/ttyUSB0"].get("radio_name"), "RadioUnit-7")
check("and it is marked as having answered",
      ports["/dev/ttyUSB0"].get("answered"), True)
check("the UART was left untouched", "radio_name" in ports["/dev/ttyS0"], False)
check("the UART records no baud either", ports["/dev/ttyS0"].get("baud"), None)

# A truly silent port - nothing comes back and no login prompt - is asked ONCE
# and left alone. Spending the baked logins on a port that never asked for one
# is what made a scan slow, so it no longer happens.
tries = []
silent = {"/dev/ttyUSB0": {"port": "/dev/ttyUSB0", "kind": "usb-serial",
                           "likely_radio": True}}
sp.capture(lambda c: tries.append(c) or "", silent, platform="linux",
           baud=115200, creds=[("root", "root"), ("admin", "admin")])
check("a silent port gets no name", "radio_name" in silent["/dev/ttyUSB0"], False)
check("a silent port is asked once, not once per login", len(tries), 1)
check("but records the baud it probed at",
      silent["/dev/ttyUSB0"].get("baud"), 115200)

# A port that shows a login prompt is worth the logins: as-is, then each one.
pl_tries = []
prompted = {"/dev/ttyUSB0": {"port": "/dev/ttyUSB0", "kind": "usb-serial",
                             "likely_radio": True}}
sp.capture(lambda c: pl_tries.append(c) or "radio login: ", prompted,
           platform="linux", baud=115200,
           creds=[("root", "root"), ("admin", "admin")])
check("a login prompt spends as-is plus each login", len(pl_tries), 3)
check("and still no name when none of them answered",
      "radio_name" in prompted["/dev/ttyUSB0"], False)

# The panel names an answered radio, and says a silent port was probed.
node_heard = {"name": "r", "extra": {"serial": ports}}
pl = {s["title"]: s["lines"] for s in device_facts(node_heard)}["Serial ports"]
check("the panel names the radio that answered",
      any("radio: RadioUnit-7" in ln for ln in pl), True)
node_silent = {"name": "r", "extra": {"serial": silent}}
ps = {s["title"]: s["lines"] for s in device_facts(node_silent)}["Serial ports"]
check("a silent port says it was probed, and heard nothing",
      any("nothing printed" in ln for ln in ps), True)


print()
print("-- a rack server's serial ports are not a radio console: skip them --")
check("a rack-mount chassis type is a server",
      sp.is_server({"chassis_type": "23"}), True)
check("a blade is a server", sp.is_server({"chassis_type": "28"}), True)
check("a desktop chassis type is not", sp.is_server({"chassis_type": "3"}), False)
check("a NUC/mini chassis type is not", sp.is_server({"chassis_type": "35"}), False)
check("a PowerEdge is a server by name when no chassis type",
      sp.is_server({"product_name": "PowerEdge R740", "sys_vendor": "Dell Inc."}),
      True)
check("an OptiPlex Micro is not a server",
      sp.is_server({"product_name": "OptiPlex Micro 7010", "sys_vendor": "Dell Inc."}),
      False)
check("nothing known means not a server (so it is still probed)",
      sp.is_server({}), False)

server_ports = {"COM4": {"port": "COM4", "kind": "usb-serial", "likely_radio": True}}
server_calls = []
sp.capture(lambda c: server_calls.append(c) or "x", server_ports,
           platform="windows", baud=115200, creds=[("root", "root")],
           hardware={"chassis_type": "23"})
check("a server's console ports are never opened", len(server_calls), 0)
check("and nothing is filed on them", "radio_name" in server_ports["COM4"], False)


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all serial-port checks passed")

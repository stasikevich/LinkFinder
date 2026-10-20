"""Which serial ports a host has, and which look like a radio's console cable.

A radio on the bench is often reached over a USB-to-serial cable and nobody
remembers which port it turned into. The host already knows: the kernel names
the adapter, the bridge chip, and the cable's own serial number. This reads
that out over the same SSH session, installing nothing, and marks the USB
adapters - which is what a console cable is - apart from the motherboard's own
COM ports and a Bluetooth link, where a radio never is.

Then, when asked to, it drives those cables one at a time: it opens each
likely-radio port at 115200, logs in with the console credentials the build
carries only if the port shows a login prompt, and reads the radio's own name
off its shell until the end marker - so a port that answers is a radio for
certain, named, and returns at once. One at a time on purpose: several of these
console cables share one USB bridge and opening them together corrupts the bytes.
Only the USB-serial cables are driven; a Bluetooth port and the motherboard's
UARTs are left alone. A rack server is skipped whole: a radio console cable lives
on a bench NUC/desktop, not a server, so its ports are not worth opening.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Callable, Dict, Optional

Run = Callable[[str], str]

# A console speaks at one rate and nobody writes it down. 115200 is what a
# modern radio console runs at and is the default; at a wrong rate the probe
# reads only garbage and the port is reported as not answering, nothing worse.
DEFAULT_BAUD = 115200

# USB-to-serial bridge chips, by USB vendor id. A port behind one of these is
# a cable somebody plugged in - the usual way a radio console is reached - as
# opposed to a motherboard UART or a Bluetooth virtual port. Lower case.
BRIDGE_VENDORS = {
    "0403": "FTDI",
    "10c4": "Silicon Labs (CP210x)",
    "1a86": "QinHeng (CH340/CH341)",
    "067b": "Prolific (PL2303)",
    "2341": "Arduino",
    "239a": "Adafruit",
    "0483": "STMicroelectronics (virtual COM)",
    "1366": "SEGGER (J-Link VCOM)",
    "0d28": "ARM mbed",
}

# The Linux reader asks for exactly these, one block per device. Kept here so
# the test feeds the reader the same shape the host produces.
LINUX_SCRIPT = r'''
for by in /dev/serial/by-id/* /dev/serial/by-path/*; do
  [ -e "$by" ] || continue
  printf 'BYID|%s|%s\n' "$(basename "$by")" "$(readlink -f "$by")"
done
for d in /dev/ttyUSB* /dev/ttyACM*; do
  [ -e "$d" ] || continue
  p=$(udevadm info -q property -n "$d" 2>/dev/null)
  get() { printf '%s\n' "$p" | sed -n "s/^$1=//p" | head -1; }
  printf 'USB|%s|%s|%s|%s|%s|%s|%s\n' "$d" \
    "$(get ID_VENDOR_ID)" "$(get ID_MODEL_ID)" "$(get ID_VENDOR)" \
    "$(get ID_MODEL)" "$(get ID_SERIAL_SHORT)" "$(get ID_USB_DRIVER)"
done
for s in /sys/class/tty/ttyS*; do
  t=$(cat "$s/type" 2>/dev/null)
  [ -z "$t" ] || [ "$t" = "0" ] && continue
  printf 'UART|/dev/%s|%s\n' "$(basename "$s")" "$t"
done
'''

# A Windows PnP entry whose name ends in (COMx). DeviceID carries the identity:
# FTDIBUS\VID_0403+PID_6015+<serial>, USB\VID_10C4&PID_EA60\..., or BTHENUM\...
WINDOWS_SCRIPT = (
    "powershell -NoProfile -Command \"$ErrorActionPreference='SilentlyContinue';"
    " Get-CimInstance Win32_PnPEntity |"
    " Where-Object { $_.Name -match '\\(COM\\d+\\)' } |"
    " ForEach-Object { $c=''; if ($_.Name -match '\\((COM\\d+)\\)') { $c=$matches[1] };"
    " ('PORT|'+$c+'|'+$_.Name+'|'+$_.Manufacturer+'|'+$_.DeviceID) }\"")

_VID = re.compile(r"VID[_]?([0-9A-Fa-f]{4})")
_PID = re.compile(r"PID[_]?([0-9A-Fa-f]{4})")


def _bridge(vid: str) -> str:
    return BRIDGE_VENDORS.get((vid or "").lower(), "")


def read_linux(run: Run) -> Dict[str, Dict[str, Any]]:
    """Every serial port on a Linux host, keyed by its /dev name."""
    try:
        text = run("sh -c %s" % shlex.quote(LINUX_SCRIPT)) or ""
    except Exception:
        return {}
    by_id: Dict[str, str] = {}
    ports: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        bits = line.rstrip("\n").split("|")
        if bits[0] == "BYID" and len(bits) >= 3 and bits[2]:
            by_id.setdefault(bits[2], bits[1])          # device -> stable name
        elif bits[0] == "USB" and len(bits) >= 8:
            dev, vid, pid, vendor, model, serial, driver = bits[1:8]
            maker = _bridge(vid)
            ports[dev] = _clean({
                "port": dev, "kind": "usb-serial",
                "vendor": vendor or maker, "model": model,
                "serial": serial, "driver": driver,
                "vid": vid.lower(), "pid": pid.lower(),
                "bridge": maker, "likely_radio": bool(maker)})
        elif bits[0] == "UART" and len(bits) >= 2:
            ports[bits[1]] = _clean({
                "port": bits[1], "kind": "uart",
                "vendor": "on-board serial port", "likely_radio": False})
    for dev, rec in ports.items():
        if dev in by_id:
            rec["by_id"] = by_id[dev]
    return ports


def read_windows(run: Run) -> Dict[str, Dict[str, Any]]:
    """Every COM port on a Windows host, keyed by its COMx name."""
    try:
        text = run(WINDOWS_SCRIPT) or ""
    except Exception:
        return {}
    ports: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        bits = line.rstrip("\r\n").split("|")
        if bits[0] != "PORT" or len(bits) < 5 or not bits[1]:
            continue
        com, name, maker, devid = bits[1], bits[2], bits[3], "|".join(bits[4:])
        vid = (_VID.search(devid) or [None, ""])[1] if _VID.search(devid) else ""
        pid = (_PID.search(devid) or [None, ""])[1] if _PID.search(devid) else ""
        bridge = _bridge(vid)
        # The description a radio cable carries ("USB Serial Port") says less
        # than the chip; keep the human name but let the chip decide the guess.
        over_bt = devid.upper().startswith("BTHENUM")
        ports[com] = _clean({
            "port": com, "kind": "usb-serial" if vid else
            ("bluetooth" if over_bt else "serial"),
            "vendor": bridge or (maker or "").strip(),
            "model": _name_only(name),
            "serial": _win_serial(devid),
            "vid": vid.lower(), "pid": pid.lower(),
            "bridge": bridge,
            "likely_radio": bool(bridge)})
    return ports


def _name_only(name: str) -> str:
    # "USB Serial Port (COM7)" -> "USB Serial Port"
    return re.sub(r"\s*\(COM\d+\)\s*$", "", name or "").strip()


def _win_serial(devid: str) -> str:
    # FTDIBUS\VID_0403+PID_6015+DK0CGVVMA\0000  -> DK0CGVVMA
    m = re.search(r"PID_[0-9A-Fa-f]{4}[+&]([0-9A-Za-z]+)", devid or "")
    if m:
        return m.group(1)
    tail = (devid or "").split("\\")[-1]
    return tail if tail and "&" not in tail else ""


def _clean(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if v not in ("", None)}


def one_line(rec: Dict[str, Any]) -> str:
    """A single serial port described for a person: port, what it is, serial."""
    port = rec.get("port", "?")
    if rec.get("kind") == "uart":
        return "%s - on-board serial port (UART)" % port
    model = (rec.get("model") or "").replace("_", " ").strip()
    vendor = (rec.get("vendor") or "").strip()
    first = vendor.split()[0].lower() if vendor else ""
    if model and (not vendor or (first and first in model.lower())):
        who = model                       # the model already names the maker
    elif model and vendor:
        who = "%s %s" % (vendor, model)
    else:
        who = vendor or model or "USB serial"
    if rec.get("kind") == "bluetooth":
        return "%s - %s (Bluetooth, not a radio)" % (port, who)
    serial = rec.get("serial")
    tail = " serial %s" % serial if serial else ""
    # Answered on its console: a radio for certain, named by what it said.
    if rec.get("radio_name"):
        return "%s - %s%s - radio: %s" % (port, who, tail, rec["radio_name"])
    guess = " - looks like a radio console cable" if rec.get("likely_radio") else ""
    return "%s - %s%s%s" % (port, who, tail, guess)


def summary(ports: Dict[str, Dict[str, Any]]) -> str:
    """One log line: how many ports, and which look like a radio cable."""
    radios = [rec.get("port") for rec in ports.values() if rec.get("likely_radio")]
    if radios:
        return "%d serial port(s), %s looks like a radio console" % (
            len(ports), ", ".join(p for p in radios if p))
    return "%d serial port(s), none look like a radio console" % len(ports)


# The identity question, asked on the radio's own console shell. Fenced so the
# answer is lifted out clean of the prompt, the echoed command and any login
# noise. `hostname` on these radios is the model and position (Integrity-...).
PROBE_BEGIN = "LF_SER_8f3a"
PROBE_END = "LF_SER_END_8f3a"
IDENTITY_CMD = "echo %s; hostname; echo %s" % (PROBE_BEGIN, PROBE_END)


def _ps_lit(value: str) -> str:
    """A PowerShell single-quoted literal: safe for $ and !, only ' is doubled."""
    return "'%s'" % str(value or "").replace("'", "''")


def active_windows(run: Run, port: str, baud: int, user: str, password: str,
                   seconds: int = 3) -> str:
    """Wake a COM console, log in if it asks and a login is given, and ask it
    who it is. The console is driven, not just listened to, so an idle radio
    that would print nothing on its own still answers."""
    ms = int(seconds * 1000)
    login = ""
    if user:
        login = ("if($r0 -notmatch '[#$]\\s*$' -and $r0 -match 'ogin:'){"
                 "$p.Write(%s);$p.Write([char]13);Start-Sleep -Milliseconds 700;"
                 "$p.ReadExisting()|Out-Null;$p.Write(%s);$p.Write([char]13);"
                 "Start-Sleep -Milliseconds 1200;$p.ReadExisting()|Out-Null};"
                 % (_ps_lit(user), _ps_lit(password)))
    ps = ('powershell -NoProfile -Command "$ErrorActionPreference=\'SilentlyContinue\';'
          '$p=New-Object System.IO.Ports.SerialPort %s,%d,None,8,One;'
          '$p.DtrEnable=$false;$p.RtsEnable=$false;$p.ReadTimeout=%d;'
          'try{$p.Open();Start-Sleep -Milliseconds 200;$p.DiscardInBuffer();'
          '$p.Write([char]13);Start-Sleep -Milliseconds 400;$r0=$p.ReadExisting();'
          '%s'
          '$p.DiscardInBuffer();$p.Write(%s);$p.Write([char]13);'
          '$dl=(Get-Date).AddMilliseconds(%d);$o=\'\';'
          'do{Start-Sleep -Milliseconds 80;$o+=$p.ReadExisting()}'
          'until(($o -match %s) -or ((Get-Date) -gt $dl));$o}'
          'catch{}finally{if($p.IsOpen){$p.Close()}}"'
          % (port, int(baud), ms, login, _ps_lit(IDENTITY_CMD), ms,
             _ps_lit(PROBE_END)))
    try:
        return run(ps) or ""
    except Exception:
        return ""


def active_linux(run: Run, port: str, baud: int, user: str, password: str,
                 seconds: int = 3) -> str:
    """The Linux twin: set the line, wake it, optionally log in, ask who it is."""
    login = ""
    if user:
        login = ("printf '%%s\\r' %s >&3; sleep 0.7; printf '%%s\\r' %s >&3; sleep 1.0; "
                 % (shlex.quote(user), shlex.quote(password)))
    # Read until the END marker then stop, so a radio that answers returns at
    # once and is read whole (a fixed wait truncated slow consoles); a silent
    # port is killed at the timeout. awk prints each line and exits on the fence.
    inner = ("stty -F %s %d raw -echo -hupcl clocal -crtscts 2>/dev/null; "
             "exec 3<>%s; printf '\\r' >&3; sleep 0.4; %s"
             "printf '%%s\\r' %s >&3; timeout %d awk '/%s/{print;exit} {print}' <&3"
             % (shlex.quote(port), int(baud), shlex.quote(port), login,
                shlex.quote(IDENTITY_CMD), int(seconds), PROBE_END))
    try:
        return run("sh -c %s" % shlex.quote(inner)) or ""
    except Exception:
        return ""


def _printable(raw: str) -> str:
    return "".join(ch if (ch.isprintable() or ch in "\n\t") else " " for ch in raw or "")


def read_identity(text: str) -> str:
    """The radio's name, lifted from between the fence - the echo and the prompt
    around it are dropped. Falls back to a `root@NAME:` prompt if the fence is
    not both there (a half-answer still names the radio)."""
    clean = _printable(text)
    # The markers each on their own line - this is the ANSWER. The echoed
    # command line ("echo BEGIN; hostname; echo END") has them mid-line, so a
    # newline on each side skips it.
    m = re.search(re.escape(PROBE_BEGIN) + r"[^\S\n]*\n(.*?)\n[^\S\n]*" +
                  re.escape(PROBE_END), clean, re.S)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line and PROBE_BEGIN not in line and PROBE_END not in line:
                return line
    m = re.search(r"@([A-Za-z0-9][A-Za-z0-9._-]{2,})\s*[:\]]", clean)
    return m.group(1) if m else ""


# SMBIOS System Enclosure types. A radio console cable lives on a bench NUC,
# desktop or workstation - never a rack server - so a rack/blade chassis is left
# alone. 17 main server, 23 rack mount, 28 blade, 29 blade enclosure.
SERVER_CHASSIS_TYPES = {"17", "23", "28", "29"}
DESKTOP_CHASSIS_TYPES = {"3", "4", "5", "6", "7", "13", "15", "16", "24", "30",
                         "31", "32", "34", "35", "36"}
# Fallback when the firmware gives no chassis type: the server product lines.
SERVER_NAME_SIGNS = ("poweredge", "proliant", "thinksystem", "thinkserver",
                     "system x", "primergy", "superserver", "supermicro",
                     "cisco ucs", "ucs ", "cloudline", "rack server", "blade",
                     "altos")

# A login/username/password prompt - the only reason to spend the baked logins.
_LOGIN_PROMPT = re.compile(r"(ogin:|sername:|assword:)")


def is_server(hardware: Optional[Dict[str, Any]]) -> bool:
    """Whether this host is a rack server (so its serial ports are not a radio
    console and are not worth opening). A bench NUC/desktop/workstation is not."""
    hw = hardware or {}
    ct = str(hw.get("chassis_type") or "").split(",")[0].strip()
    if ct in SERVER_CHASSIS_TYPES:
        return True
    if ct in DESKTOP_CHASSIS_TYPES:
        return False
    text = ("%s %s" % (hw.get("product_name", ""), hw.get("sys_vendor", ""))).lower()
    return any(sign in text for sign in SERVER_NAME_SIGNS)


def _ask_port(probe, run: Run, port: str, baud: int, seconds: int, creds) -> str:
    """One console: ask as-is, and only spend the logins if it asked for one."""
    try:
        text = probe(run, port, baud, "", "", seconds)
        who = read_identity(text)
        if not who and creds and _LOGIN_PROMPT.search(_printable(text)):
            for user, password in creds:
                who = read_identity(probe(run, port, baud, user, password, seconds))
                if who:
                    break
        return who
    except Exception:
        return ""


def capture(run: Run, ports: Dict[str, Dict[str, Any]], platform: str,
            baud: int = DEFAULT_BAUD, seconds: int = 3, creds=None,
            say: Optional[Callable[[str], None]] = None,
            hardware: Optional[Dict[str, Any]] = None, cancel=None) -> None:
    """Ask each likely-radio console who it is, in place. A port that answers is
    a radio and its name is recorded; one that stays silent is left alone.

    Each port is opened read-write (an idle radio prints nothing until poked) at
    `baud`, asked once, and only offered the baked logins if it answered with a
    login prompt. The read stops at the END marker, so an answering port returns
    at once and a silent one waits the window just once. Ports are driven one at a
    time on purpose: several of these console cables share one USB bridge, and
    opening them at once corrupts the bytes. A rack server's ports are not a radio
    console, so a server is skipped whole; only USB-serial cables are ever driven,
    never a Bluetooth port or a motherboard UART where a poke disturbs a getty."""
    if is_server(hardware):
        if say:
            say("  serial: server chassis - not opening the console ports")
        return
    probe = active_linux if platform == "linux" else active_windows
    for name, rec in (ports or {}).items():
        if not isinstance(rec, dict) or not rec.get("likely_radio"):
            continue
        if cancel is not None and cancel.is_set():
            break
        rec["baud"] = int(baud)
        who = _ask_port(probe, run, rec.get("port", name), baud, seconds, creds)
        if who:
            rec["radio_name"] = who
            rec["answered"] = True
        if say:
            say("    %s @ %d: %s" % (rec.get("port", name), baud,
                                     ("radio answered: %s" % who) if who
                                     else "no answer (ignored)"))


def attach(result, ports: Dict[str, Dict[str, Any]],
           say: Optional[Callable[[str], None]] = None) -> None:
    """File the ports on the result and say what was found."""
    if not ports:
        return
    result.extra["serial"] = ports
    if say:
        say("  serial: %s" % summary(ports))

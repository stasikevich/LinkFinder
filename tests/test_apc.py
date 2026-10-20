"""A rack PDU: what it is, and what is plugged into each socket.

A network map says which switch port a machine is in. Only the PDU knows
which socket feeds it, and that is the question asked when a rack has to
be powered down or when a server is dead and nobody knows whether it has
power at all. The outlet names are whatever somebody typed when the rack
was built, and on most benches they are the only written record of the
wiring that exists.

The card has no exec channel. Handing it a command the ordinary way
returns nothing and ends the session, which is why a plain SSH probe saw
a device that logged in and said nothing at all. It is recognised before
any login from the SSH server's own name, because its server names the
library it is built on and no general-purpose machine does.

The text here is shaped exactly like a real card's and says nothing about
any real one.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.probe_apc import (banner_says_no_shell, looks_like_apc,
                                  parse_about, parse_addresses, parse_outlets,
                                  tidy_mac)
from linkfinder.render_topology import _pdu_lines

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


ABOUT = """about
E000: Success
Hardware Factory
---------------
Model Number: \t\tAP7921B
Serial Number: \t\tZZ0000000000
Hardware Revision: \tB2
Manufacture Date: \t11/09/2019
MAC Address: \t\t00 00 5E 00 53 7C
Management Uptime: \t201 Days 1 Hour 38 Minutes

Network Management Card
---------------
Model Number: \t\t0G-1238
Serial Number: \t\tZZ0000000001
Hardware Revision: \t05

APC OS(AOS)
---------------
Name: \t\t\taos
Version: \t\tv6.8.2

apc>"""

OUTLETS = """olStatus all
 1: rack A top shelf: On
 2: rack A servers: On
 3: timing source 198.51.100.11: Off
 4: (not named): On
E000: Success

apc>"""

NETSTAT = """netstat

Current IP Information:

Family mHome Type   IPAddress                           Status
IPv6   4     auto   FE80::200:5EFF:FE00:537C/64         configured
IPv4   0     dhcp   198.51.100.116/23                   configured
IPv6   0     manual ::1/128                             configured
IPv4   0     manual 127.0.0.1/32                        configured
apc>"""

print("-- it is recognised before anybody logs in --")
# Its SSH server names the library it is built on. One connection, no
# login, and a device that cannot give a shell is never asked for one.
check("an APC card is spotted from its SSH banner",
      banner_says_no_shell("SSH-2.0-cryptlib"), True)
check("an ordinary server is not",
      banner_says_no_shell("SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19"), False)
check("nor a small embedded one that does have a shell",
      banner_says_no_shell("SSH-2.0-dropbear_2020.81"), False)
check("and nothing at all is not an APC either",
      banner_says_no_shell(""), False)
check("once logged in, the prompt settles it", looks_like_apc("apc>"), True)

print()
print("-- what the card is --")
about = parse_about(ABOUT)
check("the model", about.get("model number"), "AP7921B")
# The card prints the same field names again for its management card. The
# PDU comes first and is the thing in the rack.
check("the serial of the PDU, not of the card inside it",
      about.get("serial number"), "ZZ0000000000")
check("the hardware address, in the form everything else uses",
      about.get("mac"), "00:00:5e:00:53:7c")
check("spaces, colons or dashes all read the same",
      (tidy_mac("00 00 5E 00 53 7C"), tidy_mac("00-00-5e-00-53-7c")),
      ("00:00:5e:00:53:7c", "00:00:5e:00:53:7c"))
check("and something that is not an address is refused",
      tidy_mac("not an address"), "")

print()
print("-- what is plugged into it --")
outlets = parse_outlets(OUTLETS)
check("every outlet is read", len(outlets), 4)
check("with the name somebody typed on it",
      [o["label"] for o in outlets],
      ["rack A top shelf", "rack A servers", "timing source 198.51.100.11",
       "(not named)"])
check("and whether it is on", [o["state"] for o in outlets],
      ["On", "On", "Off", "On"])
check("a label holding an address is left exactly as written",
      outlets[2]["label"], "timing source 198.51.100.11")

print()
print("-- the address it answers on --")
check("its own address is read", parse_addresses(NETSTAT), ["198.51.100.116/23"])
check("and loopback is not offered as one",
      [a for a in parse_addresses(NETSTAT) if a.startswith("127.")], [])

print()
print("-- and the panel says it plainly --")
lines = _pdu_lines({"model number": "AP7921B", "serial number": "ZZ0000000000",
                    "hardware revision": "B2", "outlets": outlets})
text = "\n".join(lines)
check("the model and serial are on the first line",
      "AP7921B" in lines[0] and "ZZ0000000000" in lines[0], True)
check("the count says how many are on", "4 outlet(s), 3 on" in text, True)
check("every outlet is listed by name",
      all(o["label"] in text for o in outlets), True)
check("nothing at all gives nothing", _pdu_lines({}), [])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all APC checks passed")

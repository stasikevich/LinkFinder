"""Reading a host's own DHCP lease file: the zero-packet way to name a device.
Run: python tests/test_dhcp.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.dhcp_leases import (collect, lease_files, parse_dnsmasq,
                                    parse_isc)
from linkfinder.model import TargetResult
from linkfinder.neighbor6 import from_lease, leases_by_mac

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


ISC = """# The format of this file is documented in the dhcpd.leases(5) manual page.
server-duid "\\000\\001";

lease 169.254.12.11 {
  starts 2 2026/10/06 07:40:39;
  ends 3 2026/10/14 07:40:39;
  cltt 2 2026/10/07 07:40:39;
  binding state active;
  next binding state free;
  hardware ethernet 00:00:5e:00:53:41;
  set vendor-class-identifier = "o-ran-ru2";
  client-hostname "radio-one";
}

lease 169.254.13.11 {
  starts 2 2026/10/06 07:40:56;
  ends 3 2026/10/14 07:40:56;
  binding state active;
  hardware ethernet 00:00:5e:00:53:77;
}
"""

print("-- an ISC dhcpd lease file --")
isc = parse_isc(ISC)
check("both clients are found", sorted(isc), ["00:00:5e:00:53:41",
                                              "00:00:5e:00:53:77"])
first = isc["00:00:5e:00:53:41"]
check("the address it was given", first["address"], "169.254.12.11")
check("the name it gave for itself", first["hostname"], "radio-one")
check("what kind of box it said it was", first["vendor"], "o-ran-ru2")
check("the lease is live", first["state"], "active")
check("and when it last renewed, not when it expires",
      first["renewed"], "2026/10/07 07:40:39")
check("a client that gave no name still counts",
      isc["00:00:5e:00:53:77"]["address"], "169.254.13.11")
check("with no name invented for it",
      "hostname" in isc["00:00:5e:00:53:77"], False)

print()
print("-- the file is appended to, so a later record wins --")
MOVED = ISC + """
lease 169.254.12.99 {
  ends 3 2026/10/20 09:00:00;
  binding state active;
  hardware ethernet 00:00:5e:00:53:41;
}
"""
check("the client is listed once", len(parse_isc(MOVED)), 2)
check("at the address it holds now",
      parse_isc(MOVED)["00:00:5e:00:53:41"]["address"], "169.254.12.99")

print()
print("-- a lease that was handed back --")
FREED = """lease 198.51.100.40 {
  ends 3 2026/09/01 09:00:00;
  binding state free;
  hardware ethernet 00:00:5e:00:53:0a;
}
"""
check("a freed lease is still reported", len(parse_isc(FREED)), 1)
check("but it says so, so nobody reads it as present",
      parse_isc(FREED)["00:00:5e:00:53:0a"]["state"], "free")

print()
print("-- rubbish in, nothing out --")
check("an empty file", parse_isc(""), {})
check("a half-written record with no MAC",
      parse_isc("lease 198.51.100.5 {\n  binding state active;\n}\n"), {})
check("text that is not a lease file at all",
      parse_isc("total 0\ndrwxr-xr-x 2 root root\n"), {})

print()
print("-- a dnsmasq lease file --")
DNSMASQ = """1791000000 00:00:5e:00:53:21 198.51.100.21 laptop-a 01:00:00:5e:00:53:21
1791000100 00:00:5e:00:53:22 198.51.100.22 * *
not a lease line
1791000200 short 198.51.100.23 x *
"""
mas = parse_dnsmasq(DNSMASQ)
check("only the real lines are taken", sorted(mas),
      ["00:00:5e:00:53:21", "00:00:5e:00:53:22"])
check("the address", mas["00:00:5e:00:53:21"]["address"], "198.51.100.21")
check("the name", mas["00:00:5e:00:53:21"]["hostname"], "laptop-a")
check("a '*' is not a hostname", "hostname" in mas["00:00:5e:00:53:22"], False)

print()
print("-- finding the file the running server actually writes --")
SEEN = {}


def fake_ps(cmd):
    SEEN[cmd] = SEEN.get(cmd, 0) + 1
    if cmd.startswith("ps "):
        return ("1271611 /usr/sbin/dhcpd -cf /etc/site/dhcpd-front.conf "
                "-lf /var/opt/site-config/front.leases\n"
                "2201 /usr/sbin/dnsmasq --dhcp-leasefile=/run/dn.leases\n")
    return ""


where = lease_files(fake_ps)
check("the -lf path of the live server comes first", where[0],
      "/var/opt/site-config/front.leases")
check("so does an --opt=value form", where[1], "/run/dn.leases")
check("the usual places are still tried after it",
      "/var/lib/dhcp/dhcpd.leases" in where, True)
check("and no path is tried twice", len(where), len(set(where)))

print()
print("-- a host that runs no DHCP server at all --")
check("nothing is reported", collect(lambda _c: ""), {})


def angry(_cmd):
    raise RuntimeError("ssh died")


check("a host that will not answer does not take the scan down",
      collect(angry), {})

print()
print("-- end to end, as the probe calls it --")


def fake_host(cmd):
    if cmd.startswith("ps "):
        return "900 /usr/sbin/dhcpd -lf /var/opt/site/front.leases\n"
    if "/var/opt/site/front.leases" in cmd:
        return ISC
    return ""


said = []
found = collect(fake_host, said.append)
check("both leases come back", len(found), 2)
check("each one remembers which file it came from",
      found["00:00:5e:00:53:41"]["source"], "/var/opt/site/front.leases")
check("and the operator is told", any("2 device(s)" in m for m in said), True)

print()
print("-- naming a device that answers nothing --")
host = TargetResult(target="198.51.100.5", kind="linux", error="")
host.extra["dhcp"] = found
index = leases_by_mac([host])
check("the run-wide index holds both", len(index), 2)
check("a named client reads as its name first",
      from_lease("00:00:5e:00:53:41", index),
      "radio-one (00:00:5e:00:53:41, leased 169.254.12.11)")
check("an unnamed one still gains the address that matters",
      from_lease("00:00:5e:00:53:77", index),
      "ICANN, IANA Department 00:00:5e:00:53:77 (leased 169.254.13.11)")
check("a MAC nobody leased to is still named by its maker",
      from_lease("00:00:5e:00:53:ff", index).startswith("ICANN"), True)
check("and one with no maker either stays unnamed",
      from_lease("02:00:5e:00:53:ff", index), "")
check("a host with no leases contributes nothing",
      leases_by_mac([TargetResult(target="198.51.100.6", kind="linux",
                                  error="")]), {})

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all dhcp lease checks passed")

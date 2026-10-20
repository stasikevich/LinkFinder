"""Checks for reading a server's management controller from inside it.

Nobody can see a BMC's address from outside until they already know it, so
it goes unchecked and NetBox drifts. The host can read it over the internal
channel, with no network in the way and no password to hold.

Built against real output from a lab server, not a guess at the format.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import ipmi
from linkfinder.render_topology import _bmc_lines

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


REAL = """IPMI|channel|default
IPMI|lan|Set in Progress         : Set Complete
IPMI|lan|Auth Type Support       : NONE MD2 MD5 PASSWORD
IPMI|lan|IP Address Source       : Static Address
IPMI|lan|IP Address              : 192.0.2.17
IPMI|lan|Subnet Mask             : 255.255.255.0
IPMI|lan|MAC Address             : 00:00:5e:00:53:1c
IPMI|lan|SNMP Community String   : public
IPMI|answers|yes
"""

print("-- the real thing, read off a lab server --")
found = ipmi.parse(REAL)
check("the address", found.get("address"), "192.0.2.17")
check("where it came from", found.get("source"), "static")
check("its hardware address", found.get("mac"), "00:00:5e:00:53:1c")
check("and whether anything is there", found.get("answers"), True)

print()
print("-- 'IP Address Source' is not the address --")
# It starts with the same words. Matching on a prefix put "Static Address"
# in the field meant for the address, which then went to NetBox as a
# disagreement about a value that was never an address at all.
check("the two are kept apart", found.get("address"), "192.0.2.17")
check("and the source is its own fact", found.get("source"), "static")

print()
print("-- a controller with nothing configured --")
blank = ipmi.parse("IPMI|lan|IP Address              : 0.0.0.0\n"
                   "IPMI|lan|IP Address Source       : DHCP Address\n")
check("an unset address is not an address", "address" in blank, False)
check("but how it was meant to get one is kept", blank.get("source"), "DHCP")

print()
print("-- what the panel says, case by case --")
real = {"address": "192.0.2.17", "source": "static", "channel": "default",
        "mac": "00:00:5e:00:53:1c", "answers": True}

agrees = " ".join(_bmc_lines(
    real, {"addresses": [{"address": "192.0.2.17", "port": "IPMI"}]}))
check("NetBox holding the same address is said so",
      "records the same" in agrees, True)

differs = " ".join(_bmc_lines(
    real, {"addresses": [{"address": "192.0.2.99", "port": "IPMI"}]}))
check("a different one is a disagreement, with both addresses",
      "192.0.2.99" in differs and "disagree" in differs, True)

# NetBox with no IPMI address of its own adds nothing: the host's address is
# shown, and the panel does not editorialise about what NetBox lacks.
empty = _bmc_lines(real, {"addresses": [{"address": "192.0.2.1", "port": "ssh"}]})
check("the host's own address is still shown",
      any("192.0.2.17" in l for l in empty), True)
check("and NetBox holding no IPMI address adds no line about it",
      any("NetBox" in l for l in empty), False)

dead = " ".join(_bmc_lines({"address": "192.0.2.9", "answers": False}, None))
check("who could not reach it is named, not just the failure",
      "the host it lives in CANNOT reach it" in dead, True)
check("and the map agreeing it is gone is said too",
      "nothing else on this map has seen it" in dead, True)

alive = " ".join(_bmc_lines({"address": "192.0.2.9", "answers": False,
                             "seen_from": "another-server"}, None))
check("a controller seen from elsewhere is reported alive",
      "so it is alive" in alive, True)
check("without pretending its own host could reach it",
      "CANNOT reach it" in alive, True)

# The tool it could not use is not an answer. With no address known from any
# source the section is empty - it never names a program to go and install.
check("a host with no address from anywhere says nothing at all",
      _bmc_lines({"tool": "absent"}, None), [])
check("but the NetBox address alone fills it",
      any("198.51.100.9" in l and "NetBox" in l for l in _bmc_lines(
          {"tool": "absent"},
          {"addresses": [{"port": "IPMI", "address": "198.51.100.9"}]})), True)
check("a host that was never asked shows no section at all",
      _bmc_lines({}, None), [])

print()
print("-- a host that cannot ping its own BMC has not proved it is dead --")
# Measured on a live server: the host could not reach its own controller,
# and a machine already on the map had that address in its neighbour cache
# the whole time. The evidence was there and nothing joined it up.
from linkfinder.ipmi import confirm_from_map
from linkfinder.topology import Topology

t = Topology(seed="192.0.2.1")
server = t.add_node(mgmt_ip="192.0.2.1", name="server", state="scanned",
                    extra={"ipmi": {"address": "192.0.2.9", "answers": False}})
t.add_node(mgmt_ip="192.0.2.2", name="witness", state="scanned",
           extra={"neighbours": {"192.0.2.9": {"mac": "00:00:5e:00:53:09",
                                               "dev": "eno1"}}})
check("the map joins it up", confirm_from_map(t), 1)
check("and names who saw it",
      t.nodes[server.id].extra["ipmi"].get("seen_from"), "witness")

quiet = Topology(seed="192.0.2.1")
quiet.add_node(mgmt_ip="192.0.2.1", name="server", state="scanned",
               extra={"ipmi": {"address": "192.0.2.9", "answers": False}})
check("nobody having seen it stays unproven either way",
      confirm_from_map(quiet), 0)

reachable = Topology(seed="192.0.2.1")
reachable.add_node(mgmt_ip="192.0.2.1", name="server", state="scanned",
                   extra={"ipmi": {"address": "192.0.2.9", "answers": True}})
reachable.add_node(mgmt_ip="192.0.2.2", name="witness", state="scanned",
                   extra={"neighbours": {"192.0.2.9": {"mac": "x", "dev": "e"}}})
check("and one already reachable needs no second opinion",
      confirm_from_map(reachable), 0)

print()
print("-- the probe never kills a scan --")
def explode(_cmd):
    raise OSError("the link went away")

check("a probe that cannot run returns nothing",
      ipmi.collect(explode), {})
check("and a host with no ipmitool, nothing else to ask, is recorded as such",
      ipmi.collect(lambda _c: "IPMI|absent"), {"tool": "absent"})

print()
print("-- reading the BMC off the kernel when ipmitool is absent --")
# The reader prints LF_IPMI lines; this is its output parsed. Proven live
# against ipmitool on a host that had both, and on a SUSE host that had
# neither ipmitool nor a way to install one.
KERNEL = ("LF_IPMI address=10.0.0.9\n"
          "LF_IPMI source=dhcp\n"
          "LF_IPMI mac=00:00:5e:00:53:b9\n")
read = ipmi.parse_lan(KERNEL)
check("the address is read", read.get("address"), "10.0.0.9")
check("so is how it gets it", read.get("source"), "dhcp")
check("and its hardware address", read.get("mac"), "00:00:5e:00:53:b9")
check("it says plainly where the number came from",
      "without ipmitool" in read.get("read_by", ""), True)
check("the device saying it has no BMC is read as nothing",
      ipmi.parse_lan("LF_IPMI nodevice"), {})
check("and an empty answer is nothing",
      ipmi.parse_lan("LF_IPMI none"), {})

# When ipmitool is absent but a host object is given, collect falls through
# to the kernel reader - but only where /dev/ipmi0 exists, so a NUC with no
# controller is never pushed a script. A fake ssh models both.
class FakeSsh:
    def __init__(self, out, has_device=True):
        self._out = out
        self._has_device = has_device
    def have(self, _tool):
        return True
    def run(self, cmd, timeout=30):
        if "/dev/ipmi0" in cmd:
            return (0, "yes" if self._has_device else "no", "")
        return (0, "", "")

import linkfinder.sshx as _sshx
_real_push = _sshx.push_and_run_python
_sshx.push_and_run_python = lambda ssh, src, args, timeout: (0, ssh._out, "")
try:
    got = ipmi.collect(lambda _c: "IPMI|absent", ssh=FakeSsh(KERNEL))
    # A box with no /dev/ipmi0 - a NUC - is not even pushed the reader.
    pushed = []
    _sshx.push_and_run_python = lambda ssh, src, args, timeout: (
        pushed.append(1), (0, ssh._out, ""))[1]
    nuc = ipmi.collect(lambda _c: "IPMI|absent",
                       ssh=FakeSsh(KERNEL, has_device=False))
finally:
    _sshx.push_and_run_python = _real_push
check("with a host to ask, no ipmitool falls through to the kernel",
      got.get("address"), "10.0.0.9")
check("a host with no /dev/ipmi0 is recorded absent, not probed",
      nuc, {"tool": "absent"})
check("and nothing was pushed to that host", pushed, [])
check("and the host whose device says no BMC still records the absence",
      ipmi.collect(lambda _c: "IPMI|absent",
                   ssh=FakeSsh("LF_IPMI nodevice")), {"tool": "absent"})

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all management-controller checks passed")

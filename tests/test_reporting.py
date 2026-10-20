"""When a host refuses us, the output has to say which host.

A run over twenty addresses that ends in one line of text - "SSH
authentication failed" - tells an engineer nothing he can act on. Worse, a
host that never produced a single port had no row anywhere in the table,
because every row is per port, so it vanished from the run entirely.

These pin both front ends: the address travels with the reason, a partial
run is not called done, and a host that got nowhere is still named.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.cli import render_summary, render_table
# These are reporting, not window code. They moved out of gui.py so a
# machine with no tkinter - every Linux server - can still use them.
from linkfinder.diagnose import failed_hosts, friendly_error, run_verdict
from linkfinder.model import Neighbor, TargetResult

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-60s %s" % (label, "ok" if got == want else "FAIL"))


REFUSED = "SSH rejected the credentials (Authentication failed.)"


def good(host):
    result = TargetResult(target=host)
    result.add(Neighbor(host=host, local_iface="eno1", switch="sw",
                        port="1/1/1", chassis_id="aa", proto="LLDP"))
    return result


print("-- every failure carries the address it belongs to --")
MIXED = [TargetResult(target="192.0.2.1", error=REFUSED), good("192.0.2.2"),
         TargetResult(target="192.0.2.3", error="no snmp response")]
check("each bad host is listed once, with its own reason",
      failed_hosts(MIXED),
      [("192.0.2.1", "SSH authentication failed"),
       ("192.0.2.3", "SNMP timeout")])
check("a host that answered is not in the list",
      any(h == "192.0.2.2" for h, _why in failed_hosts(MIXED)), False)
check("the wording stays short enough to read",
      friendly_error(REFUSED), "SSH authentication failed")

print()
print("-- the one line at the bottom --")
check("all well", run_verdict(7, []), ("Done - 7 link(s) resolved", "UP"))
check("nothing found and nothing broken",
      run_verdict(0, []), ("Nothing resolved", "DOWN"))
check("a single failure names the host, not just the reason",
      run_verdict(0, [("192.0.2.1", "SSH authentication failed")]),
      ("192.0.2.1: SSH authentication failed", "DOWN"))
many = run_verdict(0, failed_hosts(MIXED))
check("several failures say how many and send you to the log",
      "2 host(s) failed" in many[0] and "see the log" in many[0], True)
check("and still name the reasons", "SSH authentication failed" in many[0], True)

print()
print("-- a run that half worked is not called done --")
part = run_verdict(7, failed_hosts(MIXED))
check("it says both halves", "7 link(s)" in part[0] and "2 host(s) failed" in part[0], True)
check("and it is not green", part[1], "DEGRADED")

print()
print("-- a host that never got a port is still named --")
DEAD = [TargetResult(target="192.0.2.1", error=REFUSED)]
check("the table has no row for it, because rows are per port",
      render_table(DEAD), "")
check("so the summary has to carry it", "192.0.2.1" in render_summary(DEAD), True)
check("with the reason beside it", REFUSED in render_summary(DEAD), True)
check("a host that fully resolved stays out of the summary",
      render_summary([good("192.0.2.2")]), "")


print()
print("-- when it is the shared login that is wrong, say so --")
from linkfinder.diagnose import shared_login_refused


class Answered(object):
    def __init__(self, target, error=""):
        self.target = target
        self.error = error


# Thirteen hosts refusing a password while a fourteenth works is a confusing
# morning. The one that works is the one with a login of its own, and that
# pattern says which field is wrong - without it the honest-looking reading is
# "the tool is broken", which is the wrong one.
# THE wording the tool actually raises. It used to be checked against the
# friendly label shown on screen, which shares not one word with it, so this
# whole diagnosis silently never fired on a real run.
REAL_REFUSAL = "SSH rejected the credentials (Authentication failed.)"
refused = [Answered("198.51.100.%d" % n, REAL_REFUSAL) for n in range(1, 14)]
mine = Answered("198.51.100.99")
said = shared_login_refused(refused + [mine], {"198.51.100.99": object()})
check("the pattern is named", "13 host(s) using the shared login" in said, True)
check("and the host that got in is credited",
      "with a login of their own got in" in said, True)
check("it points at the field, not at the hosts",
      "not at the hosts" in said, True)
check("it fires on the wording a real run produces",
      bool(shared_login_refused(
          [Answered("10.0.0.%d" % n, REAL_REFUSAL) for n in range(1, 15)], {})),
      True)
check("and on the other words a refusal can carry",
      bool(shared_login_refused(
          [Answered("10.0.0.%d" % n, "Permission denied (publickey,password)")
           for n in range(1, 5)], {})), True)

check("one host refusing is not a pattern",
      shared_login_refused([refused[0]], {}), "")
check("nor is it when some hosts using the same login got in",
      shared_login_refused(refused + [Answered("198.51.100.50")], {}), "")
check("and a run with nothing wrong says nothing",
      shared_login_refused([mine], {}), "")
check("a different error is not mistaken for a refusal",
      shared_login_refused(
          [Answered("198.51.100.%d" % n, "no route to host") for n in range(1, 9)],
          {}), "")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all reporting checks passed")

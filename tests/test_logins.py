"""Checks that a login typed for one host actually reaches that host.

Reported from the bench: a host was given its own password in Advanced and
the scan ignored it. The lookup was an exact string match, so a row spelled
root@10.0.0.5 - or with different capitals from the target - matched nothing
and was passed over in silence. A failed login then looks like a firewall.

The rule this file defends: a per-host login either matches, or the tool
says out loud that it did not. Silence is the bug.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.engine import Options, creds_for, host_key, unused_logins
from linkfinder.sshx import Creds

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


shared = Creds(user="root", password="rack-wide")
opts = Options(ssh=shared, per_host={
    "10.0.0.5": Creds(password="plain"),
    "root@10.0.0.6": Creds(password="prefixed"),
    "SERVER01": Creds(user="adm", password="shouty"),
    "10.9.9.9": Creds(password="for a host nobody is scanning"),
})

print("-- however the row is spelled, the password is used --")
check("a plain address", creds_for(opts, "10.0.0.5").password, "plain")
check("one written root@address",
      creds_for(opts, "10.0.0.6").password, "prefixed")
check("and the user in that prefix is kept",
      creds_for(opts, "10.0.0.6").user, "root")
check("a name in different capitals",
      creds_for(opts, "server01").password, "shouty")
check("with its own user", creds_for(opts, "server01").user, "adm")

print()
print("-- a host with no row still gets the shared login --")
check("the rack password", creds_for(opts, "10.0.0.7").password, "rack-wide")
check("and the rack user", creds_for(opts, "10.0.0.7").user, "root")

print()
print("-- a row that matches nothing is named, not swallowed --")
left = unused_logins(["10.0.0.5", "10.0.0.6", "SERVER01"], opts)
check("the one that matches nothing is reported", left, ["10.9.9.9"])
check("and the ones that matched are not",
      unused_logins(["10.0.0.5", "10.0.0.6", "server01", "10.9.9.9"], opts), [])

print()
print("-- the key is the host, whatever is wrapped round it --")
check("a user is split off", host_key("root@10.0.0.5"), ("10.0.0.5", "root"))
check("a user and password too",
      host_key("root:secret@10.0.0.5"), ("10.0.0.5", "root"))
check("capitals do not matter", host_key("Server01")[0], "server01")
check("spaces do not either", host_key("  10.0.0.5 ")[0], "10.0.0.5")

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all per-host login checks passed")

"""Reading a target list: logins per host, and catching the mistyped address.
Run: python tests/test_targets.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.engine import Options, creds_for, targets_with_logins
from linkfinder.sshx import Creds
from linkfinder.targets import complain, parse, parse_one, split

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


print("-- a target can carry its own login --")
plain = parse_one("10.0.0.5")
check("a bare address has none", (plain.host, plain.has_login),
      ("10.0.0.5", False))
user = parse_one("root@10.0.0.5")
check("a user alone", (user.host, user.user, user.password),
      ("10.0.0.5", "root", ""))
both = parse_one("admin:secret@10.0.0.9")
check("a user and a password", (both.host, both.user, both.password),
      ("10.0.0.9", "admin", "secret"))
check("a password may contain an at sign",
      parse_one("admin:se@cret@10.0.0.9").password, "se@cret")
check("because the split is from the right",
      parse_one("admin:se@cret@10.0.0.9").host, "10.0.0.9")
check("a hostname works the same", parse_one("ops@sw.example.com").host,
      "sw.example.com")

print()
print("-- a mistyped address is caught, a real hostname is not --")
check("the extra digit", parse_one("198.51.100.1801").problem,
      "not a valid IPv4 address")
check("a short one", parse_one("10.0.0").problem, "not a valid IPv4 address")
check("an octet over 255", parse_one("256.1.1.1").problem,
      "not a valid IPv4 address")
check("a good address", parse_one("198.51.100.180").problem, "")
check("a hostname", parse_one("server1").problem, "")
check("a dotted hostname", parse_one("web2.corp.example").problem, "")
check("IPv6", parse_one("2001:db8::1").problem, "")
check("something that is neither", parse_one("not a host!").problem,
      "not an address or a hostname")
check("blank", parse_one("").problem, "empty")

print()
print("-- a whole list --")
hosts, logins, bad = split(["10.0.0.1, 10.0.0.2", "root@10.0.0.3",
                            "10.0.0.9999", "10.0.0.1"])
check("commas and spaces both separate", hosts,
      ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
check("a repeat is dropped", hosts.count("10.0.0.1"), 1)
check("the login is kept against its host", logins, {"10.0.0.3": ("root", "")})
check("the bad one is set aside", len(bad), 1)
check("with a reason a person can act on", complain(bad),
      ["10.0.0.9999: not a valid IPv4 address - skipped"])

work = tempfile.mkdtemp(prefix="lf-targets-")
path = os.path.join(work, "hosts.txt")
with open(path, "w", encoding="utf-8") as handle:
    handle.write("10.0.0.1\n"
                 "admin:secret@10.0.0.2   # this one is different\n"
                 "\n"
                 "# a comment on its own\n"
                 "10.0.0.33333\n")
hosts, logins, bad = split([], path)
check("a file is read the same way", hosts, ["10.0.0.1", "10.0.0.2"])
check("comments and blanks are ignored", len(hosts), 2)
check("logins survive the file", logins["10.0.0.2"], ("admin", "secret"))
check("and so does the complaint", len(bad), 1)

print()
print("-- what the engine does with them --")
hosts, per_host = targets_with_logins(["root@10.0.0.3", "10.0.0.4"])
check("only the addresses come back as targets", hosts, ["10.0.0.3", "10.0.0.4"])
check("and the login as a Creds", per_host["10.0.0.3"].user, "root")
check("a host with no login of its own is absent", "10.0.0.4" in per_host, False)

said = []
targets_with_logins(["10.0.0.9999"], log=said.append)
check("the rejection is reported to the caller", len(said), 1)

opts = Options(ssh=Creds(user="shared", password="sharedpw", port=22))
opts.per_host = {"10.0.0.3": Creds(user="root"),
                 "10.0.0.5": Creds(user="admin", password="own")}
check("a host with no override keeps the shared login",
      creds_for(opts, "10.0.0.4").user, "shared")
check("a user-only override still borrows the shared password",
      (creds_for(opts, "10.0.0.3").user, creds_for(opts, "10.0.0.3").password),
      ("root", "sharedpw"))
check("a full override uses neither",
      (creds_for(opts, "10.0.0.5").user, creds_for(opts, "10.0.0.5").password),
      ("admin", "own"))
check("and keeps the shared port", creds_for(opts, "10.0.0.5").port, 22)
check("sudo falls back to that host's own password",
      creds_for(opts, "10.0.0.5").sudo_pw(), "own")

print()
print("-- the order credentials are tried in --")
from linkfinder.neighbors import credential_sets

order = credential_sets(opts, "10.0.0.5")
check("the host's own login goes first", order[0].user, "admin")
check("the shared one is still there as a fallback", order[1].user, "shared")
check("a host with no override just gets the shared one",
      [c.user for c in credential_sets(opts, "10.0.0.4")], ["shared"])

opts.cred_list = [Creds(user="ops", password="third")]
check("extras come after both",
      [c.user for c in credential_sets(opts, "10.0.0.5")],
      ["admin", "shared", "ops"])
check("and are not repeated when they duplicate the shared pair",
      len(credential_sets(Options(ssh=Creds(user="a", password="b"),
                                  cred_list=[Creds(user="a", password="b")]),
                          "10.0.0.1")), 1)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all target checks passed")

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
from linkfinder.targets import complain, parse_one, split

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
check("the rejection is reported to the caller",
      any("10.0.0.9999" in m and "skipped" in m for m in said), True)

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
print("-- remembering a login per host in the defaults file --")
import json

import linkfinder.engine as engine_mod
from linkfinder.engine import (load_config, per_host_from_config,
                               per_host_to_config, save_config)

cfg_dir = tempfile.mkdtemp(prefix="lf-cfg-")
cfg_file = os.path.join(cfg_dir, ".linkfinder.json")
with open(cfg_file, "w", encoding="utf-8") as handle:
    json.dump({"user": "shared", "password": "sharedpw", "community": "public"},
              handle)
original_paths = engine_mod.CONFIG_PATHS
engine_mod.CONFIG_PATHS = [cfg_file]
try:
    table = {"10.0.0.5": Creds(user="root", password="one"),
             "10.0.0.6": Creds(user="admin", password="two")}
    save_config({"per_host": per_host_to_config(table)})
    after = load_config()
    check("a hand-written key survives being saved over", after.get("user"), "shared")
    check("and so does one we never touch", after.get("community"), "public")
    check("the table is written", sorted(after.get("per_host", {})),
          ["10.0.0.5", "10.0.0.6"])
    check("and reads back as credentials",
          per_host_from_config(after)["10.0.0.6"].user, "admin")

    opts = Options(ssh=Creds(user="shared", password="sharedpw"),
                   per_host=per_host_from_config(after))
    # On a machine with no secret store the password cannot be kept at
    # all. The row must say which host lost it, rather than quietly
    # letting that host fall back to the shared password.
    from linkfinder import vault

    if vault.available():
        check("a listed host uses its own login",
              (creds_for(opts, "10.0.0.6").user,
               creds_for(opts, "10.0.0.6").password), ("admin", "two"))
    else:
        lost = []
        per_host_to_config(table, lost=lost)
        check("with no secret store, the hosts that lost it are named",
              sorted(lost), ["10.0.0.5", "10.0.0.6"])
    check("an unlisted host still uses the shared one",
          creds_for(opts, "10.0.0.9").user, "shared")

    save_config({"per_host": {}})
    check("forgetting clears only the table", load_config().get("per_host"), {})
    check("the rest of the file is untouched", load_config().get("user"), "shared")
finally:
    engine_mod.CONFIG_PATHS = original_paths

check("a defaults file with no table gives nothing", per_host_from_config({}), {})
check("a user:pass string in the file is understood",
      per_host_from_config({"per_host": {"h": "ops:pw"}})["h"].password, "pw")
check("a row with no login at all is not written",
      per_host_to_config({"h": Creds()}), {})

print()
print("-- a finished host is handed over before the slow one catches up --")
import time

import linkfinder.engine as engine_mod
from linkfinder.model import TargetResult

DELAY = {"a": 0.4, "b": 0.1, "c": 0.2}
real_one, real_finish = engine_mod.discover_one, engine_mod._finish_results


def fake_one(target, opts, log=None):
    time.sleep(DELAY[target])
    return TargetResult(target=target, kind="linux", error="",
                        device_name="host-" + target)


engine_mod.discover_one = fake_one
engine_mod._finish_results = lambda results, opts, log=None: results
try:
    seen = []
    began = time.time()
    out = engine_mod.discover_many(
        ["a", "b", "c"], Options(workers=3),
        on_result=lambda r: seen.append((r.target, time.time() - began)))
    check("every host is handed over once", sorted(t for t, _w in seen),
          ["a", "b", "c"])
    check("the quickest arrives first", seen[0][0], "b")
    check("and well before the slowest finishes", seen[0][1] < 0.4, True)
    check("the returned list keeps the order that was asked for",
          [r.target for r in out], ["a", "b", "c"])

    quiet = engine_mod.discover_many(["b"], Options(workers=1))
    check("no callback is required", [r.target for r in quiet], ["b"])

    angry = []
    engine_mod.discover_many(
        ["b"], Options(workers=1),
        on_result=lambda r: angry.append(1) or (_ for _ in ()).throw(ValueError("x")))
    check("a callback that throws does not take the scan down", angry, [1])
finally:
    engine_mod.discover_one, engine_mod._finish_results = real_one, real_finish


print()
print("-- the log says what was accepted and what was refused, at a glance --")
spoke = []
good_hosts, _creds = engine_mod.targets_with_logins(
    ["10.0.0.1,10.0.0.2,10.63249.224"], log=lambda m: spoke.append(m))
check("the valid addresses are scanned", good_hosts, ["10.0.0.1", "10.0.0.2"])
check("the log names the accepted addresses",
      any("accepted 2 address(es): 10.0.0.1, 10.0.0.2" in m for m in spoke), True)
check("and names the refused one with its reason",
      any("10.63249.224" in m and "not a valid IPv4" in m and "skipped" in m
          for m in spoke), True)
spoke2 = []
none_hosts, _ = engine_mod.targets_with_logins(
    ["nope!", "10.63249.224"], log=lambda m: spoke2.append(m))
check("nothing valid means nothing scanned", none_hosts, [])
check("and the log says to fix the addresses",
      any("nothing valid to scan" in m for m in spoke2), True)


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all target checks passed")

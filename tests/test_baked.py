"""The internal build's default logins, and the order they are tried in.

The feature is a convenience with a sharp edge: a password baked into a
binary must reach the internal build and nothing else, must never beat what
the user actually typed, and must only ever be tried against a login that was
outright refused. Each of those is checked here.

Run: python tests/test_baked.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import baked
from linkfinder.engine import Options, _login_sequence
from linkfinder.sshx import Creds

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- a run from source carries no logins at all --")
# The loader searches only the bundle (sys._MEIPASS), never the working tree,
# so a developer scanning a customer cannot do it on lab defaults by accident.
baked._LOADED = False
baked._CACHE[:] = []
check("nothing baked in when run from source", baked.default_creds(), [])


print()
print("-- reading a bundled list --")
rows = baked._read([
    {"user": "root", "password": "secret1"},
    {"user": "svc-op", "password": "secret2", "become_password": "secret3"},
    {"user": "", "password": "orphan"},          # no user: dropped
    "not a dict",                                  # junk: dropped
    {"user": "ubuntu", "password": "secret4", "port": "2222"},
])
check("only the real rows survive", [c.user for c in rows],
      ["root", "svc-op", "ubuntu"])
check("the su password is carried for the account that needs it",
      rows[1].become_password, "secret3")
check("and a port that came as a string is still an int",
      rows[2].port, 2222)


print()
print("-- many threads load the defaults at once without losing them --")
# A scan probes many hosts on threads at once. The flag that says "loaded"
# must be set only AFTER the cache is filled: setting it first let a second
# thread see "loaded" and read the still-empty cache, so that host fell back
# to an empty login and tried the operator's local account. A slow read
# widens that window so the old bug shows every time; the fix holds.
import tempfile
import threading
import time

bundle_dir = tempfile.mkdtemp(prefix="lf-baked-")
with io.open(os.path.join(bundle_dir, "logins.json"), "w", encoding="utf-8") as fh:
    json.dump([{"user": "root", "password": "p"},
               {"user": "ubuntu", "password": "p"}], fh)

real_read = baked._read
baked._read = lambda rows: (time.sleep(0.05), real_read(rows))[1]
baked._folders = lambda: iter([bundle_dir])
baked._LOADED = False
baked._CACHE[:] = []

start = threading.Barrier(12)
seen = []


def grab():
    start.wait()
    seen.append(len(baked.default_creds()))


threads = [threading.Thread(target=grab) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
baked._read = real_read
check("every thread sees the full list, never an empty one",
      sorted(set(seen)), [2])


print()
print("-- an empty username is refused, never sent as the local account --")
# paramiko would fall back to the OS user and try THAT on the lab host. The
# guard fires before any socket work, so it needs no network to prove.
from linkfinder.sshx import Ssh, SshError
try:
    Ssh("203.0.113.1", Creds(user="", password="x"), timeout=1).connect()
    check("connecting with no username raises", False, True)
except SshError as exc:
    check("connecting with no username raises", "username" in str(exc).lower(), True)


print()
print("-- a login given by hand is used alone, never topped up with defaults --")
# The rule the operator set: a login they supplied is tried and, if refused,
# the host is reported refused. The tool must NOT then work through every
# baked default behind their back - that could lock an account and hides a typo.
USER = Creds(user="me", password="mine")
baked.default_creds = lambda: [Creds(user="root", password="password"),
                               Creds(user="ubuntu", password="password")]
seq = _login_sequence(Options(), USER)
check("the hand-given login is the only one tried",
      [(c.user, c.password) for c in seq], [("me", "mine")])
check("the baked defaults are NOT appended to it",
      [c.user for c in seq], ["me"])

# Extra sets the operator added are still theirs, so they are tried too - but
# the baked defaults stay out, because a login was given.
opts = Options()
opts.cred_list = [Creds(user="extra", password="x")]
seq = _login_sequence(opts, USER)
check("hand-given extras are tried, defaults still stay out",
      [c.user for c in seq], ["me", "extra"])

# A host nobody gave a login for falls back to the baked defaults - that is
# the only time they are used.
seq = _login_sequence(Options(), Creds())
check("a host with no login given falls back to the defaults",
      [c.user for c in seq], ["root", "ubuntu"])

# With nothing baked (the open build) and no login given, there is nothing.
baked.default_creds = lambda: []
check("the open build with no login given tries nothing",
      _login_sequence(Options(), Creds()), [])

# A password typed with no username cannot be used - SSH needs a username - so
# it is dropped, never attempted (which would fall back to the local account).
# Because a login WAS given, the defaults do not step in; the sequence is empty
# and the caller reports the missing username rather than scanning on defaults.
baked.default_creds = lambda: [Creds(user="root", password="password")]
only_pw = Creds(user="", password="typed-but-no-user")
check("a password with no username leaves an empty sequence, not a default login",
      _login_sequence(Options(), only_pw), [])


print()
print("-- the radio bundles load from the binary only, like the logins --")
import tempfile as _tf
_dir = _tf.mkdtemp(prefix="lf-radio-")
with io.open(os.path.join(_dir, "radio-logins.json"), "w", encoding="utf-8") as fh:
    json.dump([{"user": "ru-admin", "password": "x"}, {"user": "root", "password": "y"},
               {"password": "no-user-dropped"}], fh)
with io.open(os.path.join(_dir, "radio-sources.json"), "w", encoding="utf-8") as fh:
    json.dump([{"glob": "/var/log/x/rrh_*", "prefix": "rrh_"}, {"no": "glob"}], fh)
baked._folders = lambda: iter([_dir])
check("radio logins load as (user, password), userless rows dropped",
      baked.radio_creds(), [("ru-admin", "x"), ("root", "y")])
check("radio file rules load, a rule with no glob dropped",
      baked.radio_file_rules(), [{"glob": "/var/log/x/rrh_*", "prefix": "rrh_"}])
baked._folders = lambda: iter([])      # the open build / a source run: nothing
check("the open build carries no radio logins", baked.radio_creds(), [])
check("and no radio file rules", baked.radio_file_rules(), [])


print()
print("-- the file the bundle would carry is well formed --")
# The real list lives in branding/, which is untracked, so it is not read
# here. If a developer has one on disk, it must at least parse and name a
# root login - a broken bundle would silently ship no defaults.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
local = os.path.join(HERE, "branding", "logins.json")
if os.path.exists(local):
    data = json.load(io.open(local, encoding="utf-8"))
    check("it is a list", isinstance(data, list), True)
    check("every row names a user",
          all(isinstance(r, dict) and r.get("user") for r in data), True)
    check("root is one of them",
          any(r.get("user") == "root" for r in data), True)
else:
    print("no local branding/logins.json - skipping (this is the open case)")


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all baked-login checks passed")

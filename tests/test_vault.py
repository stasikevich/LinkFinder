"""Checks for where a remembered password is kept.

The point of these is that a failure here is silent and expensive: a password
that looks saved but is readable, or one sealed so well it cannot be read
back. Both are checked against the real store on whatever machine runs this.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import engine, vault
from linkfinder.sshx import Creds

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


print("-- what this machine can honestly offer --")
print("   store: %s" % vault.describe())
check("a machine with no store says why, a machine with one says nothing",
      bool(vault.why_not()), not vault.available())

print()
print("-- a sealed secret is not readable, and comes back exactly --")
SECRET = "P@ssw0rd юникод §"
sealed = vault.protect(SECRET)
if not vault.available():
    check("with no store, nothing is handed back to be written", sealed, None)
else:
    check("sealing produced something", bool(sealed), True)
    check("it is tagged, so an old file can be told apart",
          vault.looks_protected(sealed), True)
    check("the password is not in it", SECRET in str(sealed), False)
    check("and it reads back byte for byte", vault.unprotect(sealed), SECRET)
    check("an empty password stays empty, not sealed", vault.protect(""), "")
    if sealed.startswith(vault.TAG_DPAPI):
        bad = sealed[:-6] + ("AAAA" if "AAAA" not in sealed[-6:] else "BBBB") + "=="
        check("a tampered blob gives nothing back, not rubbish",
              vault.unprotect(bad), "")

print()
print("-- a file written before this existed still works --")
check("an untagged value is read as the plaintext it is",
      vault.unprotect("plain-as-typed"), "plain-as-typed")
check("and is not mistaken for a sealed one",
      vault.looks_protected("plain-as-typed"), False)

print()
print("-- the defaults file stops holding passwords --")
STORE = os.path.join(tempfile.gettempdir(), "lf_vault_config.json")
if os.path.exists(STORE):
    os.remove(STORE)
io.open(STORE, "w", encoding="utf-8").write(json.dumps(
    {"per_host": {"198.51.100.4": {"user": "root", "password": "hunter2"},
                  "198.51.100.5": {"user": "root", "key_file": "~/.ssh/id"}}}))
keep = engine.CONFIG_PATHS[:]
engine.CONFIG_PATHS[:] = [STORE]
try:
    check("the plaintext row is found",
          engine.plaintext_passwords(), ["198.51.100.4"])
    sealed_hosts, why = engine.seal_stored_passwords()
    on_disk = json.load(io.open(STORE, encoding="utf-8"))
    row = on_disk["per_host"]["198.51.100.4"]
    if vault.available():
        check("it is reported as moved", sealed_hosts, ["198.51.100.4"])
        check("the file no longer holds the password",
              "hunter2" in json.dumps(on_disk), False)
        check("the scan still gets the real password back",
              engine.per_host_from_config()["198.51.100.4"].password, "hunter2")
        check("running it again finds nothing left to move",
              engine.seal_stored_passwords()[0], [])
    else:
        check("with no store it is not moved, and says why", bool(why), True)
        check("and the password is not written anywhere new",
              "password" in row, True)
    check("a host that uses a key is left alone",
          json.load(io.open(STORE, encoding="utf-8")
                    )["per_host"]["198.51.100.5"].get("key_file"), "~/.ssh/id")

    print()
    print("-- a new password is never written as typed --")
    engine.save_config({"per_host": engine.per_host_to_config(
        {"198.51.100.9": Creds(user="root", password="s3cr3t")})})
    text = io.open(STORE, encoding="utf-8").read()
    check("the file does not contain it", "s3cr3t" in text, False)
    if vault.available():
        check("but the tool still reads it",
              engine.per_host_from_config()["198.51.100.9"].password, "s3cr3t")
finally:
    engine.CONFIG_PATHS[:] = keep
    if os.path.exists(STORE):
        os.remove(STORE)


print()
print("-- the NetBox token is sealed too, and it is the more dangerous one --")
from linkfinder import netbox                            # noqa: E402

TOKEN = "nbt_akey.asecretthatisnotreal"
engine.CONFIG_PATHS[:] = [STORE]
engine.save_config({"netbox": {"url": "https://netbox.example",
                               "token": TOKEN}})
check("a token written by an older build is found",
      engine.seal_stored_token() if vault.available() else True, True)
if vault.available():
    check("the file no longer holds it as typed",
          TOKEN in io.open(STORE, encoding="utf-8").read(), False)
    check("but the tool still reads the real one",
          netbox.settings("", "", engine.load_config())[1], TOKEN)
    check("and running the migration again does nothing",
          engine.seal_stored_token(), False)
engine.CONFIG_PATHS[:] = keep
if os.path.exists(STORE):
    os.remove(STORE)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all vault checks passed")

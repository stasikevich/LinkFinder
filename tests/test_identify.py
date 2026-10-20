"""Naming a device by what it volunteers, with nobody's password.
Run: python tests/test_identify.py

The banners are the real ones a radio and a base station gave, with the
site's addresses replaced by documentation ones.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.identify import collect, describe, parse

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


RADIO = "\n".join([
    "OPEN|169.254.12.11|22",
    "SAID|169.254.12.11|22|SSH-2.0-dropbear_2020.80",
    "OPEN|169.254.12.11|830",
    "SAID|169.254.12.11|830|SSH-2.0-libssh_0.8.90",
])
PLAIN = "\n".join([
    "OPEN|169.254.12.1|22",
    "SAID|169.254.12.1|22|SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.10",
])

print("-- reading what came back --")
seen = parse(RADIO)
check("the address is the key", sorted(seen), ["169.254.12.11"])
check("both open ports are kept", seen["169.254.12.11"]["open"], [22, 830])
check("and what each said",
      seen["169.254.12.11"]["said"][830], "SSH-2.0-libssh_0.8.90")
check("an open port that said nothing still counts",
      parse("OPEN|198.51.100.5|22")["198.51.100.5"]["open"], [22])
check("rubbish is ignored", parse("total 0\nnonsense"), {})
check("and nothing at all", parse(""), {})

print()
print("-- the call: radio, or something else --")
radio = describe(seen["169.254.12.11"])
check("an embedded box speaking NETCONF is a radio", radio["kind"], "radio")
check("said in one line, not a transcript", len(radio["facts"]), 1)
check("naming the ports that answered",
      radio["facts"][0], "answers on ssh, netconf")
check("while the banner that decided it stays in the record",
      radio["said"][22], "SSH-2.0-dropbear_2020.80")

# The base station sits on the same wire and must not be mistaken for one.
host = describe(parse(PLAIN)["169.254.12.1"])
check("a general-purpose Linux box is not called a radio", host["kind"], "")
check("its banner is kept too", "OpenSSH_8.9p1" in host["said"][22], True)

print()
print("-- the embedded stack may be on either port --")
# A radio here runs a general-purpose OpenSSH on 22 and serves NETCONF over
# libssh on 830. Reading only the first banner that answered threw the second
# signal away and left ten real radios called nothing at all.
check("OpenSSH on 22 does not hide libssh on 830",
      describe({"open": [22, 830],
                "said": {22: "SSH-2.0-OpenSSH_8.7",
                         830: "SSH-2.0-libssh_0.9.8"}})["kind"], "radio")
check("but NETCONF over plain OpenSSH is still not a radio",
      describe({"open": [22, 830],
                "said": {22: "SSH-2.0-OpenSSH_9.0",
                         830: "SSH-2.0-OpenSSH_9.0"}})["kind"], "")

print()
print("-- one signal is never enough --")
check("NETCONF alone does not make a radio",
      describe({"open": [830], "said": {830: "SSH-2.0-OpenSSH_9.0"}})["kind"],
      "")
check("nor does an embedded SSH alone",
      describe({"open": [22], "said": {22: "SSH-2.0-dropbear_2020.80"}})["kind"],
      "")

print()
print("-- a device that opens a port but will not talk --")
quiet = describe({"open": [22, 443], "said": {}})
check("no kind is claimed", quiet["kind"], "")
check("and it says what it did hear",
      "answers on ssh, https" in quiet["facts"][0], True)
check("a device that answered nothing says so",
      "nothing answered" in describe({"open": [], "said": {}})["facts"][0],
      True)

print()
print("-- end to end, as the probe calls it --")
asked = []


def host_run(cmd):
    asked.append(cmd)
    return RADIO


said = []
out = collect(host_run, ["169.254.12.11"], pid="1265316", log=said.append)
check("the device is identified", out["169.254.12.11"]["kind"], "radio")
check("the probe ran inside the namespace",
      "nsenter -t 1265316 -n " in asked[0], True)
check("one read, not one per port", len(asked), 1)
check("and the operator is told",
      any("answers on ssh, netconf - a radio" in m for m in said), True)

print()
print("-- it never logs in and never writes, except to ask HTTP --")
body = asked[0]
for forbidden in ("password", "login", "PUT ", "POST ", "DELETE "):
    check("no %s anywhere in what we run" % forbidden.strip().lower(),
          forbidden in body, False)
check("the only thing written is an HTTP HEAD",
      body.count("sendall"), 1)
check("and it is a HEAD", "HEAD / HTTP/1.0" in body, True)

print()
print("-- a host that cannot run the probe --")
check("nothing asked for, nothing done", collect(lambda _c: "", []), {})


def angry(_cmd):
    raise RuntimeError("ssh died")


check("a dead connection does not take the scan down",
      collect(angry, ["198.51.100.5"]), {})

tries = []


def no_python3(cmd):
    tries.append(cmd)
    return "" if "python3" in cmd else RADIO


check("a host without python3 is retried as python",
      collect(no_python3, ["169.254.12.11"])["169.254.12.11"]["kind"], "radio")
check("and only then", len(tries), 2)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all identification checks passed")

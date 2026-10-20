"""Reading a radio's identity over NETCONF, and the file-name fallback.

The point is one answer - the radio's own serial and model - got the standard
way (ietf-hardware over NETCONF) when its M-plane is up, and off the host's own
files when it is not. These pin down the XML lift, the login order, and the
file reader, all on sample data (no device and no lab).

Run: python tests/test_netconf.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import netconf, radiolog

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


# A trimmed ietf-hardware reply, shaped like a real one.
REPLY = (
    "<rpc-reply message-id='1'><data><hardware "
    "xmlns='urn:ietf:params:xml:ns:yang:ietf-hardware'><component>"
    "<name>radio</name><mfg-name>Example Radio Co</mfg-name>"
    "<model-name>true</model-name>"
    "<model-name>RU-Model-7</model-name>"
    "<serial-num>SN0000TEST01</serial-num>"
    "<hardware-rev>REVB</hardware-rev>"
    "<software-rev>ru-fw-9.9.9</software-rev>"
    "</component></hardware></data></rpc-reply>\n]]>]]>\n")

print("-- the identity leaves are lifted out of the ietf-hardware reply --")
hw = netconf.parse_hardware(REPLY)
check("the serial is read", hw.get("serial"), "SN0000TEST01")
check("a boolean of the same name is skipped, the real model kept",
      hw.get("model"), "RU-Model-7")
check("the maker is read", hw.get("made_by"), "Example Radio Co")
check("the hardware revision is read", hw.get("hw_rev"), "REVB")
check("the software revision is read", hw.get("sw_rev"), "ru-fw-9.9.9")

print()
print("-- a refusal or a reply with no hardware yields nothing, not a crash --")
check("permission denied is nothing", netconf.parse_hardware(
    "root@x's password: \nPermission denied (password)."), {})
check("a hello with no hardware is nothing",
      netconf.parse_hardware("<hello><capabilities/></hello>]]>]]>"), {})

print()
print("-- the logins are tried in order, stopping at the first that answers --")
calls = []
def fake_run(cmd):
    calls.append(cmd)
    # the first login is refused, the second gets the hardware
    if "wrong" in cmd:
        return "x's password: \nPermission denied (password)."
    return REPLY
got = netconf.read_hardware(fake_run, "123", "169.254.0.1",
                            [("wrong", "nope"), ("right", "yes")])
check("the second login's identity is returned", got.get("serial"), "SN0000TEST01")
check("both logins were attempted, in order", len(calls), 2)

# A reply that is neither a refusal nor hardware (M-plane down) ends it at once.
calls2 = []
def dead_run(cmd):
    calls2.append(cmd)
    return "ssh: connect to host 169.254.0.1 port 830: No route to host"
check("a shut M-plane returns nothing",
      netconf.read_hardware(dead_run, "1", "169.254.0.1",
                            [("a", "b"), ("c", "d")]), {})
check("and does not keep trying logins against a dead port", len(calls2), 1)

print()
print("-- deploy writes the helper, cleanup removes it, no port is written to --")
seen = []
netconf.deploy(lambda c: seen.append(c) or "OK")
check("deploy writes the script via base64, not inline",
      "base64 -d" in seen[-1] and netconf.SCRIPT_PATH in seen[-1], True)
netconf.cleanup(lambda c: seen.append(c) or "")
check("cleanup removes exactly that file",
      "rm -f" in seen[-1] and netconf.SCRIPT_PATH in seen[-1], True)
check("the NETCONF script only ever runs a get, never an edit",
      "edit-config" not in netconf.SCRIPT and "<get>" in netconf.SCRIPT, True)

print()
print("-- the file-name fallback lifts serials off a host's own files --")
LS = ("/var/log/x/rrh_AA20A2162544\n"
      "/var/log/x/rrh_AA2113435345\n"
      "/var/log/x/rrh_AA2113435345\n"   # a repeat is not listed twice
      "/var/log/x/notarrh.log\n")
rules = [{"glob": "/var/log/x/rrh_*", "prefix": "rrh_"}]
serials = radiolog.read(lambda c: LS, rules)
check("the serials are lifted off the names, prefix stripped",
      serials, ["AA20A2162544", "AA2113435345"])
check("no rules means nothing, not a crash", radiolog.read(lambda c: LS, []), [])
seen_cmd = []
radiolog.read(lambda c: seen_cmd.append(c) or LS, rules)
check("the reader only ever lists names, never reads a file",
      "ls -1" in seen_cmd[-1] and "cat" not in seen_cmd[-1], True)

print()
print("-- the panel shows the radios a host records in its own files --")
from linkfinder.render_topology import device_facts
node = {"name": "bbu-host", "extra": {"radios_in_logs": ["AA20A2162544", "AA2113435345"]}}
sects = {s["title"]: s["lines"] for s in device_facts(node)}
check("there is a 'Radios this host records' section",
      "Radios this host records" in sects, True)
check("it lists each serial",
      sects.get("Radios this host records"),
      ["serial AA20A2162544", "serial AA2113435345"])

# The radios a BBU reached, named by their NETCONF identity, on the host panel.
bbu = {"name": "bbu", "kind": "host", "extra": {"identified": {
    "2c:00:00:00:00:01": {"kind": "radio", "address": "169.254.12.30",
                          "model": "RU-Model-7", "serial": "SN01"},
    "2c:00:00:00:00:02": {"kind": "radio", "address": "169.254.13.30"},
    "aa:bb:cc:dd:ee:ff": {"kind": "host", "address": "10.0.0.9"}}}}
rsect = {s["title"]: s["lines"] for s in device_facts(bbu)}
lines = rsect.get("Radios on this host") or []
check("the radio with a NETCONF serial shows its model and serial",
      any("RU-Model-7" in ln and "SN01" in ln for ln in lines), True)
check("a radio whose serial was not readable still appears",
      any("169.254.13.30" in ln and "not readable" in ln for ln in lines), True)
check("a non-radio identified device is not listed as a radio",
      any("10.0.0.9" in ln for ln in lines), False)


print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all netconf/radiolog checks passed")

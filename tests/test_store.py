"""Checks for the remembered-switch store. Run: python tests/test_store.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import store

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-42s %s" % (label, "ok" if got == want else "FAIL"))


path = os.path.join(tempfile.mkdtemp(prefix="lf-store-"), "switches.json")

check("empty store", store.load(path), {})
check("learn is new", store.learn("10.70.244.30", seen_from="hostA/eth0", path=path), True)
check("learn again is not new", store.learn("10.70.244.30", seen_from="hostB/eth1", path=path), False)
check("both sources kept", store.load(path)["10.70.244.30"]["seen_from"],
      ["hostA/eth0", "hostB/eth1"])

check("ipv6 refused", store.learn("fe80:0000:0000:0000:06f4:1cff:feeb:065d", path=path), False)
check("link-local refused", store.learn("169.254.1.1", path=path), False)
check("zero refused", store.learn("0.0.0.0", path=path), False)
check("garbage refused", store.learn("not-an-ip", path=path), False)

store.mark_snmp("10.70.244.30", True, community="public", name="sw-lab-1",
                descr="ExampleNOS 1.2.3", path=path)
entry = store.load(path)["10.70.244.30"]
check("snmp ok recorded", entry["snmp_ok"], True)
check("community NOT recorded by default", "community" in entry, False)
check("name recorded", entry["name"], "sw-lab-1")

store.mark_snmp("10.70.244.30", True, community="public", store_community=True, path=path)
check("community recorded only on request",
      store.load(path)["10.70.244.30"]["community"], "public")
store.mark_snmp("10.70.244.30", True, community="public", path=path)
check("and it is not dropped afterwards",
      store.load(path)["10.70.244.30"]["community"], "public")

store.learn("10.78.94.1", name="MikroTik", path=path)
store.mark_snmp("10.78.94.1", False, path=path)
check("working switch is a candidate",
      "10.70.244.30@public" in store.candidates("public", path=path), True)
check("dead switch is skipped",
      any(c.startswith("10.78.94.1") for c in store.candidates("public", path=path)), False)
check("dead switch returns with include_failed",
      any(c.startswith("10.78.94.1") for c in store.candidates("public", include_failed=True, path=path)), True)

data = store.load(path)
data["10.78.94.1"]["snmp_last_try"] = time.time() - store.RETRY_BAD_AFTER - 10
store.save(data, path)
check("dead switch retried after a day",
      any(c.startswith("10.78.94.1") for c in store.candidates("public", path=path)), True)

check("per-switch community wins", "10.70.244.30@public" in store.candidates("other", path=path), True)


check("forget one", store.forget("10.78.94.1", path=path), 1)
check("forget missing", store.forget("1.2.3.4", path=path), 0)
check("forget all", store.forget(path=path), 1)
check("store empty again", store.load(path), {})

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all store checks passed")

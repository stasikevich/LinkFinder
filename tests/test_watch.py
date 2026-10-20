"""Checks for the memory between runs: what changed, and what only looks new.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import watch

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-64s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def bench(port="up", reachable=True, rx=-2.3, optic_c=30, clock="SLAVE",
          card_c=40, extra_port=None):
    ports = {"eno1": {"state": port}}
    if extra_port:
        ports.update(extra_port)
    return {"nodes": [{"id": "ip:198.51.100.10", "name": "node-a",
                       "state": "scanned" if reachable else "unreachable",
                       "error": "" if reachable else "no SSH",
                       "mgmt_ips": ["198.51.100.10"],
                       "extra": {
                           "ports": ports,
                           "optics": {"eno1": {"rx_dbm": rx,
                                               "temperature_c": optic_c}},
                           "sensors": {"0000:17:00": {"chip": "ice",
                                                      "celsius": card_c,
                                                      "max_celsius": 95}},
                           "ptp": {"eno1": {"state": clock}}}}],
            "edges": []}


def run(state, topology, label):
    return watch.update(state, watch.read(topology), now=label)


print("-- a reading is not an event --")
state = {"signals": {}, "events": []}
state, events = run(state, bench(), "run-1")
check("the first sight of a thing says nothing", events, [])
state, events = run(state, bench(), "run-2")
check("and neither does it staying the same", events, [])

print()
print("-- a fall is doubted once, a recovery is believed at once --")
state, events = run(state, bench(port="down"), "run-3")
check("one bad run is not a fault", events, [])
state, events = run(state, bench(port="down"), "run-4")
check("the second one is", [e["to"] for e in events], ["DOWN"])
check("and it says which thing", events[0]["label"], "node-a eno1")
state, events = run(state, bench(), "run-5")
check("coming back is reported immediately",
      [(e["label"], e["to"]) for e in events], [("node-a eno1", "UP")])
state, events = run(state, bench(), "run-6")
check("but only once", events, [])

print()
print("-- a host nobody could reach is not a host whose parts all failed --")
state, events = run(state, bench(reachable=False), "run-7")
check("one missed poll is still nothing", events, [])
state, events = run(state, bench(reachable=False), "run-8")
became = {e["label"]: e["to"] for e in events}
check("the host itself is reported down", became.get("node-a"), "DOWN")
check("and it is the only line printed", sorted(became), ["node-a"])
# the parts are not events any more, but what they are recorded as still matters
held = {s["label"]: s["state"] for s in state["signals"].values()}
check("its port is held unknown, not down", held.get("node-a eno1"), "UNKNOWN")
check("and so is its optic", held.get("node-a eno1 optic"), "UNKNOWN")
check("nothing under it was called a failure",
      sorted({v for k, v in held.items() if k != "node-a"}), ["UNKNOWN"])

print()
print("-- thresholds, not every movement --")
state = {"signals": {}, "events": []}
state, _ = run(state, bench(rx=-2.3), "a")
state, events = run(state, bench(rx=-9.0), "b")
check("light dropping well inside the budget is not news", events, [])
state, _ = run(state, bench(rx=-15.2), "c")
state, events = run(state, bench(rx=-15.4), "d")
check("light under what the optic needs is", [e["to"] for e in events], ["DEGRADED"])
check("and the line says the number and the limit",
      "under the -14 dBm" in events[0]["detail"], True)

state = {"signals": {}, "events": []}
state, _ = run(state, bench(card_c=40), "a")
state, events = run(state, bench(card_c=60), "b")
check("a card warming up is not news either", events, [])
state, _ = run(state, bench(card_c=88), "c")
state, events = run(state, bench(card_c=88), "d")
check("a card within ten of its own limit is",
      [e["to"] for e in events], ["DEGRADED"])
check("measured against the limit the card itself published",
      "95 C limit" in events[0]["detail"], True)

print()
print("-- a clock is only serving time in two of its states --")
check("a slave is fine", watch._clock_state({"state": "SLAVE"})[0], "UP")
check("a master is fine", watch._clock_state({"state": "MASTER"})[0], "UP")
check("holdover is not", watch._clock_state({"state": "HOLDOVER"})[0], "DEGRADED")
check("faulty is a failure", watch._clock_state({"state": "FAULTY"})[0], "DOWN")
check("and a port that said nothing is unknown",
      watch._clock_state({"state": ""})[0], "UNKNOWN")

print()
print("-- a port with no carrier to report is not watched at all --")
seen = watch.read(bench(extra_port={"cilium_vxlan": {"state": "unknown"},
                                    "br0": {"state": "down"}}))
check("a tunnel is left out", any("cilium_vxlan" in k for k in seen), False)
check("a bridge that is really down is not", any("br0" in k for k in seen), True)

print()
print("-- the record is kept, and does not grow for ever --")
STORE = os.path.join(tempfile.gettempdir(), "lf_watch_test.json")
if os.path.exists(STORE):
    os.remove(STORE)
watch.save(state, STORE)
again = watch.load(STORE)
check("what was known survives a restart",
      sorted(again["signals"]) == sorted(state["signals"]), True)
big = {"signals": {}, "events": [{"at": str(n), "label": "x", "to": "DOWN"}
                                 for n in range(watch.HISTORY + 50)]}
big, _ = watch.update(big, watch.read(bench()), now="z")
big, _ = watch.update(big, watch.read(bench(port="down")), now="z1")
big, _ = watch.update(big, watch.read(bench(port="down")), now="z2")
check("the history stops at its limit", len(big["events"]) <= watch.HISTORY, True)

print()
print("-- what a person reads --")
lines = watch.describe([{"at": "run-4", "label": "node-a eno1", "to": "DOWN",
                         "detail": "the port is down"}])
check("one line, in words", lines, ["run-4  node-a eno1 went down - the port is down"])
check("a count of where everything stands",
      sorted(watch.standing(state)), ["DEGRADED", "DOWN", "UNKNOWN", "UP"])

print()
print("-- one cause, one line, and a name that says which device --")
TWINS = {"nodes": [
    {"id": "ip:198.51.100.10", "name": "bench-c", "state": "scanned",
     "mgmt_ips": ["198.51.100.10"],
     "extra": {"ports": {"eno1": {"state": "up"}, "eno2": {"state": "up"}},
               "sensors": {"s": {"chip": "ice", "celsius": 40, "max_celsius": 95}}}},
    {"id": "ip:198.51.100.11", "name": "bench-c", "state": "scanned",
     "mgmt_ips": ["198.51.100.11"],
     "extra": {"ports": {"eno1": {"state": "up"}}}},
    {"id": "ip:198.51.100.12", "name": "node-c", "state": "scanned",
     "mgmt_ips": ["198.51.100.12"], "extra": {"ports": {"eno1": {"state": "up"}}}}],
    "edges": []}
seen = watch.read(TWINS)
labels = {s["label"] for s in seen.values()}
check("two devices sharing a name are told apart",
      "bench-c (198.51.100.10) eno1" in labels, True)
check("a name nobody shares is left plain", "node-c eno1" in labels, True)

GONE = {"nodes": [dict(TWINS["nodes"][0], state="unreachable",
                       error="no answer on 22"),
                  TWINS["nodes"][1], TWINS["nodes"][2]], "edges": []}
state = {"signals": {}, "events": []}
state, _ = watch.update(state, watch.read(TWINS), now="a")
state, _ = watch.update(state, watch.read(GONE), now="b")
state, events = watch.update(state, watch.read(GONE), now="c")
check("a host that stopped answering is one event, not twenty", len(events), 1)
check("and it is the host itself", events[0]["kind"], "device")
check("the parts it took with it are counted, not listed",
      "3 thing(s) on it went out of sight" in events[0]["detail"], True)
check("a device that is still fine is untouched",
      any(e["device"] == "ip:198.51.100.12" for e in events), False)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all watch checks passed")

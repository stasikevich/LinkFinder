"""Press Scan, press Map, and insist on the same answer.

Scan returns a list of links off one device. Map takes the same readings and
draws them between devices. Everything the scan resolved has to survive that
conversion, and nothing in the scan output alone proves it did - which is how
a host whose management port the scan placed on a switch could appear on the
map with no switch at all and nobody notice.

So the conversion is driven here with the real code, not a stand-in: the same
TargetResult goes through view_from_result and the crawler's own neighbour
recording, and the finished map is held against what the scan said.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.crosscheck import (compare, far_identity, links_from_map,
                                  links_from_scan, shared_addresses)
from linkfinder.crawl import Crawler
from linkfinder.engine import Options
from linkfinder.model import Neighbor, ProbeAttempt, TargetResult
from linkfinder.neighbors import view_from_result

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-62s %s" % (label, "ok" if got == want else "FAIL"))


def scan_of(host, rows):
    """A scan result the way a probe hands one back."""
    result = TargetResult(target=host, kind="linux", device_name="serverB",
                          attempts=[ProbeAttempt(method="ssh", ok=True)])
    for iface, switch, port, chassis in rows:
        result.add(Neighbor(host=host, local_iface=iface, switch=switch,
                            port=port, chassis_id=chassis, proto="LLDP",
                            source="toolbox-tcpdump"))
    return result


def map_of(results):
    """The same readings pushed through the crawler's own conversion."""
    crawler = Crawler(Options())
    for result in results:
        view = view_from_result(result.target, result)
        node = crawler._record_device(view, 0)
        crawler._record_neighbours(node, view, 0)
    return crawler.topology.to_dict()


print("-- one far end, named the same way wherever it is read --")
check("a chassis id wins", far_identity("AA:BB", "10.0.0.9", "sw"), "aa:bb")
check("then an address", far_identity("", "10.0.0.9", "sw"), "10.0.0.9")
check("then the name it gives", far_identity("", "", "SW-1"), "sw-1")
check("and nothing is nothing", far_identity("", "", ""), "")

print()
print("-- what the scan found has to be on the map --")
SCAN = [scan_of("192.0.2.52", [("oam", "sw-edge-1", "1/1/49", "11:22:33:44:55:66"),
                               ("ens2f0", "sw-edge-1", "1/1/50", "11:22:33:44:55:66")])]
MAP = map_of(SCAN)

check("the scan's links are read back",
      sorted(links_from_scan(SCAN)["192.0.2.52"]),
      [("ens2f0", "11:22:33:44:55:66", "1/1/50"),
       ("oam", "11:22:33:44:55:66", "1/1/49")])
check("and the map holds the same ones",
      links_from_map(MAP)["192.0.2.52"], links_from_scan(SCAN)["192.0.2.52"])
check("so pressing either button says the same thing", compare(SCAN, MAP), [])

print()
print("-- and when it is not, the check says so instead of us noticing by eye --")
LOST = {"nodes": MAP["nodes"],
        "edges": [e for e in MAP["edges"] if e.get("a_iface") != "oam"
                  and e.get("b_iface") != "oam"]}
missed = compare(SCAN, LOST)
check("one link dropped, one complaint", len(missed), 1)
check("it names the port that lost its switch", "on oam" in missed[0], True)
check("and who should have been there", "11:22:33:44:55:66" in missed[0], True)

print()
print("-- the map is allowed to know more, never less --")
RICHER = {"nodes": MAP["nodes"],
          "edges": MAP["edges"] + [{"a": MAP["nodes"][0]["id"], "a_iface": "ens9",
                                    "b": MAP["nodes"][1]["id"], "b_iface": "1/1/51"}]}
check("an extra cable the far end reported is not a fault",
      compare(SCAN, RICHER), [])

print()
print("-- a port the scan could not resolve is not a missing link --")
QUIET = [scan_of("192.0.2.53", [("oam", "", "", "")])]
check("nothing resolved, nothing to lose",
      compare(QUIET, map_of(QUIET)), [])

print()
print("-- a device the map never reached is not compared against it --")
check("the scan of a host that is not on the map is left alone",
      compare(SCAN, {"nodes": [], "edges": []}), [])

print()
print("-- an address two devices hold is not quietly compared against both --")
TWINS = {"nodes": [dict(MAP["nodes"][0]),
                   {"id": "chassis:aa", "name": "radio-a", "chassis_id": "aa",
                    "mgmt_ips": ["169.254.12.12"]},
                   {"id": "chassis:bb", "name": "radio-b", "chassis_id": "bb",
                    "mgmt_ips": ["169.254.12.12"]}],
         "edges": []}
check("the clash is spotted", shared_addresses(TWINS), {"169.254.12.12"})
check("a map where every address is its own is clean",
      shared_addresses(MAP), set())
BOTH = [scan_of("169.254.12.12", [("eth0", "sw", "1/1/1", "cc:dd")])]
said = compare(BOTH, TWINS)
check("and the scan of that address is refused, not guessed at", len(said), 1)
check("with the reason in the open", "more than one device" in said[0], True)

print()
print("-- the readings the scan showed reach the map too --")
READINGS = TargetResult(target="192.0.2.54", kind="linux", device_name="serverC",
                        attempts=[ProbeAttempt(method="ssh", ok=True)],
                        extra={"optics": {"oam": {"vendor": "Intel Corp"}},
                               "sensors": {"0000:04:00": {"chip": "bnxt_en",
                                                          "celsius": 47}}})
carried = view_from_result("192.0.2.54", READINGS).extra
check("the optics are not dropped on the way to the map",
      carried.get("optics", {}).get("oam", {}).get("vendor"), "Intel Corp")
check("nor the card temperature",
      carried.get("sensors", {}).get("0000:04:00", {}).get("celsius"), 47)
drawn = {n["id"]: n for n in map_of([READINGS])["nodes"]}
kept = [n for n in drawn.values() if "192.0.2.54" in (n.get("mgmt_ips") or [])]
check("and the node on the map carries them", bool(kept) and
      kept[0]["extra"].get("sensors", {}).get("0000:04:00", {}).get("celsius"), 47)

print()
print("-- a device the map copied from the scan is not evidence of agreement --")
check("reused devices are named, not silently passed",
      len(compare(SCAN, MAP, {"192.0.2.52"})), 1)
check("and the reason is in the open",
      "reused the scan" in compare(SCAN, MAP, {"192.0.2.52"})[0], True)
check("a device the map really asked is still compared",
      compare(SCAN, MAP, {"10.0.0.1"}), [])

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all cross-check checks passed")

"""The renderer turns topology JSON into one offline HTML page.
Run: python tests/test_render.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import base64
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.render_topology import (PTP_COLOUR, KIND_SIZE, KIND_STYLE, assets_dir,
                                        icon_uris, node_label, render_file,
                                        render_html, summarise, tint, to_elements,
                                        write_html)
from linkfinder.topology import SCHEMA_VERSION, STATE_SCANNED, Topology

FAILURES = []
STUB = "/* cytoscape stub */ var cytoscape = function () { return {}; };"


def check(label, got, want):
    if got != want:
        FAILURES.append("%s: got %r want %r" % (label, got, want))
    print("%-52s %s" % (label, "ok" if got == want else "FAIL"))


print("-- the vendored library is present --")
check("cytoscape.min.js shipped",
      os.path.exists(os.path.join(assets_dir(), "cytoscape.min.js")), True)
check("its MIT licence shipped",
      os.path.exists(os.path.join(assets_dir(), "cytoscape-LICENSE.txt")), True)
licence = open(os.path.join(assets_dir(), "cytoscape-LICENSE.txt"), encoding="utf-8").read()
check("licence really is MIT", "Permission is hereby granted" in licence, True)

print()
print("-- a topology with one node and no edges still renders --")
t = Topology(seed="10.0.0.1")
t.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="lonely-switch",
           kind="switch", state=STATE_SCANNED)
single = t.to_dict()
page = render_html(single, library=STUB)
check("page produced", page.startswith("<!doctype html>"), True)
check("node name reaches the page", "lonely-switch" in page, True)
check("no leftover template tokens", bool(re.search(r"__[A-Z_]+__", page)), False)
check("summary line", summarise(single), "1 devices (1 scanned), 0 links (0 confirmed)")
elements = to_elements(single)
check("one element, no edges", (len(elements["nodes"]), len(elements["edges"])), (1, 0))

print()
print("-- an incomplete scan says so on the page --")
t2 = Topology(seed="10.0.0.1")
t2.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="sw", kind="switch")
t2.stopped_because = "depth limit reached (2)"
cut = render_html(t2.to_dict(), library=STUB)
check("reason embedded", "depth limit reached (2)" in cut, True)
check("header element present", 'id="stopnote"' in cut, True)

print()
print("-- nothing in the page reaches the network --")
check("no external script tags", bool(re.search(r"<script[^>]+src=", page)), False)
check("no stylesheet links", bool(re.search(r"<link[^>]+href=", page)), False)
check("no XMLHttpRequest", "XMLHttpRequest" in page, False)
check("nothing is fetched from anywhere but this machine",
      bool(re.search(r"fetch\(\s*['\"]?https?://", page)), False)
# The rule is where a request may go, not how many there are: the page now
# asks for setups, findings and tags. Every one must name the window.
check("every request goes to the window that served the page",
      page.count("fetch(") == page.count("fetch(LIVE"), True)
check("and a saved copy never makes it, because it has no window",
      "if (!LIVE) { return; }" in page, True)
body = page.replace(STUB, "")
check("no http urls of our own", bool(re.search(r"https?://", body)), False)

print()
print("-- styling by kind, state and confidence --")
t = Topology(seed="10.0.0.1")
a = t.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="sw",
               kind="switch", state=STATE_SCANNED)
b = t.add_node(chassis_id="aa:bb:cc:dd:ee:02", mgmt_ip="10.0.0.2", name="srv", kind="host")
c = t.add_node(chassis_id="aa:bb:cc:dd:ee:03", name="ilo", kind="bmc")
t.add_edge(a.id, "1/1/1", b.id, "eno1", proto="LLDP", reported_by=a.id)
t.add_edge(b.id, "eno1", a.id, "1/1/1", proto="LLDP", reported_by=b.id)
t.add_edge(a.id, "1/1/2", c.id, "?", proto="FDB", reported_by=a.id)
elements = to_elements(t.to_dict())
kinds = {n["data"]["name"]: n["data"]["colour"] for n in elements["nodes"]}
check("switch and host differ in colour", kinds["sw"] != kinds["srv"], True)
check("bmc has its own colour", kinds["ilo"] not in (kinds["sw"], kinds["srv"]), True)
shapes = {n["data"]["name"]: n["data"]["shape"] for n in elements["nodes"]}
check("shapes differ too", len({shapes["sw"], shapes["srv"], shapes["ilo"]}), 3)
styles = sorted(e["data"]["lineStyle"] for e in elements["edges"])
check("confirmed link is solid, inferred is dotted", styles, ["dotted", "solid"])
labels = sorted(e["data"]["label"] for e in elements["edges"])
check("interface names are on the links", labels, ["1/1/1 - eno1", "1/1/2 - ?"])

print()
print("-- every device kind ships a drawing --")
missing = [k for k in KIND_STYLE
           if not os.path.exists(os.path.join(assets_dir(), "icons", k + ".svg"))]
check("no kind is left without an icon", missing, [])
check("every kind has a node size", sorted(KIND_SIZE), sorted(KIND_STYLE))
uris = icon_uris()
faces = {k: v for k, v in uris.items() if k.startswith("clock:")}
# A vendor or device glyph is a drawing, not a kind: a Dell server stays the
# host kind, a MikroTik the switch kind - only the drawing differs. So each
# ships an icon without a KIND_STYLE row of its own.
from linkfinder.render_topology import GLYPH_COLOUR
GLYPHS = sorted(set(list(KIND_STYLE) + list(GLYPH_COLOUR)))
kinds = {k: v for k, v in uris.items() if not k.startswith("clock:")}
check("one data URI per glyph", sorted(kinds), GLYPHS)
check("they are base64 SVG data URIs",
      all(v.startswith("data:image/svg+xml;base64,") for v in uris.values()), True)
check("no two glyphs share a drawing", len(set(kinds.values())), len(GLYPHS))
check("a clock face per PTP role", len(faces) > 5, True)
# Three of the unsettled states deliberately share one amber, so the faces
# are as many as the colours, not as many as the roles.
check("one clock face per PTP colour",
      len(set(faces.values())), len(set(PTP_COLOUR.values())))
raw = base64.b64decode(uris["switch"].split(",", 1)[1]).decode("utf-8")
check("the accent placeholder is filled in", "__ACCENT__" in raw, False)
check("filled with the switch colour", KIND_STYLE["switch"][0] in raw, True)
check("the drawing carries an intrinsic size (or it vanishes when zoomed)",
      'width="' in raw.split(">", 1)[0] and 'height="' in raw.split(">", 1)[0], True)

print()
print("-- the tint keeps an icon readable on its node --")
check("a kind colour washes out towards white", tint("#1f77b4", 0.13), "#e2edf5")
check("full strength is the colour itself", tint("#1f77b4", 1.0), "#1f77b4")
check("a broken colour falls back to white", tint("not-a-colour"), "#ffffff")

print()
print("-- a node carries its icon, tint and size --")
t = Topology(seed="10.0.0.1")
t.add_node(chassis_id="aa:bb:cc:dd:ee:01", mgmt_ip="10.0.0.1", name="sw",
           kind="switch", state=STATE_SCANNED)
t.add_node(chassis_id="aa:bb:cc:dd:ee:02", mgmt_ip="10.0.0.2", name="odd",
           kind="toaster")
data = {n["data"]["name"]: n["data"] for n in to_elements(t.to_dict())["nodes"]}
check("switch points at the switch drawing", data["sw"]["icon"], "switch")
check("and gets the switch node size",
      [data["sw"]["iconW"], data["sw"]["iconH"]], KIND_SIZE["switch"])
check("an unheard-of kind falls back to the unknown drawing", data["odd"]["icon"],
      "unknown")
check("and to the unknown node size",
      [data["odd"]["iconW"], data["odd"]["iconH"]], KIND_SIZE["unknown"])
page = render_html(t.to_dict(), library=STUB)
check("the icons reach the page", "data:image/svg+xml;base64," in page, True)
check("the page can still be drawn without them", 'id="showIcons"' in page, True)
check("no raw svg markup leaked into the page", "<svg" in page, False)


print()
print("-- a hostile device name cannot break out of the script block --")
t = Topology(seed="10.0.0.1")
t.add_node(chassis_id="aa:bb:cc:dd:ee:09", mgmt_ip="10.0.0.1",
           name="</script><img src=x onerror=alert(1)>", kind="switch")
nasty = render_html(t.to_dict(), library=STUB)
check("no injected closing tag", "</script><img" in nasty, False)
check("escaped instead", "u003c/script" in nasty, True)
check("only the two script blocks we wrote", nasty.count("<script>"), 2)

print()
print("-- node labels fall back sensibly --")
check("name wins", node_label({"name": "sw1", "mgmt_ips": ["10.0.0.1"]}), "sw1")
check("then address", node_label({"name": "", "mgmt_ips": ["10.0.0.1"]}), "10.0.0.1")
check("then chassis", node_label({"mgmt_ips": [], "chassis_id": "aa:bb"}), "aa:bb")
check("then id", node_label({"id": "name:thing"}), "name:thing")

print()
print("-- round trip through files --")
work = tempfile.mkdtemp(prefix="lf-render-")
json_path = os.path.join(work, "site.topology.json")
html_path = os.path.join(work, "site.topology.html")
with open(json_path, "w", encoding="utf-8") as handle:
    handle.write(t.to_json())
render_file(json_path, html_path)
check("html written", os.path.exists(html_path), True)
size = os.path.getsize(html_path)
check("and carries the library (over 300 KB)", size > 300000, True)
page = open(html_path, encoding="utf-8").read()
check("real cytoscape inside", "cytoscape" in page.lower(), True)
check("data inside too", '"schema_version"' in page or "schema_version" in page, True)

written = write_html(t.to_dict(), os.path.join(work, "again.html"))
check("write_html returns its path", os.path.basename(written), "again.html")

print()
print("-- the schema version is enforced on load --")
from linkfinder.topology import load_topology
bad = os.path.join(work, "bad.json")
with open(bad, "w", encoding="utf-8") as handle:
    json.dump({"schema_version": SCHEMA_VERSION + 99, "nodes": [], "edges": []}, handle)
try:
    load_topology(bad)
    check("future schema refused", False, True)
except ValueError as exc:
    check("future schema refused", "version" in str(exc).lower(), True)

print()
print("-- the device panel reads as lines, not as a record dump --")
from linkfinder.render_topology import device_facts

NODE = {"name": "server4", "extra": {
    "ptp": {
        "enp101s0f0": {"state": "MASTER", "domain": 24,
                       "summary": "MASTER dom 24 +0ns"},
        "enp101s0f1": {"state": "SLAVE", "domain": 24, "steps_removed": 1,
                       "summary": "SLAVE dom 24 -2ns"}},
    "sensors": {
        "0000:17:00": {"chip": "ice", "slot": "0000:17:00", "celsius": 41,
                       "max_celsius": 95, "critical_celsius": 105},
        "0000:06:00": {"chip": "i350bb", "slot": "0000:06:00", "celsius": 31,
                       "max_celsius": 120, "critical_celsius": 110}},
    "optics": {
        "enp101s0f1": {"speed_gbps": 10, "form": "SR", "media": "multimode",
                       "vendor": "PROLABS", "part": "E10GSFPSR-I-NP",
                       "serial": "SN0000000001", "wavelength_nm": 850}},
    "neighbours": {"198.51.100.9": {"mac": "aa:bb:cc:dd:ee:01", "state": "STALE"}},
}}

sections = {s["title"]: s["lines"] for s in device_facts(NODE)}
check("timing is a section", "Timing" in sections, True)
check("one line per port", len(sections["Timing"]), 2)
check("the master says so", "MASTER dom 24 +0ns" in sections["Timing"][0], True)
check("a slave says how far it is from the grandmaster",
      "1 step from the grandmaster" in sections["Timing"][1], True)

check("temperature is shown even when nothing is wrong",
      "Card temperature" in sections, True)
check("against the limit the card itself reports",
      "41 of 95 C" in sections["Card temperature"][1], True)
check("and a healthy card is not dressed up as a warning",
      "close to the limit" in " ".join(sections["Card temperature"]), False)

# Still one line per module, and it now carries the two numbers somebody
# matching a module to a fibre actually needs: 850 nm will not light a
# single-mode run, and "10G SR" does not say so.
check("optics are one line, not a datasheet",
      sections["Optics"], ["enp101s0f1     10G SR multimode 850 nm, PROLABS"])
check("and it stays one line", len(sections["Optics"]), 1)
check("the serial number stays out of the panel",
      "SN0000000001" in " ".join(sections["Optics"]), False)

check("the ARP cache is not drawn on a topology map",
      "ARP cache" in " ".join(sections.get("Optics", [])
                              + sections.get("Timing", [])), False)
check("but it is named as still being in the file",
      sections["Also recorded, in the JSON"], ["ARP cache (1)"])

hot = {"extra": {"sensors": {"0000:17:00": {"chip": "ice", "slot": "0000:17:00",
                                            "celsius": 90, "max_celsius": 95}}}}
heat = {s["title"]: s["lines"] for s in device_facts(hot)}["Card temperature"]
check("a card near its own limit says so", "close to the limit" in heat[0], True)

check("a device with nothing recorded keeps only its heading",
      [s["title"] for s in device_facts({})], ["?"])
check("an empty section is not printed",
      [s["title"] for s in device_facts({"extra": {"ptp": {}}})], ["?"])

page = render_html({"nodes": [dict(NODE, id="a", kind="host")], "edges": []})
check("the digest reaches the page", "Card temperature" in page, True)
check("and nothing prints the record field by field",
      "function rows(" in page, False)

print()
print("-- the link panel answers the question, not the record --")
from linkfinder.render_topology import link_facts

LABELS = {"chassis:sw": "sw-access-1", "ip:host": "node-a"}
EDGE = {"a": "chassis:sw", "b": "ip:host", "a_iface": "1/1/9", "b_iface": "eno1",
        "protos": ["LLDP"], "sources": ["networkctl-lldp"],
        "reported_by": ["ip:host"], "confidence": "reported",
        "speed": "1000Mb/s", "vlan": "100",
        "first_seen": "2026-10-07T13:54:27", "last_seen": "2026-10-07T13:54:27",
        "attrs": {}}

flat = "\n".join(line for section in link_facts(EDGE, LABELS)
                 for line in section["lines"])
check("each end is named by its device, not its node id",
      "sw-access-1" in flat and "node-a" in flat, True)
check("a node id never reaches the panel",
      "chassis:sw" in flat or "ip:host" in flat, False)
check("each port sits beside its own device",
      "sw-access-1" in flat.split("1/1/9")[0].splitlines()[-1], True)
check("the speed is between them", "1000Mb/s" in flat, True)
check("confidence is a sentence, not a keyword",
      "one end reported it" in flat, True)
check("the word reported alone is not shown", "\nreported\n" in flat, False)
check("how we know names the protocol and who said it",
      "LLDP, from node-a" in flat, True)
check("the capture method is not paraded",
      "networkctl" in flat, False)
check("the timestamp loses its seconds", "2026-10-07 13:54" in flat, True)
check("and is not printed twice", flat.count("2026-10-07 13:54"), 1)

print()
print("-- which side a reading came from --")
# The checks are computed by one device looking outward, so their "a" is that
# device; an edge sorts its ends by node id. The two orders are unrelated.
ONE_SIDED = dict(EDGE, attrs={
    "mtu": {"verdict": "unknown", "a": 1500, "a_node": "ip:host",
            "b_node": "chassis:sw", "detail": "MTU known on one side only"},
    "lacp": {"verdict": "unknown", "a_node": "ip:host", "b_node": "chassis:sw"},
    "vlan": {"verdict": "unknown", "a_node": "ip:host", "b_node": "chassis:sw",
             "detail": "no VLAN reported by either side"}})
checked = [s for s in link_facts(ONE_SIDED, LABELS) if s["title"] == "Checked"][0]
said = " ".join(checked["lines"])
check("the reading is credited to the device that made it",
      "Only node-a answered" in said, True)
check("and not to the one that merely sorted first",
      "sw-access-1 answered" in said, False)
check("what is known is still shown", "MTU 1500" in said, True)
check("a check with no answer is not listed as a row",
      "LACP" in said, False)
check("and its excuse is not dressed up as a finding",
      "no VLAN reported" in said, False)

print()
print("-- a layer that explains without judging --")
PTP = dict(EDGE, attrs={"ptp": {"evidence": "direct",
                                "detail": "most likely a direct cable"}})
checked = [s for s in link_facts(PTP, LABELS) if s["title"] == "Checked"][0]
check("PTP evidence is shown even with no verdict",
      "most likely a direct cable" in " ".join(checked["lines"]), True)

print()
print("-- a decided check --")
DECIDED = dict(EDGE, attrs={"mtu": {"verdict": "mismatch", "a": 9000, "b": 1500,
                                    "a_node": "ip:host", "b_node": "chassis:sw",
                                    "detail": "sending 9000 into a port that takes 1500"}})
checked = [s for s in link_facts(DECIDED, LABELS) if s["title"] == "Checked"][0]
said = " ".join(checked["lines"])
check("both values are shown", "9000 / 1500" in said, True)
check("the verdict is in words", "do not match" in said, True)
check("with the reason under it", "takes 1500" in said, True)

print()
print("-- nothing at all --")
checked = [s for s in link_facts(EDGE, LABELS) if s["title"] == "Checked"][0]
check("says so plainly",
      "Neither end reported anything" in " ".join(checked["lines"]), True)

page = render_html({"nodes": [{"id": "ip:host", "name": "node-a", "kind": "host"},
                              {"id": "chassis:sw", "name": "sw-access-1",
                               "kind": "switch"}],
                    "edges": [EDGE]})
check("the link digest reaches the page", "one end reported it" in page, True)

print()
print("-- the device panel opens with who it is, not with its id --")
PROBED = {"id": "ip:198.51.100.11", "name": "node-b", "kind": "host",
          "state": "scanned", "mgmt_ips": ["198.51.100.11"], "depth": 0,
          "source": "networkctl-lldp",
          "first_seen": "2026-10-07T14:22:11", "last_seen": "2026-10-07T14:22:11",
          "extra": {}}
shown = device_facts(PROBED)
flat = "\n".join(line for section in shown for line in section["lines"])
check("the name is the heading", shown[0]["title"], "node-b")
check("kind and address are the first line",
      shown[0]["lines"][0].split(), ["host", "198.51.100.11"])
check("and the state is said in the same word the header uses",
      "scanned" in shown[0]["lines"][1], True)
check("the probe that answered is nobody's business",
      "networkctl" in flat, False)
check("nor is the internal id", "ip:198.51.100.11" in flat, False)
check("nor the crawl depth", "depth" in flat.lower(), False)
check("nor a timestamp", "2026-10-07" in flat, False)

print()
print("-- a device that did not answer says why --")
FAILED = dict(PROBED, state="unreachable", error="no route to host")
line = device_facts(FAILED)[0]["lines"][1]
check("the reason replaces the explanation rather than trailing it",
      line.strip(), "unreachable - no route to host")
check("and there is no double dash", " - " in line and " -  - " not in line, True)

print()
print("-- a device nobody could reach still identifies itself --")
HEARD = {"id": "chassis:aa:bb:cc:dd:ee:01", "name": "sw-access-1",
         "kind": "switch", "state": "unscanned", "mgmt_ips": [],
         "chassis_id": "aa:bb:cc:dd:ee:01", "extra": {}}
lines = device_facts(HEARD)[0]["lines"]
check("the missing address is stated, not left blank",
      "no management address" in lines[0], True)
check("why it was not scanned is explained",
      "a neighbour reported it" in lines[1], True)
check("and the chassis id is kept, because that is its identity",
      any("aa:bb:cc:dd:ee:01" in one for one in lines), True)

page = render_html({"nodes": [PROBED], "edges": []})
check("the heading reaches the page", "node-b" in page, True)
# The embedded data stays complete on purpose - it is the topology JSON, and
# the filter box searches it. What matters is that nothing prints it blindly.
check("the record is still in the page data, for search and for export",
      "networkctl-lldp" in page, True)
check("but no code there prints a record field by field",
      "function rows(" in page, False)

print()
print("-- a state you do not want to look at can be hidden --")
page = render_html({"nodes": [
    {"id": "ip:198.51.100.1", "name": "node-a", "kind": "host",
     "state": "scanned", "mgmt_ips": ["198.51.100.1"], "extra": {}},
    {"id": "ip:198.51.100.2", "name": "node-b", "kind": "host",
     "state": "unreachable", "mgmt_ips": ["198.51.100.2"], "extra": {}}],
    "edges": []})
check("there is a tick for devices that never answered",
      'id="showUnreachable"' in page, True)
check("beside the one that was already there",
      'id="showUnscanned"' in page, True)
check("both go through one handler, not two copies of it",
      page.count("hideByState("), 3)
check("and the states they hide are named",
      "hideByState('showUnreachable', 'unreachable')" in page, True)

print()
print("-- a bond, and the two things that go wrong inside one --")
BONDED = {"name": "node-a", "kind": "host", "state": "scanned",
          "mgmt_ips": ["198.51.100.1"],
          "extra": {"bonds": {"bond0": ["eno1", "eno2"]},
                    "bond_detail": {"bond0": {
                        "name": "bond0", "mode": "802.3ad 4",
                        "lacp_rate": "slow 0",
                        "ad_partner_mac": "00:00:00:00:00:00",
                        "legs": [{"name": "eno1", "mii": "up", "failures": "0"},
                                 {"name": "eno2", "mii": "down", "failures": "3"}]}}}}
bonds = {s["title"]: s["lines"] for s in device_facts(BONDED)}["Bonds"]
said = " ".join(bonds)
check("the mode is shown, because the switch has to match it",
      "802.3ad" in said, True)
check("every leg is listed", "eno1" in said and "eno2" in said, True)
check("a leg that is down says so", "down" in said, True)
check("and its failures are counted", "3 link failures" in said, True)
check("the count of down legs is spelled out", "1 of 2 legs are down" in said, True)
check("a bond that is not negotiating is called out",
      "LACP is not negotiating" in said, True)
check("the bond is no longer just a number in the JSON line",
      "bonds (1)" in " ".join(l for sec in device_facts(BONDED)
                              for l in sec["lines"]), False)

HEALTHY = {"name": "node-b", "kind": "host", "state": "scanned", "mgmt_ips": [],
           "extra": {"bonds": {"bond0": ["eno1", "eno2"]},
                     "bond_detail": {"bond0": {
                         "name": "bond0", "mode": "802.3ad 4",
                         "ad_partner_mac": "aa:bb:cc:dd:ee:01",
                         "legs": [{"name": "eno1", "mii": "up", "failures": "0"},
                                  {"name": "eno2", "mii": "up", "failures": "0"}]}}}}
said = " ".join({s["title"]: s["lines"] for s in device_facts(HEALTHY)}["Bonds"])
check("a working bond is reported without alarm",
      "not negotiating" in said or "legs are down" in said, False)
check("but it is still shown", "802.3ad" in said, True)

print()
print("-- the ways a bond fails while still looking like a bond --")


def bond_said(legs, partner="aa:bb:cc:dd:ee:01",
              mode="IEEE 802.3ad Dynamic link aggregation"):
    node = {"name": "n", "kind": "host", "state": "scanned", "mgmt_ips": [],
            "extra": {"bonds": {"bond0": [l["name"] for l in legs]},
                      "bond_detail": {"bond0": {"name": "bond0", "mode": mode,
                                                "ad_partner_mac": partner,
                                                "legs": legs}}}}
    return " ".join({s["title"]: s["lines"]
                     for s in device_facts(node)}["Bonds"])


SETTLED = {"actor_churn": "none", "partner_churn": "none", "aggregator": "1"}
good = [dict(SETTLED, name="eno1", mii="up", speed="10000 Mbps", failures="0"),
        dict(SETTLED, name="eno2", mii="up", speed="10000 Mbps", failures="0")]
check("a healthy bond says nothing alarming",
      any(w in bond_said(good) for w in ("not negotiating", "churn", "down",
                                         "aggregators")), False)
check("but its legs and speeds are still shown",
      "10000 Mbps" in bond_said(good), True)

churned = [dict(good[0]), dict(good[1], partner_churn="churned")]
said = bond_said(churned)
check("a leg that is up but churning is called out",
      "LACP has not settled on eno2" in said, True)
check("and the reason it matters is given",
      "carries nothing" in said, True)

split = [dict(good[0]), dict(good[1], aggregator="2")]
check("legs in different aggregators are not bundled",
      "2 different aggregators" in bond_said(split), True)

check("none of this is claimed for a bond that is not LACP",
      any(w in bond_said(churned, partner="00:00:00:00:00:00",
                         mode="fault-tolerance (active-backup)")
          for w in ("not negotiating", "churn", "aggregators")), False)

check("an active-backup bond with a leg down still says so",
      "1 of 2 legs are down" in bond_said(
          [dict(good[0]), dict(good[1], mii="down")],
          mode="fault-tolerance (active-backup)"), True)

print()
print("-- ports a container is holding --")
from linkfinder.render_topology import device_facts as _facts

HIDDEN = {"name": "bbu-host", "kind": "host", "state": "scanned",
          "mgmt_ips": ["198.51.100.7"],
          "extra": {"netns": {"4026536055": {
              "inode": "4026536055", "name": "bbu-65445c6fdc-wwgj8",
              "ifaces": {
                  "ens3f0np0": {"mac": "00:00:5e:00:53:00", "pci": "0000:c1:01.0",
                                "carrier": "1", "addrs": ["169.254.12.1/24"]},
                  "ens3f1np1": {"mac": "00:00:5e:00:53:01", "pci": "0000:c1:09.0",
                                "carrier": "1", "addrs": ["169.254.13.1/24"]},
                  "ens3f3np3": {"mac": "00:00:5e:00:53:02", "pci": "0000:c1:19.0",
                                "carrier": "0", "addrs": []},
                  "eth0": {"mac": "00:00:5e:00:53:10", "pci": "",
                           "carrier": "1", "addrs": ["10.0.0.5/24"]}},
              "neighbours": [{"ip": "169.254.12.11", "iface": "ens3f0np0",
                              "mac": "00:00:5e:00:53:41", "state": "REACHABLE"}]}}}}
panel = {s["title"]: s["lines"] for s in _facts(HIDDEN)}
# This used to be the other way round: the pod name was drawn along every
# such link and the panel kept quiet so as not to repeat the picture. The
# name is a replicaset hash that changes on every restart and it was the
# longest thing on the map, so it moved to where it can be read.
held = " ".join(panel.get("Ports held by something else") or [])
check("the panel names the namespace", "bbu-65445c6fdc-wwgj8" in held, True)
check("and the cards it holds", "ens3f0np0, ens3f1np1, ens3f3np3" in held, True)
check("a veth or a dummy is not a port of anything",
      "eth0" in held, False)
check("and it says why this matters",
      "the host itself cannot see" in held, True)

BLIND = {"name": "locked-down", "kind": "host", "state": "scanned",
         "mgmt_ips": ["198.51.100.8"],
         "extra": {"netns_hidden": "not root"}}
blind = {s["title"]: s["lines"] for s in _facts(BLIND)}
check("and a host we could not look inside is not given a section either",
      "Ports held inside a container" in blind, False)

print()
print("-- a worked-out link says what it was worked out from --")
from linkfinder.render_topology import link_facts as _link
from linkfinder.render_topology import unique_labels as _labels

NODES = [{"id": "a", "name": "host-a", "kind": "host", "mgmt_ips": ["198.51.100.1"]},
         {"id": "b", "name": "", "kind": "unknown", "chassis_id": "00:00:5e:00:53:41",
          "mgmt_ips": ["169.254.12.11"]},
         {"id": "c", "name": "", "kind": "unknown", "chassis_id": "00:00:5e:00:53:77",
          "mgmt_ips": ["169.254.12.11"]}]
marks = _labels(NODES)
check("two devices on the same fronthaul address are told apart",
      marks["b"] != marks["c"], True)
check("by the only thing that differs, the MAC",
      "00:00:5e:00:53:41" in marks["b"], True)

ND = {"id": "e1", "a": "a", "a_iface": "eth0", "b": "b", "b_iface": "?",
      "protos": ["ND"], "sources": ["ipv6-neighbour"], "confidence": "inferred",
      "attrs": {}, "last_seen": "2026-10-08T07:33:56"}
said = " ".join(sum([s["lines"] for s in _link(ND, marks)], []))
check("a neighbour link does not claim a forwarding table",
      "forwarding table" in said, False)
check("it says what it was worked out from",
      "what answered on the port" in said, True)

FDB = dict(ND, sources=["snmp-fdb"])
check("a forwarding-table link still says so",
      "forwarding table" in " ".join(sum([s["lines"] for s in _link(FDB, marks)],
                                         [])), True)
PTP = dict(ND, sources=["ptp"])
check("and a timing link says timing",
      "timing traffic" in " ".join(sum([s["lines"] for s in _link(PTP, marks)],
                                       [])), True)

print()
print("-- which port of which device hands the time over --")
# The whole point of the layer: a slave names its parent by clock identity,
# which is a MAC, and that MAC is a port. Where the parent is a switch the
# identity is its chassis instead, so the cable we already drew is what says
# which socket the time left by.
from linkfinder.render_topology import ptp_roles

CHAIN = {"nodes": [
    {"id": "sw", "name": "switch", "chassis_id": "00:00:5e:00:53:a0",
     "extra": {"ptp": {"1": {"state": "MASTER", "domain": 24}}}},
    {"id": "a", "name": "boxA", "extra": {
        "macs": {"eth1": "00:00:5e:00:53:11", "eth2": "00:00:5e:00:53:12"},
        "ptp": {"eth1": {"state": "SLAVE", "domain": 24, "gm_present": True,
                         "parent_port": "00005e.fffe.0053a0-7",
                         "gm_identity": "00005e.fffe.0053a0"},
                "eth2": {"state": "MASTER", "domain": 24}}}},
    {"id": "b", "name": "boxB", "extra": {
        "macs": {"eth9": "00:00:5e:00:53:21"},
        "ptp": {"eth9": {"state": "SLAVE", "domain": 24, "gm_present": True,
                         "parent_port": "00005e.fffe.005312-1",
                         "gm_identity": "00005e.fffe.005312"}}}},
], "edges": [
    {"a": "sw", "a_iface": "1/1/9", "b": "a", "b_iface": "eth1"},
]}
seats = ptp_roles(CHAIN)
check("the chained box follows the one before it",
      seats["b"]["follows"], "a")
check("naming the port the time left by", seats["b"]["out_of"], "eth2")
check("and the port it arrived on", seats["b"]["through"], "eth9")
check("a switch parent is found through its chassis",
      seats["a"]["follows"], "sw")
check("and its port comes from the cable, not the clock",
      seats["a"]["out_of"], "1/1/9")
check("the one that only hands time out is not a root of its own tree",
      seats["a"]["role"], "boundary")
check("the switch is the grandmaster", seats["sw"]["role"], "grandmaster")
links = [e["data"] for e in to_elements(CHAIN)["edges"]
         if e["data"].get("ptptree")]
check("one clock link per follower", len(links), 2)
check("labelled from port to port",
      sorted(l["label"] for l in links), ["1/1/9 -> eth1", "eth2 -> eth9"])

# One device can take time on two ports from two different clocks, and a
# port can follow another port of the same box - a loop worth seeing, not
# hiding. Holding one parent per device threw both away.
TWO = {"nodes": [
    {"id": "gps1", "chassis_id": "00:00:5e:00:53:f0"},
    {"id": "gps2", "chassis_id": "00:00:5e:00:53:f1"},
    {"id": "h", "name": "twinfed", "extra": {
        "macs": {"eth1": "00:00:5e:00:53:31", "eth2": "00:00:5e:00:53:32"},
        "ptp": {"eth1": {"state": "SLAVE", "domain": 24,
                         "parent_port": "00005e.fffe.0053f0-1"},
                "eth2": {"state": "SLAVE", "domain": 24,
                         "parent_port": "00005e.fffe.0053f1-1"},
                "eth3": {"state": "SLAVE", "domain": 24,
                         "parent_port": "00005e.fffe.005331-1"}}}},
], "edges": []}
seat = ptp_roles(TWO)["h"]
check("every slave port keeps its own parent", len(seat["parents"]), 3)
check("two different clocks feed the same device",
      sorted({p["follows"] for p in seat["parents"]}), ["gps1", "gps2", "h"])
check("a port following another port of the same box is kept",
      [p["through"] for p in seat["parents"] if p["follows"] == "h"], ["eth3"])
check("and it names the port the time left by",
      [p["out_of"] for p in seat["parents"] if p["follows"] == "h"], ["eth1"])
many = [e["data"] for e in to_elements(TWO)["edges"] if e["data"].get("ptptree")]
check("one clock link per slave port, not per device", len(many), 3)
check("each link has its own id", len({e["id"] for e in many}), 3)

print()
print("-- how to reach a device the scan could not --")
from linkfinder.render_topology import device_facts as _facts2
from linkfinder.render_topology import off_net, reach_plans

def _space(pod, pid, port, mac, ip):
    return {pid: {"inode": pid, "name": pod, "pid": pid,
                  "ifaces": {port: {"mac": "00:00:5e:00:53:ff",
                                    "pci": "0000:70:01.0"}},
                  "neighbours": [{"ip": ip, "iface": port, "mac": mac,
                                  "state": "REACHABLE"}]}}

# Two base stations, each numbering its own radio 169.254.12.11. Keying the
# route on the address would send the reader to whichever host came last.
TWINS = {"nodes": [
    {"id": "h1", "name": "bench-c", "state": "scanned",
     "mgmt_ips": ["198.51.100.35"],
     "extra": {"netns": _space("bbu-one", "3113225", "ens1f0np0",
                               "00:00:5e:00:53:20", "169.254.12.11")}},
    {"id": "h2", "name": "bench-b", "state": "scanned",
     "mgmt_ips": ["198.51.100.200"],
     "extra": {"netns": _space("bbu-two", "1265316", "ens3f0np0",
                               "00:00:5e:00:53:41", "169.254.12.11")}},
    {"id": "r1", "name": "", "state": "unscanned", "kind": "unknown",
     "chassis_id": "00:00:5e:00:53:20", "mgmt_ips": ["169.254.12.11"]},
    {"id": "r2", "name": "", "state": "unscanned", "kind": "unknown",
     "chassis_id": "00:00:5e:00:53:41", "mgmt_ips": ["169.254.12.11"]},
]}
plans = reach_plans(TWINS)
check("each radio is routed to its own host",
      (plans["r1"]["held"]["host"], plans["r2"]["held"]["host"]),
      ("bench-c", "bench-b"))
check("with its own container", plans["r1"]["held"]["pod"], "bbu-one")
check("and its own process to enter", plans["r2"]["held"]["pid"], "1265316")
check("a scanned host needs no plan of its own", "h1" in plans, False)

said = " ".join({s["title"]: s["lines"] for s in
                 _facts2(TWINS["nodes"][2], plans["r1"])}
                .get("How to reach it", []))
check("the hop onto the host is spelled out", "ssh 198.51.100.35" in said, True)
check("and the hop into the container", "nsenter -t 3113225 -n" in said, True)
check("the address is carried through", "169.254.12.11" in said, True)
check("it says the address is useless from anywhere else",
      "reachable only inside bbu-one" in said, True)
check("and warns the process id goes stale",
      "changes when the container restarts" in said, True)
check("without a line nobody reads", len(said) < 200, True)

print()
print("-- the other kinds of unreachable --")
REST = {"nodes": [
    {"id": "d", "name": "", "state": "unreachable", "kind": "host",
     "mgmt_ips": ["198.51.100.73"]},
    {"id": "l", "name": "sw", "state": "unscanned", "kind": "switch",
     "mgmt_ips": ["fe80::7a9a:18ff:fe11:2e7"]},
    {"id": "n", "name": "sw2", "state": "unscanned", "kind": "switch",
     "chassis_id": "00:00:5e:00:53:90", "mgmt_ips": []},
]}
rest = reach_plans(REST)


def words(node_id, index):
    return " ".join({s["title"]: s["lines"] for s in
                     _facts2(REST["nodes"][index], rest[node_id])}
                    .get("How to reach it", []))


check("a routable address is just the command",
      words("d", 0).strip(), "ssh 198.51.100.73")
check("a link-local-only switch is honest about it, in one line",
      words("l", 1), "fe80::7a9a:18ff:fe11:2e7 is not reachable from here")
# A device with no address has nothing to say under this heading, so the
# heading does not appear. Explaining an absence is the noise we removed.
check("a device with no address gets no such section", words("n", 2), "")

check("a link-local address is off the network", off_net("169.254.12.11"), True)
check("so is fe80", off_net("fe80::1"), True)
check("a real one is not", off_net("198.51.100.1"), False)
check("and nothing is not an address", off_net(""), True)

print()
print("-- a port nobody reported a neighbour on --")

QUIET = {"nodes": [
    {"id": "ip:10.0.0.1", "kind": "host", "name": "serverA", "state": "scanned",
     "mgmt_ips": ["10.0.0.1"],
     "extra": {"macs": {"eno1": "aa:bb:cc:00:00:01"},
               "ports": {"eno1": {"role": "mgmt", "state": "up",
                                  "addresses": ["10.0.0.1/24"]}}}},
    {"id": "ip:10.0.0.2", "kind": "host", "name": "serverB", "state": "scanned",
     "mgmt_ips": ["10.0.0.2"],
     "extra": {"macs": {"oam": "aa:bb:cc:00:00:02",
                        "ens1": "aa:bb:cc:00:00:03",
                        "ens2": "aa:bb:cc:00:00:04"},
               "ports": {"oam": {"role": "mgmt", "state": "up",
                                 "addresses": ["10.0.0.2/24"]},
                         "ens1": {"role": "physical", "state": "up",
                                  "addresses": ["fe80::1/64"]},
                         "ens2": {"role": "physical", "state": "down",
                                  "addresses": []},
                         "br0": {"role": "physical", "state": "up",
                                 "addresses": []}}}},
    {"id": "chassis:11:22:33:44:55:66", "kind": "switch", "name": "top-of-rack",
     "chassis_id": "11:22:33:44:55:66", "state": "unscanned", "extra": {}}],
    "edges": [{"id": "e1", "a": "chassis:11:22:33:44:55:66", "a_iface": "1/1/7",
               "b": "ip:10.0.0.1", "b_iface": "eno1",
               "confidence": "reported", "protos": ["LLDP"], "attrs": {}}]}

quiet = {n["data"]["label"]:
         {s["title"]: s["lines"] for s in n["data"]["facts"]}
         for n in to_elements(QUIET)["nodes"]}
SILENT = "Ports with no neighbour found"

check("a port that was found needs no explaining",
      SILENT in quiet["serverA"], False)
check("a port that was not is named",
      any(line.startswith("oam") for line in quiet["serverB"][SILENT]), True)
check("so is a data port with a cable and nothing on it",
      any(line.startswith("ens1") for line in quiet["serverB"][SILENT]), True)
check("a port that is down is expected to be quiet",
      any(line.startswith("ens2") for line in quiet["serverB"][SILENT]), False)
check("and a bridge is not a port anybody can patch",
      any(line.startswith("br0") for line in quiet["serverB"][SILENT]), False)
check("the switch the neighbours on that network named is offered",
      any("others on 10.0.0.0/24 report top-of-rack" in line
          for line in quiet["serverB"][SILENT]), True)
check("but only where there is a network to share",
      any("others on" in line and "ens1" in line
          for line in quiet["serverB"][SILENT]), False)


print()
print("-- physical shows every link, PTP colours the clocks on top of it --")
from linkfinder.render_topology import _asset
PAGE = _asset("topology.html")
check("a clock link is drawn on the physical layer, so a grandmaster "
      "is never left hanging",
      "selector: 'edge[ptptree = 1]', style: { 'display': 'none' }" in PAGE, False)
check("but not where it would repeat a cable already drawn",
      "'edge[ptptree = 1][dup = 1]', style: { 'display': 'none' }" in PAGE, True)
check("the PTP layer keeps a device without a clock, drawn pale",
      "'node.ptp[ptpOff = 1]', style: { 'display': 'none' }" in PAGE, False)
check("and keeps the cabling in grey, so an uplink can still be read",
      "'line-color': '#cbd5e0', 'width': 2, 'line-style': 'solid' } }," in PAGE, True)

from linkfinder.render_topology import ptp_tree
ROLES = {"host": {"role": "SLAVE", "parents": [
    {"follows": "gm", "through": "eno1", "out_of": "", "under": 24},
    {"follows": "sw", "through": "eno2", "out_of": "", "under": 24},
    {"follows": "host", "through": "eno3", "out_of": "", "under": 24}]}}
TREE = {e["data"]["source"]: e["data"]["dup"]
        for e in ptp_tree(ROLES, {}, {frozenset(("sw", "host"))})}
check("a clock link with no cable of its own shows on both layers",
      TREE["gm"], 0)
check("one that repeats a cable is left to the PTP layer",
      TREE["sw"], 1)
check("and a port taking time from its own device is never a cable",
      TREE["host"], 1)

print()
print("-- a device we only have a MAC for is still drawn as its hardware --")
from linkfinder.render_topology import device_facts, icon_uris, to_elements
GM = {"nodes": [{"id": "chassis:00:17:47:70:4c:4a", "kind": "unknown",
                 "chassis_id": "00:17:47:70:4c:4a", "state": "unscanned"},
                {"id": "chassis:00:11:22:33:44:55", "kind": "unknown",
                 "chassis_id": "00:11:22:33:44:55", "state": "unscanned"}],
      "edges": []}
DREW = {n["data"]["id"]: n["data"] for n in to_elements(GM)["nodes"]}
check("a clock maker is drawn as a timing source, not a question mark",
      DREW["chassis:00:17:47:70:4c:4a"]["icon"], "timing")
check("and the icon set carries that drawing",
      "timing" in icon_uris(), True)
check("a maker the register does not know stays a question mark",
      DREW["chassis:00:11:22:33:44:55"]["icon"], "unknown")
LINES = " ".join(device_facts(GM["nodes"][0])[0]["lines"])
check("the panel says the icon came from the maker, not from the device",
      "guessed from the maker" in LINES, True)
check("and says so in one line, under the kind it already shows",
      "nothing on the device was asked" in LINES, True)

print()
print("-- a search answers for what a device is, not what it has heard of --")
from linkfinder.render_topology import _identity_text
SEEN = {"label": "server4", "name": "server4", "mgmt_ips": ["203.0.113.183"],
        "chassis_id": "", "extra": {
            "ports": {"eno1": {"mac": "02:00:00:00:00:04",
                               "addresses": ["203.0.113.183/24"]}},
            "macs": {"eno1": "02:00:00:00:00:04"},
            "optics": {"eno1": {"part": "AFBR-709", "serial": "OPT12345"}},
            "neighbours": {"203.0.113.180": {"mac": "02:00:00:00:00:01",
                                             "dev": "eno1"}},
            "dhcp": {"02:00:00:00:00:09": {"address": "169.254.12.11"}}}}
TEXT = _identity_text(SEEN)
check("its own address answers", "203.0.113.183" in TEXT, True)
check("its own port answers", "eno1" in TEXT, True)
check("its own mac answers", "02:00:00:00:00:04" in TEXT, True)
check("an optic it carries answers", "opt12345" in TEXT, True)
check("a neighbour's address in its ARP cache does not",
      "203.0.113.180" in TEXT, False)
check("nor an address it merely hands out",
      "169.254.12.11" in TEXT, False)
check("the page filters on that field, not on the whole record",
      "(data.search || '').indexOf(q)" in PAGE, True)
check("and still counts the devices that only mention it",
      "more only mention it" in PAGE, True)

print()
print("-- a handset hangs off a USB port, and which one matters --")
from linkfinder.render_topology import USB_STYLE, link_facts, usb_generation, usb_note
def usb_edge(version, speed):
    return {"a": "x", "b": "y", "a_iface": "usb1-4", "b_iface": "adb",
            "protos": ["USB"], "sources": ["adb"], "reported_by": ["x"],
            "confidence": "confirmed", "attrs": {"usb": {"version": version,
                                                         "speed": speed}}}
check("a port that negotiated 5 Gbit/s is USB 3",
      usb_generation(usb_edge("3.20", 5000)), "USB 3")
check("one that negotiated 480 Mbit/s is USB 2",
      usb_generation(usb_edge("2.00", 480)), "USB 2")
check("anything slower is called out as USB 1",
      usb_generation(usb_edge("1.10", 12)), "USB 1")
check("the rate decides, not the claim",
      usb_generation(usb_edge("3.20", 480)), "USB 2")
check("and a device connected below what it can do is told on",
      "connected as USB 2" in usb_note(usb_edge("3.20", 480)), True)
check("without quoting a rate nobody asked for",
      "Mbit" in usb_note(usb_edge("3.20", 480)), False)
check("a link that kept its promise says nothing extra",
      usb_note(usb_edge("3.20", 5000)), "")
check("a full-speed device is not accused: 2.00 never promised 480",
      usb_note(usb_edge("2.00", 12)), "")
check("USB 1 is drawn plainly, not as a warning", USB_STYLE["USB 1"], "#7f7f7f")
check("USB 2 is black", USB_STYLE["USB 2"], "#111111")
check("USB 3 is blue", USB_STYLE["USB 3"], "#0057d9")

from linkfinder.render_topology import KIND_SIZE, KIND_STYLE, icon_uris
drawn = icon_uris()
check("every kind the map can draw has an icon of its own",
      sorted(k for k in KIND_STYLE if k not in drawn), [])
check("and a size", sorted(k for k in KIND_STYLE if k not in KIND_SIZE), [])
check("a console cable and a usb device are kinds now",
      ("serial" in KIND_STYLE, "peripheral" in KIND_STYLE), (True, True))
check("an ordinary cable is not a USB link",
      usb_generation({"protos": ["LLDP"], "attrs": {}}), "")
check("USB 2 is drawn black", USB_STYLE["USB 2"], "#111111")
check("USB 3 is drawn blue", USB_STYLE["USB 3"], "#0057d9")
LINES = " ".join(l for sec in link_facts(usb_edge("3.20", 480), {}) for l in sec["lines"])
check("and the panel names the generation", "Carried over" in LINES, True)

print()
print("-- the page a browser gets has to parse, or nothing on it works --")
import io
import subprocess
import tempfile
PLACEHOLDERS = ("__TOPOLOGY__", "__ELEMENTS__", "__KIND_STYLE__", "__EDGE_STYLE__",
                "__USB_STYLE__", "__ICONS__", "__PTP_STYLE__", "__VERDICT_STYLE__")
script = PAGE.split("<script>")[-1].split("</script>")[0]
for name in PLACEHOLDERS:
    script = script.replace(name, "{}")
JS = os.path.join(tempfile.gettempdir(), "lf_page_check.js")
io.open(JS, "w", encoding="utf-8").write(script)
try:
    run = subprocess.run(["node", "--check", JS], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
except OSError:
    # A check that quietly disappears is not a check. On a laptop without
    # node it is fair to skip; on a build machine it must be an error, which
    # is what LINKFINDER_REQUIRE_NODE is for.
    if os.environ.get("LINKFINDER_REQUIRE_NODE"):
        FAILURES.append("no javascript engine, and this machine was told to "
                        "require one (LINKFINDER_REQUIRE_NODE)")
        print("%-52s %s" % ("a real engine parses and runs the page", "FAIL"))
    else:
        print("skipped: no javascript engine on this machine")
else:
    check("a real engine parses it", run.returncode, 0)

    # Parsing is not running. A const used before its declaration parses
    # perfectly and then throws at load, which blanked the whole map once while
    # every suite stayed green. So the page's top level is executed as well.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.page_runs import run as page_runs
    # The two nodes carry the extra the crawl really puts on them, which is
    # NOT the same shape on both: a host files a device per port, a handset
    # files its own record, flat. A panel builder that read the second as the
    # first threw, and a panel that throws takes the whole page with it - the
    # tool drew no map at all. The fixture had no extra, so nothing saw it.
    live = render_html(
        {"nodes": [{"id": "ip:198.51.100.10", "name": "nuc1", "kind": "host",
                    "state": "scanned", "mgmt_ips": ["198.51.100.10"],
                    "extra": {"usb": {"1-2": {"port": "1-2", "kind": "phone",
                                              "product": "ONEPLUS A5000",
                                              "serial": "aaa1"},
                                      "1-1": {"port": "1-1",
                                              "kind": "peripheral",
                                              "product": "a keyboard"}}}},
                   {"id": "chassis:usb:aaa1", "name": "a handset",
                    "kind": "phone", "state": "scanned",
                    "extra": {"usb": {"port": "1-2", "kind": "phone",
                                      "product": "ONEPLUS A5000",
                                      "serial": "aaa1", "version": "2.00"}}}],
         "edges": [{"id": "e1", "a": "ip:198.51.100.10", "a_iface": "1-2",
                    "b": "chassis:usb:aaa1", "b_iface": "usb",
                    "protos": ["usb"], "sources": ["usb"],
                    "attrs": {"usb": {"version": "2.00", "speed_mbps": 480}}}]},
        library=STUB)
    check("and the page's own script actually runs", page_runs(live), "")

    # Assigning a handler proves nothing - the blank map parsed perfectly.
    # Press every button the page has, and fail if one of them throws. The
    # NetBox strip that used to be pressed here is gone: its answers are
    # written as CSV beside the map now, so the page is the map again.
    from tools.page_runs import HARNESS, page_script

    pressable = render_html(
        {"nodes": [{"id": "ip:198.51.100.5", "name": "a host", "kind": "host",
                    "state": "scanned", "mgmt_ips": ["198.51.100.5"]}],
         "edges": []}, library=STUB, live="/m/test")
    PRESS = ("var pressed = [];" + chr(10)
             + "['pickmark', 'pickunmark', 'pickclear'].forEach(function (id) {"
             + chr(10)
             + "  var b = document.getElementById(id);" + chr(10)
             + "  if (!b || typeof b.onclick !== 'function') { return; }"
             + chr(10)
             + "  try { b.onclick({ preventDefault: function () {} });"
             + " pressed.push(id); }" + chr(10)
             + "  catch (e) { console.log('THREW on ' + id); }" + chr(10)
             + "});" + chr(10)
             + "console.log('pressed:' + pressed.join(','));" + chr(10))
    where = tempfile.mkdtemp(prefix="lf-press-")
    js = os.path.join(where, "page.js")
    io.open(js, "w", encoding="utf-8").write(
        HARNESS + chr(10) + page_script(pressable) + chr(10) + PRESS)
    pressed = subprocess.run([ "node", js], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    told = (pressed.stdout or b"").decode("utf-8", "replace")
    check("every button on the page can be pressed",
          "pressed:pickmark,pickunmark,pickclear" in told, True)
    check("and none of them throws", "THREW" in told, False)
    check("and the strip that was taken out left no button behind",
          pressable.count('<button id="nb'), 0)


    # The legend, read off the page rather than off the source. The stub DOM
    # swallows everything, so it cannot say what was appended; this one
    # records it. Asked for from the bench: "there is USB 2 but no USB 3" -
    # the entry was hidden when nothing used it, which reads as unsupported
    # rather than absent. Every generation is listed now, the unused ones
    # greyed, and only a running page can show that it really happens.
    RECORDER = """
function elem(tag) {
  const store = { tagName: tag, kids: [] };
  return new Proxy(store, {
    get(t, name) {
      if (name in t) { return t[name]; }
      if (name === 'style' || name === 'classList' || name === 'dataset') {
        t[name] = {}; return t[name];
      }
      if (name === 'appendChild') { return function (c) { t.kids.push(c); }; }
      return anything;
    },
    set(t, name, value) { t[name] = value; return true; },
  });
}
const byId = {};
globalThis.document = new Proxy(function () {}, {
  get(target, name) {
    if (name === 'getElementById') {
      return function (id) { return byId[id] || (byId[id] = elem(id)); };
    }
    if (name === 'createElement') { return elem; }
    return anything;
  },
  apply() { return anything; },
  set() { return true; },
});
"""
    REPORT = ("const seen = (byId.legend ? byId.legend.kids : [])" + chr(10)
              + "  .filter(function (k) { return /USB/.test(k.innerHTML); })"
              + chr(10)
              + "  .map(function (k) {" + chr(10)
              # An unset property answers with the stub, which is truthy - so
              # the class has to be asked for by type, not for truth.
              + "    const cls = typeof k.className === 'string' && k.className"
              + " ? k.className : 'shown';" + chr(10)
              + "    return cls + ' '"
              + " + k.innerHTML.replace(/<[^>]*>/g, '') + ' | ' + k.title;"
              + chr(10) + "  });" + chr(10)
              + "console.log(JSON.stringify(seen));" + chr(10))
    where = tempfile.mkdtemp(prefix="lf-legend-")
    js = os.path.join(where, "page.js")
    io.open(js, "w", encoding="utf-8").write(
        HARNESS + chr(10) + RECORDER + chr(10) + page_script(live) + chr(10)
        + REPORT)
    told = subprocess.run(["node", js], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    try:
        entries = json.loads((told.stdout or b"[]").decode("utf-8").strip()
                             .splitlines()[-1])
    except (ValueError, IndexError):
        entries = []
        FAILURES.append("the legend could not be read back: %s"
                        % (told.stderr or b"").decode("utf-8", "replace")[:300])
    shown = [e.split(" ", 1)[1].split(" |")[0] for e in entries
             if e.startswith("shown ")]
    greyed = [e.split(" ", 1)[1].split(" |")[0] for e in entries
              if e.startswith("absent ")]
    check("every USB generation is in the legend",
          sorted(shown + greyed), ["USB", "USB 1", "USB 2", "USB 3"])
    check("the one this map actually has is shown plainly", shown, ["USB 2"])
    check("and the three it has not are greyed, not gone",
          sorted(greyed), ["USB", "USB 1", "USB 3"])
    check("a greyed one says why it is empty",
          all("nothing on this map" in e for e in entries
              if e.startswith("absent ")), True)
    check("and each says what the speed means",
          any("5 Gb/s or faster" in e for e in entries), True)

    # "go to" says "name, address or serial" and meant only the whole composed
    # line - "server1   10.0.0.5   SN ABC". Typing the address did nothing at
    # all, silently, which is what was reported from the bench. Driven here
    # through a stub that hands the page its OWN nodes: the plain stub walks
    # nothing, so the index stays empty and every answer looks the same.
    from tools.page_runs import REAL_NODES

    GOTO_MAP = {
        "nodes": [
            {"id": "ip:192.0.2.10", "name": "alpha", "kind": "host",
             "state": "scanned", "mgmt_ips": ["192.0.2.10"],
             "serial": "SN0000000A"},
            {"id": "ip:192.0.2.11", "name": "beta", "kind": "host",
             "state": "scanned", "mgmt_ips": ["192.0.2.11"],
             "serial": "SN0000000B"},
            # Two of a name: the label carries the address to tell them apart,
            # so the bare name is a question with two answers.
            {"id": "ip:192.0.2.20", "name": "twin", "kind": "host",
             "state": "scanned", "mgmt_ips": ["192.0.2.20"]},
            {"id": "ip:192.0.2.21", "name": "twin", "kind": "host",
             "state": "scanned", "mgmt_ips": ["192.0.2.21"]},
        ],
        "edges": [],
    }
    ASK = ("[['192.0.2.10'], ['  192.0.2.11  '], ['SN0000000A'],"
           " ['sn0000000b'], ['alpha'], ['bet'], ['twin'], ['192.0.2.99'],"
           " ['']].forEach(function (pair) {\n"
           "  console.log('GOTO ' + pair[0] + ' => ' + (findTyped(pair[0]) || 'none'));\n"
           "});\n")
    where = tempfile.mkdtemp(prefix="lf-goto-")
    js = os.path.join(where, "page.js")
    io.open(js, "w", encoding="utf-8").write(
        HARNESS + chr(10) + REAL_NODES + chr(10)
        + page_script(render_html(GOTO_MAP, library=STUB)) + chr(10) + ASK)
    asked = subprocess.run(["node", js], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    answers = dict(
        line[5:].split(" => ", 1) for line in
        (asked.stdout or b"").decode("utf-8", "replace").splitlines()
        if line.startswith("GOTO "))
    check("an address on its own finds the device",
          answers.get("192.0.2.10"), "ip:192.0.2.10")
    check("even typed with spaces round it",
          answers.get("  192.0.2.11  "), "ip:192.0.2.11")
    check("a serial finds it too", answers.get("SN0000000A"), "ip:192.0.2.10")
    check("in whatever case it was typed",
          answers.get("sn0000000b"), "ip:192.0.2.11")
    check("so does the name", answers.get("alpha"), "ip:192.0.2.10")
    check("and the start of a name, when only one device begins that way",
          answers.get("bet"), "ip:192.0.2.11")
    # Guessing between two machines is worse than doing nothing: the browser
    # still offers both full lines as you type.
    check("a name two devices share answers with neither",
          answers.get("twin"), "none")
    check("an address nothing has finds nothing",
          answers.get("192.0.2.99"), "none")
    check("and an empty box is not a search", answers.get(""), "none")

    # The negative control: a check that cannot fail is worth nothing.
    anchor = "const legend = document.getElementById('legend');"
    broken = live.replace(anchor, "tooSoon.edges();" + chr(10)
                          + "const tooSoon = 1;" + chr(10) + anchor, 1)
    said = page_runs(broken)
    check("and it catches a value used before it exists",
          "before initialization" in said or "not defined" in said, True)
    if run.returncode:
        print(run.stderr.decode("utf-8", "replace")[:400])

print()
print("-- a bench mini-PC is drawn apart from a rack server --")
# Both are hosts to the topology - a NUC keeps the host kind so nothing in
# the graph logic changes - but a bench mini-PC and a rack server should not
# wear the same glyph. The product names are off the real bench.
from linkfinder.render_topology import _is_nuc, icon_uris
NUCS = [("Dell Pro Micro QCM1250", "Dell Inc."),
        ("OptiPlex Micro 7020", "Dell Inc."),
        ("NUC13ANKi7", "Intel(R) Client Systems")]
for product, vendor in NUCS:
    node = {"kind": "host",
            "extra": {"hardware": {"product_name": product, "sys_vendor": vendor}}}
    check("%s is a NUC" % product[:24], _is_nuc(node), True)
check("a rack server is not",
      _is_nuc({"kind": "host", "extra": {"hardware":
              {"product_name": "PowerEdge R640", "sys_vendor": "Dell Inc."}}}),
      False)
check("and a switch is never a NUC whatever it is called",
      _is_nuc({"kind": "switch", "extra": {"hardware":
              {"product_name": "mini pc"}}}), False)
check("the NUC glyph is embedded in the page", "nuc" in icon_uris(), True)
nuc_map = render_html({"nodes": [
    {"id": "ip:1", "kind": "host", "name": "bench", "state": "scanned",
     "mgmt_ips": ["198.51.100.1"],
     "extra": {"hardware": {"product_name": "OptiPlex Micro 7020",
                            "sys_vendor": "Dell Inc."}}},
    {"id": "ip:2", "kind": "host", "name": "rack", "state": "scanned",
     "mgmt_ips": ["198.51.100.2"],
     "extra": {"hardware": {"product_name": "PowerEdge R640"}}}],
    "edges": []}, library=STUB)
drawn = json.loads(nuc_map.split("const ELEMENTS = ")[1].split("\n")[0]
                       .rstrip().rstrip(";"))
icons = {n["data"]["name"]: (n["data"]["kind"], n["data"]["icon"])
         for n in drawn["nodes"]}
check("the NUC keeps the host kind but gets the nuc glyph",
      icons["bench"], ("host", "nuc"))
check("the rack server stays on the host glyph",
      icons["rack"], ("host", "host"))

print()
print("-- a device can be picked by name, however many there are --")
check("the map offers a go-to box", "id=\"goto\"" in PAGE, True)
check("and it can be typed into, not only scrolled",
      'list="devicelist"' in PAGE and "<datalist" in PAGE, True)
check("and the line says which one, when two share a name",
      "n.data('mgmt_ips') || [])[0] || n.data('chassis_id')" in PAGE, True)
check("the serial is on the line, because an inventory is keyed by it",
      "'SN ' + n.data('serial')" in PAGE, True)
check("picking one centres the map on it", "center: { eles: node }" in PAGE, True)

# A device with a serial must carry it into the page, or typing one finds nothing.
withsn = render_html({"nodes": [{"id": "ip:198.51.100.10", "name": "nuc1",
                                 "kind": "host", "state": "scanned",
                                 "serial": "S900001X1234567",
                                 "mgmt_ips": ["198.51.100.10"]}],
                      "edges": []}, library=STUB)
check("a serial reaches the page", "S900001X1234567" in withsn, True)
# The search text is folded to lower case so typing a serial off a label
# matches whatever case it was printed in.
check("and the search matches on it, in any case",
      "s900001x1234567" in json.loads(
          withsn.split("const ELEMENTS = ")[1].split(chr(10))[0].rstrip(";")
      )["nodes"][0]["data"]["search"], True)


print()
print("-- marking a setup on the map itself --")
check("the map carries a selection bar", 'id="pick"' in PAGE, True)
check("with a name box that offers the ones already there",
      'list="setupnames"' in PAGE, True)
check("there is nothing to save, so no button to save it",
      'id="picksave"' in PAGE, False)
check("the buttons are dead until something is selected",
      "document.getElementById('pickmark').disabled" in PAGE, True)
check("marking is a mode, not something that happens on a click",
      'id="markmode"' in PAGE and "checked> mark setups" not in PAGE, True)
check("no marks file is produced at all",
      "'linkfinder-marks'" in PAGE, False)

# A browser is the only thing that can judge this: the marking code only means
# anything together with Cytoscape's own selection.
marked = render_html(
    {"nodes": [{"id": "ip:198.51.100.10", "name": "nuc1", "kind": "host",
                "state": "scanned", "serial": "ABC1234",
                "mgmt_ips": ["198.51.100.10"], "setups": ["from netbox"]},
               {"id": "chassis:usb:aaa1", "name": "a handset", "kind": "phone",
                "state": "scanned", "serial": "aaa1"}],
     "edges": []},
    # the real library (a stub cannot select anything), served as the window
    # would serve it - a saved copy has no marking at all
    live="/m/testtoken")
try:
    from tools.page_marks import drive
    got = drive(marked, name="bench one", count=2)
    if "error" in got:
        raise RuntimeError(got["error"])
except Exception as problem:
    print("skipped: %s" % str(problem)[:90])
else:
    check("marking is offered when the window is answering",
          got["markingOffered"], True)
    check("but it is off until it is asked for", got["modeOffByDefault"], True)
    check("so clicking devices does not make anything pop up",
          got["quietUntilAsked"], True)
    check("turning it on shows the strip", got["barShown"], True)
    check("it counts what is selected", got["counted"], "2")
    check("it says what those devices are already in, rather than asking again",
          "already in" in got["already"], True)
    # It reports what happened, not what you still have to do. There is no
    # "remember to save" any more because there is nothing left to save.
    check("it says the change was written", got["said"], "saved")
    check("and never asks you to do anything about it",
          "remember" in got["said"] or "not saved" in got["said"], False)
    check("the filter offers the new name straight away",
          "bench one" in got["filterHasName"], True)
    check("beside the one NetBox already gave",
          "from netbox" in got["filterHasName"], True)
    sent = got["sent"]
    check("each marked device is sent to the window", len(sent), 2)
    check("to the address the window gave the page",
          all(s["url"].startswith("/m/testtoken/setups") for s in sent), True)
    first = [s["body"] for s in sent if s["body"].get("serial") == "ABC1234"][0]
    check("carrying the serial, which is what finds it again later",
          first["serial"], "ABC1234")
    check("and the setup that came from NetBox is kept beside the new one",
          sorted(first["setups"]), ["bench one", "from netbox"])


print()
print("-- a setup is written by the tool, not by a file --")
# The page used to download what you marked so somebody could import it. That
# was ceremony born of a limitation, and it produced copies nobody wanted.
check("nothing is downloaded", "showSaveFilePicker" in PAGE, False)
check("no handle is remembered", "indexedDB" in PAGE, False)
check("no copy is kept in the browser", "localStorage" in PAGE, False)
check("and closing the tab asks nothing", "beforeunload" in PAGE, False)
check("a change is sent to the window instead",
      "LIVE + '/setups'" in PAGE, True)
check("it carries enough for the tool to recognise the device later",
      "serial: node.data('serial')" in PAGE, True)
check("a failure to save is never dressed up as a save",
      "NOT saved" in PAGE, True)
check("and a saved copy of the page cannot mark at all",
      "if (LIVE) { document.getElementById('markbox')" in PAGE, True)

print()
print("-- a device is put in a setup from its own panel --")
check("the panel carries a setup field", "id=\"setupedit\"" in PAGE or
      "setupedit" in PAGE, True)
check("with the names already there as chips", "setupchips" in PAGE, True)
check("a chip can be taken off again", "take it out" in PAGE, True)
check("the box offers the setups that exist", 'list="setupnames"' in PAGE, True)
check("Enter works, not only the button", "e.key === 'Enter'" in PAGE, True)
check("a name typed there is escaped before it reaches the page",
      "function escapeText" in PAGE, True)
check("and editing one device is written by the window, like any other mark",
      "tellTheTool(node);" in PAGE.split("function setSetups")[1][:400], True)
check("the panel driver is part of the tree",
      os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
          os.path.abspath(__file__))), "tools", "page_panel.py")), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all render checks passed")

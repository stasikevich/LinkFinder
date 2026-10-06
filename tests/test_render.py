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

from linkfinder.render_topology import (KIND_SIZE, KIND_STYLE, assets_dir, embed_json,
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
check("no fetch calls", "fetch(" in page, False)
check("no XMLHttpRequest", "XMLHttpRequest" in page, False)
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
check("one data URI per kind", sorted(uris), sorted(KIND_STYLE))
check("they are base64 SVG data URIs",
      all(v.startswith("data:image/svg+xml;base64,") for v in uris.values()), True)
check("no two kinds share a drawing", len(set(uris.values())), len(KIND_STYLE))
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
    check("future schema refused", "schema_version" in str(exc), True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("all render checks passed")

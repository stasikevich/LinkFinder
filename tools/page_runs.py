"""Run a generated topology page's own script, to catch what parsing cannot.

`node --check` proves a page PARSES. It can never catch a const used before its
declaration: that is a runtime ReferenceError, and it blanked the whole map once
while every suite stayed green. So the page's script is executed here under a
stub DOM and a stub Cytoscape - far enough to run every top-level statement,
which is where that class of fault lives.

Nothing is drawn and nothing is fetched; the stubs answer any call.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile

SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)

# Anything the page asks for answers, and answers with something that can be
# asked again. That is enough to run the top level without a browser.
HARNESS = """
const anything = new Proxy(function () {}, {
  get(target, name) {
    if (name === 'length') { return 0; }
    if (name === 'style' || name === 'dataset' || name === 'classList') { return anything; }
    if (name === Symbol.toPrimitive || name === 'toString') { return () => ''; }
    if (name === Symbol.iterator) { return [][Symbol.iterator].bind([]); }
    return anything;
  },
  apply() { return anything; },
  set() { return true; },
});
// Assigned, never declared: a page may carry its own `var cytoscape` stub, and
// a lexical declaration here would collide with it before anything ran.
globalThis.document = anything;
globalThis.window = anything;
globalThis.navigator = anything;
globalThis.location = anything;
globalThis.cytoscape = function () { return anything; };
"""


# The page's own script is the one that wires the page up. Picking it by size
# instead was wrong in the direction that hides faults: against a stubbed
# library the page script IS the longest block, so the check ran the stub and
# could not fail. Pick it by what only it contains.
OURS = "getElementById('legend')"


def page_script(html: str) -> str:
    """The page's own script, never the bundled library beside it."""
    blocks = SCRIPT.findall(html)
    if not blocks:
        raise ValueError("the page carries no script at all")
    mine = [b for b in blocks if OURS in b]
    if not mine:
        raise ValueError("no script on this page builds the legend - is it a "
                         "topology page at all?")
    return "\n".join(mine)


def run(html: str, node: str = "node") -> str:
    """Return "" when the page's top level runs, or the error it raised."""
    where = tempfile.mkdtemp(prefix="lf-page-")
    path = os.path.join(where, "page.js")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(HARNESS + "\n" + page_script(html) + "\n")
    try:
        done = subprocess.run([node, path], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as problem:
        return "could not run node: %s" % problem
    if done.returncode == 0:
        return ""
    return (done.stderr or done.stdout or "exit %d" % done.returncode).strip()


# A graph library that hands back the page's OWN nodes. The plain stub in
# HARNESS answers every call with a proxy, which is enough to prove the page
# runs and useless for proving what it does: code that walks cy.nodes() walks
# nothing, so an index built from them stays empty and a check against it
# passes while measuring an empty answer. This returns real node objects and
# falls back to the proxy for the hundred methods a test does not care about.
REAL_NODES = """
function lfWrap(own) {
  // The fallback has to answer with the PROXY, not the bare object, or a
  // chained call - cy.layout(...).one(...) - walks off the end of the stub.
  const proxy = new Proxy(own, {
    get(t, name) {
      if (name in t) { return t[name]; }
      if (typeof name === 'string') { return function () { return proxy; }; }
      return anything;
    },
  });
  return proxy;
}
function lfCollection(items) {
  return lfWrap({
    length: items.length,
    forEach: Array.prototype.forEach.bind(items),
    map: Array.prototype.map.bind(items),
    filter: function (f) { return lfCollection(items.filter(f)); },
    sort: function (f) { return lfCollection(items.slice().sort(f)); },
    empty: function () { return items.length === 0; },
    first: function () { return items[0]; },
  });
}
function lfNode(data) {
  return lfWrap({
    data: function (k) { return k === undefined ? data : data[k]; },
    id: function () { return data.id; },
    empty: function () { return false; },
    position: function () { return { x: 0, y: 0 }; },
  });
}
globalThis.cytoscape = function (opts) {
  const made = (((opts || {}).elements || {}).nodes || [])
    .map(function (n) { return lfNode(n.data || {}); });
  return lfWrap({
    nodes: function () { return lfCollection(made); },
    edges: function () { return lfCollection([]); },
    elements: function () { return lfCollection(made); },
    getElementById: function (id) {
      const hit = made.filter(function (n) { return n.id() === id; });
      return hit.length ? hit[0] : { empty: function () { return true; } };
    },
  });
};
"""


if __name__ == "__main__":
    with io.open(sys.argv[1], encoding="utf-8") as handle:
        said = run(handle.read())
    if said:
        print(said)
        sys.exit(1)
    print("the page's own script runs")

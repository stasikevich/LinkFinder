"""Put one device in a setup from its own panel, in a real browser.

The panel is where a person already is when they click a device, so it is
where a single device is given its bench. Only a browser can judge it:
the field, the chips and the filter are wired to Cytoscape's own data.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, r"C:\Users\smitin\tools\linkfinder")
from linkfinder.render_topology import render_html

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

TOPO = {"nodes": [
    {"id": "ip:198.51.100.10", "name": "nuc1", "kind": "host", "state": "scanned",
     "serial": "ABC1234", "mgmt_ips": ["198.51.100.10"], "setups": ["from netbox"]},
    {"id": "chassis:usb:aaa1", "name": "a handset", "kind": "phone",
     "state": "scanned", "serial": "aaa1"}], "edges": []}

DRIVER = """
<script>
(function () {
  function report(v) {
    const b = document.createElement('pre'); b.id = 'r';
    b.textContent = 'LF ' + JSON.stringify(v); document.body.appendChild(b);
  }
  try {
    const node = cy.nodes()[0];
    showNode(node);                       // same path a click takes
    const hadField = !!document.getElementById('setupadd');
    const chipsBefore = document.querySelectorAll('#setupchips .chip').length;
    document.getElementById('setupadd').value = 'bench two';
    document.getElementById('setupaddgo').click();
    const afterAdd = (node.data('setups') || []).slice();
    const chipsAfter = document.querySelectorAll('#setupchips .chip').length;
    // and taking one back out
    const cross = document.querySelector('#setupchips .chip a');
    cross.click();
    const afterRemove = (node.data('setups') || []).slice();
    const other = cy.nodes()[1];
    showNode(other);
    report({
      field: hadField, chipsBefore: chipsBefore, chipsAfter: chipsAfter,
      afterAdd: afterAdd, afterRemove: afterRemove,
      otherUntouched: (other.data('setups') || []),
      otherSaysEmpty: (document.getElementById('setupchips') || {}).textContent,
      filter: Array.prototype.map.call(
        document.getElementById('setup').options, function (o) { return o.value; })
    });
    marksChanged = false;
  } catch (e) { report({ error: String(e) }); }
})();
</script>
"""

where = tempfile.mkdtemp(prefix="lf-panel-")
page = os.path.join(where, "panel.html")
open(page, "w", encoding="utf-8").write(
    render_html(TOPO).replace("</body>", DRIVER + "</body>", 1))
done = subprocess.run(
    [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
     "--user-data-dir=" + os.path.join(where, "profile"),
     "--virtual-time-budget=15000", "--dump-dom",
     "file:///" + page.replace("\\", "/")],
    capture_output=True, timeout=280)
out = (done.stdout or b"").decode("utf-8", "replace")
found = re.search(r"LF (\{.*?\})</pre>", out, re.S)
if not found:
    print("no answer:", out[-400:])
    sys.exit(1)
got = json.loads(found.group(1).replace("&quot;", '"'))
for key in sorted(got):
    print("  %-16s %s" % (key, got[key]))

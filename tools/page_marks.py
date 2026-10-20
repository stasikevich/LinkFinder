"""Drive a generated map in a real browser and read back what marking sent.

The marking code only means anything together with Cytoscape's own selection,
so stubs cannot judge it. This appends a short driver to a COPY of the page -
never to the shipped one - turns marking on, marks devices, and captures the
request the page would have made to the window, so the whole path can be
checked without a person clicking and without anything being written.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

EDGES = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]

DRIVER = """
<script>
(function () {
  function report(value) {
    const box = document.createElement('pre');
    box.id = 'lfresult';
    box.textContent = 'LF-RESULT ' + JSON.stringify(value);
    document.body.appendChild(box);
  }
  // Catch what the page would send instead of sending it. This driver writes
  // nothing anywhere.
  const sent = [];
  window.fetch = function (url, opts) {
    sent.push({ url: String(url), body: JSON.parse((opts || {}).body || '{}') });
    return Promise.resolve({ ok: true, status: 200,
                             json: function () { return Promise.resolve({}); } });
  };
  try {
    const want = %(count)d;
    let taken = 0;
    cy.nodes().forEach(function (n) {
      if (taken < want) { n.select(); taken += 1; }
    });
    const quiet = pick.className !== 'on';
    const mode = document.getElementById('markmode');
    const offByDefault = !mode.checked;
    mode.checked = true;
    mode.onchange();
    let again = want;
    cy.nodes().forEach(function (n) { if (again-- > 0) { n.select(); } });
    document.getElementById('pickname').value = %(name)s;
    document.getElementById('pickmark').click();
    setTimeout(function () {
      report({
        quietUntilAsked: quiet,
        modeOffByDefault: offByDefault,
        markingOffered: document.getElementById('markbox').style.display !== 'none',
        barShown: pick.className === 'on',
        counted: document.getElementById('pickcount').textContent,
        already: document.getElementById('pickalready').textContent,
        said: document.getElementById('pickstate').textContent,
        filterHasName: Array.prototype.map.call(
          document.getElementById('setup').options, function (o) { return o.value; }),
        sent: sent
      });
    }, 300);
  } catch (err) {
    report({ error: String(err) });
  }
})();
</script>
"""


def browser() -> str:
    for path in EDGES:
        if os.path.exists(path):
            return path
    raise RuntimeError("no Edge on this machine")


def drive(html: str, name: str = "bench-c", count: int = 2) -> dict:
    where = tempfile.mkdtemp(prefix="lf-marks-")
    page = os.path.join(where, "driven.html")
    driver = DRIVER % {"count": count, "name": json.dumps(name)}
    with io.open(page, "w", encoding="utf-8") as handle:
        handle.write(html.replace("</body>", driver + "</body>", 1))
    # Its own profile: two headless runs sharing the default user-data-dir
    # collide, and the failure looks like a timeout rather than a lock.
    done = subprocess.run(
        [browser(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         "--user-data-dir=" + os.path.join(where, "profile"),
         "--virtual-time-budget=15000",
         "--dump-dom", "file:///" + page.replace("\\", "/")],
        capture_output=True, timeout=300)
    out = (done.stdout or b"").decode("utf-8", "replace")
    found = re.search(r"LF-RESULT (\{.*?\})</pre>", out, re.S)
    if not found:
        raise RuntimeError("the page never reported: %s" % out[-400:])
    return json.loads(found.group(1).replace("&quot;", '"'))


if __name__ == "__main__":
    with io.open(sys.argv[1], encoding="utf-8") as handle:
        print(json.dumps(drive(handle.read()), indent=2)[:2000])

"""Checks for the help page: it must be complete, honest and offline.

A page that quietly falls behind the program is worse than none, because
people believe it. So the page is rebuilt here from the real sources and
every option the parser accepts must appear in it.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


import make_help                                   # noqa: E402

from linkfinder.cli import build_parser            # noqa: E402

print("-- it builds from the sources that are actually there --")
OUT = os.path.join(tempfile.gettempdir(), "lf_help_check.html")
if os.path.exists(OUT):
    os.remove(OUT)
make_help.build(OUT)
page = io.open(OUT, encoding="utf-8").read()
check("a page was produced", len(page) > 20000, True)

print()
print("-- it reaches nothing, like everything else this tool writes --")
# An inline script is fine and is what makes a long page navigable; what
# must never appear is anything fetched from somewhere else.
check("no script is loaded from elsewhere",
      bool(re.search(r"<script[^>]+src=", page)), False)
check("no stylesheet is loaded from elsewhere",
      bool(re.search(r"<link[^>]+href=", page)), False)
check("no font is loaded from elsewhere", "@font-face" in page, False)
check("no address of any kind", len(re.findall(r"https?://", page)), 0)
check("and no image to fetch", "<img" in page, False)

print()
print("-- every option the program accepts is written down --")
flags = set()
for action in build_parser()._actions:
    for option in action.option_strings:
        if option.startswith("--"):
            flags.add(option)
missing = sorted(f for f in flags if f not in page)
check("the program has options to document", len(flags) > 40, True)
check("none of them is missing from the page", missing, [])

print()
print("-- the markdown really became html --")
check("headings became headings", page.count("<h2") > 10, True)
check("tables became tables", page.count("<table>") > 10, True)
check("code fences became code blocks", page.count("<pre>") > 10, True)
check("and nothing was left as raw markdown",
      bool(re.search(r"^\|[^|]*\|\s*$", page, re.M)), False)

print()
print("-- the contents can be navigated --")
anchors = set(re.findall(r'id="([^"]+)"', page))
links = set(re.findall(r'href="#([^"]+)"', page))
check("every contents link lands somewhere", sorted(links - anchors), [])
check("and there is a contents at all", len(links) > 20, True)

print()
print("-- a long page has to be navigable, not just readable --")
check("there is a way to the contents", 'href="#contents"' in page, True)
check("and a way back to the top", 'id="up"' in page, True)
check("the sections can be filtered", 'id="find"' in page, True)
check("every section can be linked to on its own",
      page.count('class="anchor"') > 20, True)
check("a keyboard can skip the navigation", 'class="skip"' in page, True)
ids = re.findall(r'\sid="([^"]+)"', page)
check("and no element claims an id twice",
      sorted(set(i for i in ids if ids.count(i) > 1)), [])

print()
print("-- the typography follows what a help page is held to --")
check("body text is at least 16px", "font:16px/1.7" in page, True)
check("the measure is capped, not the full window",
      "max-width:72ch" in page, True)
check("the current section is marked as you read", ".here" in page, True)

print()
print("-- the page's own script runs, which no syntax check can tell you --")
import subprocess                                   # noqa: E402

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
check("there is exactly one inline script", len(blocks), 1)
STUB = """
var listeners = {};
function El(id) { this.id = id; this.style = {}; this.className = '';
  this.textContent = ''; this.value = ''; this.offsetTop = 0;
  this.scrollTop = 0;
  this.getBoundingClientRect = function () { return { top: 10 }; };
  this.addEventListener = function (n, f) { listeners[id + ':' + n] = f; };
  this.querySelectorAll = function () { return [new El('a1'), new El('a2')]; };
  this.getAttribute = function () { return '#one'; };
}
var made = {};
var document = { getElementById: function (id) {
    if (!made[id]) { made[id] = new El(id); } return made[id]; } };
var window = { scrollY: 0,
  addEventListener: function (n, f) { listeners['win:' + n] = f; },
  requestAnimationFrame: function (f) { f(); },
  scrollTo: function () { made.scrolled = true; } };
"""
DRIVE = """
listeners['find:input']();
listeners['win:scroll']();
listeners['up:click']();
console.log('ran');
"""
run = os.path.join(tempfile.gettempdir(), "lf_help_script.js")
io.open(run, "w", encoding="utf-8").write(STUB + blocks[0] + DRIVE)
try:
    done = subprocess.run(["node", run], capture_output=True, text=True)
    check("it runs, and so do the filter, the scroll and the top button",
          "ran" in done.stdout, True)
except OSError:
    if os.environ.get("LINKFINDER_REQUIRE_NODE"):
        FAILURES.append("no javascript engine, and this machine requires one")
    else:
        print("skipped: node is not available here")
finally:
    if os.path.exists(run):
        os.remove(run)

print()
print("-- it is where the program expects to find it --")
packed = os.path.join(ROOT, "linkfinder", "assets", "help.html")
check("the built page is bundled with the assets", os.path.exists(packed), True)

print()
print("-- a code span cannot be broken by the surrounding markup --")
check("a star inside code stays a star",
      "<code>a*b</code>" in make_help.inline("`a*b`"), True)
check("html in the prose is escaped, not rendered",
      "&lt;b&gt;" in make_help.escape("<b>"), True)

os.remove(OUT)
print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all help checks passed")

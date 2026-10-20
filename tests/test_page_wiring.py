"""The map page asks for nothing that is not on it.

One `document.getElementById('x')` for an element nobody put there returns
null, the next line reads a property off it, and the whole script stops -
so the map draws and then responds to nothing at all. It fails silently,
in the browser, where no test was looking.

That is exactly the risk in taking a panel out: the markup goes, a handler
somewhere still reaches for it, and the page is dead. Parsing the script
does not catch it - the syntax is perfect. This does.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


PAGE = os.path.join(HERE, "linkfinder", "assets", "topology.html")
src = open(PAGE, encoding="utf-8").read()

ids = set(re.findall(r'\bid="([^"]+)"', src))
asked = set(re.findall(r"getElementById\(\s*'([^']+)'\s*\)", src))
asked |= set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', src))

print("-- every element the script reaches for is on the page --")
check("nothing is asked for that was never put there",
      sorted(asked - ids), [])
check("and the page does ask for something, so this can fail",
      len(asked) > 10, True)

print()
print("-- the panel that was taken out left nothing behind --")
# It was 730 lines across markup, styling and script. A single leftover
# handler would have been enough to stop the page.
GONE = ("nbbar", "nblist", "nbsummary", "nbsaid", "nbwhere", "nbhelp",
        "nbpick", "nbnetbox", "nbsetups", "nbdrift", "nbodd", "nbrow",
        "nbTell", "openNetbox", "showNetboxPicker", "netboxCounts")
left = [name for name in GONE if name in src]
check("no trace of it in the page", left, [])

print()
print("-- a cable into the same machine does not label itself twice --")
# Seen on the picture: both ends of a loop are one node, so the two port
# labels are measured from the same point in the same direction and land
# on each other and on the device. One label naming both ports instead.
check("loops are styled apart from ordinary edges",
      "selector: 'edge:loop'" in src, True)
check("and that style drops the two end labels",
      re.search(r"selector: 'edge:loop'.*?'source-label': '', 'target-label': ''",
                src, re.S) is not None, True)
check("putting the port names back skips loops",
      "cy.style().selector('edge:simple')" in src, True)
check("and nothing still asks for every edge at once",
      "cy.style().selector('edge')\n" in src, False)

print()
print("-- the setup filter stays put when there is nothing in it --")
# Reported as "you removed the option to choose by setup". Nothing was
# removed: the control hid itself whenever no device carried a setup, and
# a control that vanishes is indistinguishable from one that was taken
# away. It stays, greyed, and says which of the two it is.
check("it does not start hidden",
      'id="setupbox" style="display:none"' in src, False)
check("and nothing hides it afterwards",
      "setupbox').style.display = names.length" in src, False)
check("an empty list says so in the control itself",
      "none named yet" in src, True)
check("and the title says where setups come from",
      "No device here is in a setup yet" in src, True)
check("it is greyed rather than gone",
      "picker.disabled = !names.length" in src, True)

print()
print("-- the greying the legend relies on is actually styled --")
# The script sets className 'absent' and a running page cannot tell whether
# anything came of it: delete the rule and every generation looks present.
check("the rule exists", ".legend span.absent" in src, True)
check("and it dims rather than hides",
      re.search(r"\.legend span\.absent \{[^}]*opacity", src) is not None, True)
check("hiding it would defeat the point",
      re.search(r"\.legend span\.absent \{[^}]*display:\s*none", src) is None,
      True)

print()
print("-- what the map still has to do --")
for name in ("filter", "setup", "pickmark", "pickunmark", "legend"):
    check("%s is still there" % name, name in ids, True)
check("and the window still answers the page", "tellTheTool" in src, True)

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all page wiring checks passed")

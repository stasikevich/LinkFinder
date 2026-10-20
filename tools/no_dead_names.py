"""Refuse to build code that uses a name nothing defines.

    python tools/no_dead_names.py

A name that is never defined is a crash waiting for whoever takes the
branch it sits in. Python does not notice until that line runs, and a
branch behind an option - `--port-health`, a particular vendor, an error
path - can sit there for months.

It was sitting there. `port_health.py` called `client.walk(OID_LLDP_LOC_PORTID)`
without ever importing the constant, inside a `try` with a bare `except`,
so on every switch the lookup raised NameError, the except swallowed it,
and the exact route that function is built around had never once run. The
tests were green throughout: nothing had a switch to walk.

pyflakes answers exactly this question and nothing else, which is why it
is the one tool here. It is required rather than optional - a check that
quietly skips itself when a tool is missing is not a check.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOK_AT = ("linkfinder", "tools", "tests")


def main(argv=None) -> int:
    # Asked for rather than imported: an import kept only to prove a thing
    # exists is itself an unused import, and this file has to pass its own
    # check.
    import importlib.util

    if importlib.util.find_spec("pyflakes") is None:
        print("pyflakes is not installed, so nothing can be checked:")
        print("    pip install --user pyflakes")
        return 1
    where = [os.path.join(ROOT, name) for name in LOOK_AT
             if os.path.isdir(os.path.join(ROOT, name))]
    done = subprocess.run([sys.executable, "-m", "pyflakes"] + where,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    said = done.stdout.decode("utf-8", "replace").strip()
    lines = [l for l in said.splitlines() if l.strip()]
    if lines:
        print("%d thing(s) the code says but does not mean:" % len(lines))
        for line in lines:
            print("  " + line.replace(ROOT + os.sep, ""))
        return 1
    print("every name used is a name something defines")
    return 0


if __name__ == "__main__":
    sys.exit(main())

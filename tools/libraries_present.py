"""The libraries the binary needs, found in the source rather than listed.

    python tools/libraries_present.py          # check them, loudly
    python tools/libraries_present.py --names  # one per line, for the build

Both build scripts used to carry `--hidden-import paramiko --hidden-import
requests` typed by hand. That list was wrong in two ways at once. It was
short - urllib3 was never on it - and nothing checked that the libraries
were installed on the machine doing the building. PyInstaller treats a
hidden import it cannot find as a warning, so a Linux build host without
paramiko produced a binary that started, drew menus, and could not open a
single SSH session. It shipped, and the first person to press Scan was
told to pip install something into a binary.

So the list is read out of the source every time, and the build stops when
one of them is not installed.

A note on telling stdlib from the rest: on Windows site-packages lives
INSIDE the stdlib directory, so "is this path under the stdlib" answers
yes for every third-party package there. That check was written, it passed,
and it hid paramiko completely.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import sys
import sysconfig
from typing import Dict, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "linkfinder")

# Installed beside the interpreter rather than with it. Both spellings are
# needed: Debian puts pip's work in dist-packages, everybody else in
# site-packages, and a --user install goes somewhere else again.
OUTSIDE = ("site-packages", "dist-packages", ".local")

# Standard library, but only on one operating system. Absent on the other
# one is correct, not a hole: gui.py reads the Windows registry behind a
# platform check, and a Linux build is right to have no winreg in it.
PLATFORM_ONLY = ("winreg", "_winreg", "msvcrt", "fcntl", "termios", "pwd",
                 "grp", "posix", "nt")


def imported_names() -> Dict[str, Set[str]]:
    """Every top-level module name the package imports, and where from."""
    found: Dict[str, Set[str]] = {}
    for here, _dirs, files in os.walk(PACKAGE):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(here, name)
            tree = ast.parse(io.open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.setdefault(alias.name.split(".")[0],
                                         set()).add(name)
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    if node.module:
                        found.setdefault(node.module.split(".")[0],
                                         set()).add(name)
    found.pop("linkfinder", None)
    return found


def classify(name: str) -> Tuple[str, str]:
    """Return (verdict, where). Verdict is stdlib, outside or missing."""
    if name in sys.builtin_module_names:
        return "stdlib", "built in"
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "missing", ""
    origin = spec.origin or ""
    if origin in ("built-in", "frozen") or not origin:
        return "stdlib", origin or "namespace"
    flat = os.path.realpath(origin).replace("\\", "/")
    if any(mark in flat for mark in OUTSIDE):
        return "outside", origin
    stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"]).replace("\\", "/")
    return ("stdlib" if flat.startswith(stdlib) else "outside"), origin


def wanted() -> List[str]:
    """The third-party libraries this package imports, in name order."""
    return sorted(name for name in imported_names()
                  if classify(name)[0] == "outside")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    used = imported_names()
    if "--names" in argv:
        for name in wanted():
            print(name)
        return 0

    outside, missing, elsewhere = [], [], []
    for name in sorted(used):
        verdict, where = classify(name)
        if verdict == "outside":
            outside.append((name, where))
        elif verdict == "missing":
            if name in PLATFORM_ONLY:
                elsewhere.append(name)
            else:
                missing.append((name, sorted(used[name])))

    for name, where in outside:
        print("  %-14s %s" % (name, where))
    if elsewhere:
        print("  %-14s %s" % ("(not here)",
                              "%s - standard library on the other platform"
                              % ", ".join(elsewhere)))
    if missing:
        print("%d librarie(s) the program imports are not installed here, so "
              "the build would leave them out and say only a warning:"
              % len(missing))
        for name, files in missing:
            print("  %-14s imported by %s" % (name, ", ".join(files)))
            print("                 install it:  pip install %s" % name)
        return 1
    if not outside:
        print("the program imports nothing outside the standard library")
        return 0
    print("all %d librarie(s) the program needs are installed" % len(outside))
    return 0


if __name__ == "__main__":
    sys.exit(main())

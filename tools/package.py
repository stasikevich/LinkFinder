"""Arrange what was built into one folder per audience, and zip it.

PyInstaller drops everything into `dist/` side by side, which is fine for a
developer and wrong for anybody else: four Windows binaries whose names
differ by a suffix, a Linux one with no extension, and nothing saying which
of them a given person should run. This sorts them by who the copy is for
and then by platform, puts the licence and the documentation beside the
binaries, and leaves one zip to hand over.

The open and branded sets are never mixed in one folder. They carry
different names on purpose and a zip that held both would put the question
"which of these is mine" back on the reader.

The zip is not made unless it is asked for, because the folders are the
deliverable and whoever hands them on may want to pack them their own way.

Run: python tools/package.py open
     python tools/package.py pw --zip

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import shutil
import sys
import zipfile
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

BUILDS: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
    "open": {
        "windows": [("linkfinder.exe", "linkfinder.exe"),
                    ("LinkFinder-GUI.exe", "LinkFinder-GUI.exe")],
        "linux": [("linkfinder", "linkfinder"),
                  ("linkfinder-gui", "linkfinder-gui")],
    },
    "pw": {
        "windows": [("linkfinder-pw.exe", "linkfinder-pw.exe"),
                    ("LinkFinder-PW-GUI.exe", "LinkFinder-PW-GUI.exe")],
        "linux": [("linkfinder-pw", "linkfinder-pw"),
                  ("linkfinder-pw-gui", "linkfinder-pw-gui")],
    },
}

# A Linux binary and its Windows twin differ only by ".exe", so the one
# built elsewhere lands in its own folder rather than beside them.
LANDING = {"linux": "_linux"}

PAPERS = ("README.md", "DOCUMENTATION.md", "LICENSE", "AUTHORS")

WINDOWS_NOTE = """What is in here

  LinkFinder%(suffix)s-GUI.exe   the window - double-click it
  linkfinder%(low)s.exe       the same tool for a command line and for scripts

Nothing is installed and nothing is written outside the folder you choose
for the report. Run either one as you are; administrator rights are not
needed unless your own account cannot reach the devices.
"""

LINUX_NOTE = """What is in here

  linkfinder%(low)s           the command-line tool, one file, nothing to install

  chmod +x linkfinder%(low)s && ./linkfinder%(low)s --help

Built against glibc %(glibc)s. A much older distribution will refuse to start it;
in that case run it from the source tree instead - it needs only Python 3.8.
%(gui)s"""

NO_GUI = """
There is no window for Linux in this package: the build host has no tkinter,
and a binary built without it would fail only when somebody opened it.
Run the window from the source tree, or use the command line above.
"""


def _say(message: str) -> None:
    print("  " + message)


def collect(brand: str, glibc: str = "2.35") -> str:
    """Sort the built files into dist/<brand>/<platform>/ and paper them."""
    where = os.path.join(DIST, brand)
    if os.path.isdir(where):
        shutil.rmtree(where)
    found = 0
    for platform, wanted in sorted(BUILDS[brand].items()):
        holder = os.path.join(where, platform)
        here = []
        for built, name in wanted:
            source = os.path.join(DIST, LANDING.get(platform, ""), built)
            if not os.path.exists(source):
                continue
            os.path.isdir(holder) or os.makedirs(holder)
            shutil.copy2(source, os.path.join(holder, name))
            here.append(name)
            found += 1
        if not here:
            _say("%s: nothing built" % platform)
            continue
        low = "-pw" if brand == "pw" else ""
        note = (WINDOWS_NOTE % {"suffix": "-PW" if brand == "pw" else "",
                                "low": low} if platform == "windows"
                else LINUX_NOTE % {
                    "low": low, "glibc": glibc,
                    "gui": "" if any("gui" in n for n in here) else NO_GUI})
        with open(os.path.join(holder, "READ ME FIRST.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(note)
        _say("%s: %s" % (platform, ", ".join(sorted(here))))
    if not found:
        raise SystemExit("nothing to package - build first")
    for paper in PAPERS:
        source = os.path.join(ROOT, paper)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(where, paper))
    docs = os.path.join(ROOT, "docs")
    if os.path.isdir(docs):
        shutil.copytree(docs, os.path.join(where, "docs"))
    return where


def pack(where: str, brand: str, version: str) -> str:
    """One zip, named so the person opening it knows what they have."""
    target = os.path.join(DIST, "LinkFinder-%s-%s.zip" % (brand, version))
    if os.path.exists(target):
        os.remove(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for folder, _dirs, files in os.walk(where):
            for name in sorted(files):
                full = os.path.join(folder, name)
                inside = os.path.join("LinkFinder-%s-%s" % (brand, version),
                                      os.path.relpath(full, where))
                item = zipfile.ZipInfo.from_file(full, inside)
                item.compress_type = zipfile.ZIP_DEFLATED
                # Zipping on Windows loses the executable bit, and a binary
                # that will not run until someone thinks of chmod is a bad
                # first minute. Set it for the Linux folder on the way in.
                mode = 0o755 if os.sep + "linux" + os.sep in full else 0o644
                item.external_attr = mode << 16
                with open(full, "rb") as handle:
                    bundle.writestr(item, handle.read())
    return target


def main() -> int:
    brand = (sys.argv[1] if len(sys.argv) > 1 else "open").lower()
    if brand not in BUILDS:
        print("unknown brand %r - use %s"
              % (brand, " or ".join(sorted(BUILDS))))
        return 2
    sys.path.insert(0, ROOT)
    from linkfinder import __version__

    rest = [a for a in sys.argv[2:] if a != "--zip"]
    glibc = rest[0] if rest else "2.35"
    print("packaging the %s build" % brand)
    where = collect(brand, glibc)
    print("  %s" % os.path.relpath(where, ROOT))
    if "--zip" not in sys.argv:
        return 0
    target = pack(where, brand, __version__)
    size = os.path.getsize(target) / (1024.0 * 1024.0)
    print("  %s  %.1f MB" % (os.path.relpath(target, ROOT), size))
    return 0


if __name__ == "__main__":
    sys.exit(main())

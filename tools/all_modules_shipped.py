"""Refuse to ship a build that is missing part of the program.

    python tools/all_modules_shipped.py

PyInstaller works out what to bundle by reading the source for import
statements. A module reached through a name built at run time -
`__import__("linkfinder." + which)` - is invisible to it and is simply
left out, and nothing says so until the feature quietly does nothing on
somebody else's machine.

That happened. A list of link builders was refactored to hold module
names as strings, and the binary shipped without three of them: every ARP
link, every PTP link and the consistency check vanished from the product
while every test stayed green, because tests run from source where the
import always works.

So the binary is asked what it contains, and every module in the tree has
to be in there. Nothing here guesses - it reads the archive.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "linkfinder")
# Windows-only and Linux-only paths are both in the tree; a build for one
# platform is right to leave the other's out.
EXCUSED = {"__main__"}


def ours() -> set:
    """Every module in the package, by the name it is imported under."""
    found = set()
    for name in os.listdir(PACKAGE):
        if name.endswith(".py") and not name.startswith("_"):
            found.add(name[:-3])
    return found - EXCUSED


def libraries_wanted() -> set:
    """The third-party libraries the source imports, read from the source."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from libraries_present import wanted

    return set(wanted())


def inside(path: str) -> tuple:
    """(our modules in the binary, every top-level package name in it).

    The second half is what catches a missing library. PyInstaller treats a
    hidden import it cannot resolve as a warning, so a build host without
    paramiko produced binaries that started, drew every menu and could not
    open one SSH session - and said so only to the person who pressed Scan.
    """
    import tempfile

    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    archive = CArchiveReader(path)
    for name in list(archive.toc):
        if not str(name).lower().endswith(".pyz"):
            continue
        blob = archive.extract(name)
        if isinstance(blob, tuple):
            blob = blob[1]
        handle, where = tempfile.mkstemp(suffix=".pyz")
        os.close(handle)
        try:
            with open(where, "wb") as out:
                out.write(blob)
            held = ZlibArchiveReader(where)
            names = [str(m) for m in held.toc]
            return ({n.split(".", 1)[1] for n in names
                     if n.startswith("linkfinder.")},
                    {n.split(".")[0] for n in names})
        finally:
            try:
                os.remove(where)
            except OSError:
                pass
    return set(), set()


def data_files(path: str) -> set:
    """The names of the data files bundled beside the code (brand, icon, ...).

    These sit in the outer CArchive, not the PYZ: --add-data puts them there.
    It is where logins.json lands, so this is how the gate proves the open
    build carries no default passwords and the internal one does.
    """
    from PyInstaller.archive.readers import CArchiveReader

    try:
        archive = CArchiveReader(path)
    except Exception:
        return set()
    return {os.path.basename(str(n)) for n in archive.toc}


def binaries(where: str):
    for folder, dirs, names in os.walk(where):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for name in names:
            path = os.path.join(folder, name)
            if os.path.splitext(name)[1].lower() not in ("", ".exe"):
                continue
            if os.path.getsize(path) >= 1024 * 1024:
                yield path


def main(argv=None) -> int:
    """With no arguments, every binary in dist. With them, only those.

    A build of one brand kept failing on the other brand's binaries,
    which were simply from an earlier run and had never heard of the new
    module. That is worth knowing before shipping and is not a reason to
    stop the build in front of you, so the build names what it just made
    and a bare run still judges the lot.
    """
    dist = os.path.join(ROOT, "dist")
    chosen = [a for a in (argv if argv is not None else sys.argv[1:]) if a]
    if chosen:
        here = [p for p in chosen if os.path.isfile(p)]
        missing = [p for p in chosen if not os.path.isfile(p)]
        if missing:
            print("asked about %d file(s) that are not there: %s"
                  % (len(missing), ", ".join(missing)))
            return 1
        return _judge(here)
    if not os.path.isdir(dist):
        print("nothing built yet - nothing to check")
        return 0
    return _judge(sorted(binaries(dist)))


def _judge(paths) -> int:
    wanted = ours()
    libraries = libraries_wanted()
    bad = []
    looked = 0
    for path in paths:
        try:
            held, packages = inside(path)
        except Exception as problem:
            bad.append("%s could not be read: %s"
                       % (os.path.relpath(path, ROOT), problem))
            continue
        if not held:
            continue                 # not a PyInstaller binary of ours
        looked += 1
        missing = sorted(wanted - held)
        if missing:
            bad.append("%s is missing %d module(s): %s"
                       % (os.path.relpath(path, ROOT), len(missing),
                          ", ".join(missing)))
        absent = sorted(libraries - packages)
        if absent:
            bad.append("%s was built without %s - it will start and then be "
                       "unable to do the work that needs it"
                       % (os.path.relpath(path, ROOT), ", ".join(absent)))
        # Default logins belong to the internal build and nowhere else. The
        # brand gives it away: an internal binary has "pw" in its name.
        rel = os.path.relpath(path, ROOT)
        base = os.path.basename(path).lower()
        files = data_files(path)
        has_logins = "logins.json" in files
        internal = "pw" in base
        # The internal-only secrets: host logins, radio logins, and the
        # site-specific log-file rules. None may ride in the open build.
        secrets = ("logins.json", "radio-logins.json", "radio-sources.json")
        if not internal:
            carried = sorted(s for s in secrets if s in files)
            if carried:
                bad.append("%s is an open build but carries %s - they must ship "
                           "in the internal build only" % (rel, ", ".join(carried)))
        if internal and not has_logins:
            bad.append("%s is the internal build but carries no default "
                       "logins - branding/logins.json was not bundled" % rel)
    if not looked and not bad:
        print("no packaged binary to look inside")
        return 0
    if bad:
        print("%d binary(s) do not carry the whole program:" % len(bad))
        for line in bad:
            print("  " + line)
        print("either that binary is from an earlier build, made before the")
        print("module existed - build the other brand too - or the module is")
        print("reached by a name put together at run time, which the bundler")
        print("cannot see. An import statement is the thing that is seen.")
        return 1
    print("all %d module(s) are in every one of the %d binaries"
          % (len(wanted), looked))
    return 0


if __name__ == "__main__":
    sys.exit(main())

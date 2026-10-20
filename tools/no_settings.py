"""Refuse to ship a build that carries anybody's settings.

Settings, the NetBox token, remembered switches, setups and saved maps all
live beside the user, not inside the program. That is easy to say and easy
to break: one stray file copied next to the binaries, or one default left
in the source, and a lab's own data goes out with the product.

So it is checked rather than promised, and the build stops if it is wrong.

    python tools/no_settings.py            # the repo and dist, both

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import re
import sys
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything this tool ever writes for a person, by the name it writes it as.
PERSONAL = (
    ".linkfinder.json",          # settings, logins, the NetBox address
    "linkfinder.json",           # the same, kept beside the program
    ".linkfinder-switches.json",  # switches remembered from earlier runs
    ".linkfinder-history",       # the maps it keeps to compare against
    "setups.json",
    "marks.json",
)

# These match a value, not a mention of one. Looking for the bare word
# "nbt_" fired on the help page explaining what a token looks like, and a
# gate that goes off at its own documentation is a gate people switch off.
# What must never ship is somebody's actual token, address or tag list.
NEVER_INSIDE = (
    (re.compile(rb"nbt_[A-Za-z0-9]{10,}\.[A-Za-z0-9]{10,}"),
     "a NetBox API token"),
    (re.compile(rb'"token"\s*:\s*"[^"]{12,}"'), "a saved token"),
    (re.compile(rb'"setup_tags"\s*:\s*\[\s*"'), "somebody's setup tags"),
    (re.compile(rb'"netbox"\s*:\s*\{[^}]{0,200}?"url"\s*:\s*"[^"]+"'),
     "a saved NetBox address"),
)
# There is deliberately no rule for `"password": "..."`. The help page ships
# with the defaults file written out in full, placeholders and all, and no
# pattern tells `"secret"` in an example from a real one - real passwords
# here run shorter than the placeholders. Shape cannot answer it; where the
# text came from can, which is what the PERSONAL names above and
# tools/no_site_refs.py are for.

# A string in the source ends up zlib-compressed inside the binary, so
# reading the raw bytes finds nothing and the check passes on everything.
# It did: a real token baked into a shipped asset would have gone straight
# out. Something known to be in there proves the archive was really opened.
#
# This has to come from our own Python and nowhere else. The first control
# was "ipmitool", which is also a word in the shipped help page - so it was
# found, the gate looked satisfied, and the Python modules were in fact
# still unreadable inside a nested archive nobody had opened.
PROOF = b"perm_hwaddr"

SKIP_DIRS = {".git", "build", "__pycache__", "node_modules", ".venv"}


def _walk(where: str):
    for folder, dirs, names in os.walk(where):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            yield os.path.join(folder, name)
        for name in dirs:
            yield os.path.join(folder, name)


def stray_files(where: str):
    """Anything personal sitting where it would be packaged."""
    found = []
    for path in _walk(where):
        if os.path.basename(path) in PERSONAL:
            found.append(os.path.relpath(path, ROOT))
    return sorted(found)


def _unpacked(path: str):
    """Every file inside a packaged binary, as the program will see it.

    Yields (name, bytes). An archive that cannot be opened yields nothing,
    and the caller is the one that decides what that silence means.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:
        return
    try:
        archive = CArchiveReader(path)
    except Exception:
        return
    for name in list(archive.toc):
        try:
            data = archive.extract(name)
        except Exception:
            continue
        if isinstance(data, tuple):       # older readers return (code, data)
            data = data[1]
        if not isinstance(data, (bytes, bytearray)):
            continue
        yield name, bytes(data)
        # Our own Python does not live here. It lives compiled inside a
        # second archive carried as one of these entries, and until that
        # was opened the whole scan was reading data files and assets
        # only - a token hardcoded in a .py went completely unseen.
        if str(name).lower().endswith(".pyz"):
            for inner, text in _inside_pyz(bytes(data)):
                yield "%s > %s" % (name, inner), text


def _inside_pyz(blob: bytes):
    """Every string constant in every module of the bundled Python."""
    import marshal
    import tempfile

    try:
        from PyInstaller.archive.readers import ZlibArchiveReader
    except Exception:
        return
    handle, path = tempfile.mkstemp(suffix=".pyz")
    os.close(handle)
    try:
        with open(path, "wb") as out:
            out.write(blob)
        try:
            archive = ZlibArchiveReader(path)
        except Exception:
            return
        for name in list(archive.toc):
            try:
                code = archive.extract(name)
            except Exception:
                continue
            if isinstance(code, tuple):
                code = code[1]
            if isinstance(code, (bytes, bytearray)):
                try:
                    code = marshal.loads(bytes(code))
                except Exception:
                    continue
            found: List[str] = []
            _constants(code, found)
            if found:
                yield name, "\n".join(found).encode("utf-8", "replace")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _constants(code, out: List[str], depth: int = 0) -> None:
    """Every string in a code object, however it is nested.

    A table written as a tuple of tuples - which is how this tool keeps
    most of its rules - is one constant, not a string, so walking only
    strings and code objects walked straight past it.
    """
    if depth > 12:
        return
    _walk_value(getattr(code, "co_consts", ()) or (), out, depth)


def _walk_value(value, out: List[str], depth: int) -> None:
    if depth > 12:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, bytes):
        out.append(value.decode("utf-8", "replace"))
    elif isinstance(value, (tuple, list, set, frozenset)):
        for part in value:
            _walk_value(part, out, depth + 1)
    elif isinstance(value, dict):
        for key, part in value.items():
            _walk_value(key, out, depth + 1)
            _walk_value(part, out, depth + 1)
    elif hasattr(value, "co_consts"):
        _constants(value, out, depth + 1)


def _binaries(where: str):
    for path in _walk(where):
        if not os.path.isfile(path):
            continue
        if os.path.splitext(path)[1].lower() not in ("", ".exe"):
            continue
        if os.path.getsize(path) >= 1024 * 1024:
            yield path            # the READ ME is not a binary


def inside_binaries(where: str):
    """Settings-shaped text baked into a compiled binary.

    Both layers are needed. The raw bytes catch anything stored plainly;
    the unpacked archive catches what is compressed, which is where the
    whole of our own source lives.
    """
    found = []
    opened = 0
    looked = 0
    for path in _binaries(where):
        looked += 1
        short = os.path.relpath(path, ROOT)
        with open(path, "rb") as handle:
            pieces = [("the file itself", handle.read())]
        saw_proof = False
        for name, blob in _unpacked(path):
            pieces.append((name, blob))
            if PROOF in blob:
                saw_proof = True
        if saw_proof:
            opened += 1
        for name, blob in pieces:
            for shape, what in NEVER_INSIDE:
                if shape.search(blob):
                    found.append("%s holds %s, in %s" % (short, what, name))
    # A check that passes because it could not look is worse than no check.
    if looked and not opened:
        found.append("could not read inside any of the %d binaries - install "
                     "PyInstaller, or this proves nothing" % looked)
    return sorted(found)


def main(argv=None) -> int:
    dist = os.path.join(ROOT, "dist")
    bad = stray_files(ROOT)
    if os.path.isdir(dist):
        bad += inside_binaries(dist)
    if bad:
        print("%d thing(s) carrying somebody's settings - these must not ship:"
              % len(bad))
        for line in bad:
            print("  " + line)
        return 1
    print("no settings, tags or setups in the build")
    return 0


if __name__ == "__main__":
    sys.exit(main())

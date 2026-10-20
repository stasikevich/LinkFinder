"""Fail the build if anything in the tree points at a real site.

Run: python tools/no_site_refs.py

This is a deny list, not a cleverness contest. The realistic mistake is not
inventing a secret - it is pasting real output from a live network into a test
fixture or a documentation example, which is how every leak in this project so
far has happened. So the gate looks for the markers of a real site: address
ranges that are not documentation ranges, hostnames of real boxes, and the
vendor OUIs of equipment somebody actually owns.

Locally administered MACs (02:, aa:bb:cc and the like) and the protocol
multicast groups are safe by construction and are not listed.

Add a marker here the moment a new site is worked against. A gate that is only
updated after a leak is not a gate.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A virtualenv sits inside the tree on Linux, and PyInstaller ships a hook
# named after one of the denied words. Third-party code is not ours to leak.
SKIP_DIRS = {".git", "dist", "build", "__pycache__", ".pytest_cache", "docs",
             "branding", ".venv", "venv", "env", "site-packages", ".tox",
             ".mypy_cache", "node_modules", ".eggs"}
# oui.gz is the IEEE's public register of who holds which MAC prefix. It
# names every maker on earth, including the ones denied below, and says
# nothing about any site. Exempted on purpose, not because it is compressed:
# a gate that only passes because the data is unreadable is not a gate.
SKIP_FILES = {"cytoscape.min.js", "no_site_refs.py", "oui.gz"}
SKIP_EXT = (".pyc", ".png", ".jpg", ".ico", ".pcap", ".pcapng", ".stackdump")

# The markers themselves - site IP ranges, bench hostnames and the OUIs of
# equipment somebody owns - are what the gate must keep out, so listing them
# here would make this very file the leak. They live in a gitignored file
# (branding/ is gitignored and skipped below); the internal build carries it,
# the open tree does not. An absent file means empty lists: a clean public
# checkout has nothing to catch, which is the point. A maker's name as the
# IEEE register prints it is public and stays allowed - it names a company.
_DENY = os.path.join(ROOT, "branding", "deny_list.json")
DENY_PREFIX = DENY_WORDS = DENY_OUI = ()
if os.path.exists(_DENY):
    try:
        _d = json.load(io.open(_DENY, encoding="utf-8"))
        DENY_PREFIX = tuple(_d.get("prefix", ()))
        DENY_WORDS = tuple(_d.get("words", ()))
        DENY_OUI = tuple(_d.get("oui", ()))
    except (ValueError, OSError):
        pass


def body_of(path):
    try:
        raw = io.open(path, "rb").read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", "replace")


def files():
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name in SKIP_FILES or name.endswith(SKIP_EXT):
                continue
            yield os.path.join(folder, name)


def main():
    hits = []
    for path in files():
        body = body_of(path)
        if body is None:
            continue
        rel = os.path.relpath(path, ROOT)
        for number, line in enumerate(body.splitlines(), start=1):
            low = line.lower()
            for prefix in DENY_PREFIX:
                if prefix in line:
                    hits.append((rel, number, "address " + prefix, line))
            for word in DENY_WORDS:
                if word in low:
                    hits.append((rel, number, "name " + word, line))
            for oui in DENY_OUI:
                if oui in low:
                    hits.append((rel, number, "equipment " + oui, line))

    if not hits:
        print("no site references found")
        return 0

    print("%d site reference(s) - these must not reach the repository:" % len(hits))
    for rel, number, why, line in hits[:60]:
        print("  %s:%d  [%s]  %s" % (rel, number, why, line.strip()[:100]))
    if len(hits) > 60:
        print("  ... and %d more" % (len(hits) - 60))
    return 1


if __name__ == "__main__":
    sys.exit(main())

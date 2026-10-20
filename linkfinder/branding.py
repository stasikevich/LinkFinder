"""How a build signs itself on screen.

Two builds come out of this tree: the open one, which carries the author's
name, and an internal one that carries a company icon instead. Only what is
*displayed* changes. The licence, the copyright notice, the AUTHORS file and
the header of every source file stay exactly as they are - MIT requires the
notice to travel with every copy, and a build flag is not a way around that.

A packaged build picks this up from a `brand.json` bundled beside it. Running
from the source tree finds nothing and uses the defaults, so the open build is
what you get unless somebody deliberately packaged otherwise.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import json
import os
import sys
from typing import Dict

from . import __author__, __title__, __version__

_CACHE: Dict[str, str] = {}


def _folders():
    base = getattr(sys, "_MEIPASS", None)
    here = os.path.dirname(os.path.abspath(__file__))
    # Only where a packaged build puts its files. The branding/ folder in a
    # working tree is deliberately not searched: running from source must
    # behave like the open build, whatever happens to be sitting on disk.
    for folder in ([base] if base else []) + [here, os.path.dirname(here)]:
        if folder:
            yield folder


def _candidates():
    for folder in _folders():
        yield os.path.join(folder, "brand.json")


def load() -> Dict[str, str]:
    """The brand this build was packaged with, or the defaults."""
    if _CACHE:
        return _CACHE
    found = {"title": __title__, "author": __author__}
    for path in _candidates():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for key in ("title", "author"):
                if key in data:
                    found[key] = str(data[key])
            break
    _CACHE.update(found)
    return _CACHE


def icon_path() -> str:
    """The icon this build was packaged with, if it carries one.

    PyInstaller's --icon only dresses the file in Explorer. The window and
    the taskbar take whatever the toolkit hands them, which is Tk's own
    default - so a branded build looks right on disk and wrong once opened
    unless the window is told as well.
    """
    for folder in _folders():
        candidate = os.path.join(folder, "icon.ico")
        if os.path.isfile(candidate):
            return candidate
    return ""


def title() -> str:
    return load().get("title") or __title__


def author() -> str:
    return load().get("author", "")


def signature() -> str:
    """"LinkFinder 1.1.0 - by Stas Mitin", or just the name when unsigned."""
    who = author()
    base = "%s %s" % (title(), __version__)
    return "%s - by %s" % (base, who) if who else base

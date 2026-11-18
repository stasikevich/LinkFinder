"""Every device the map can draw has an icon - no node ever shows a "?".

The RC-1SP6T switch once drew a question mark: it was classified into a kind
whose glyph the icon map did not hold. That is a whole class of bug - a kind,
a maker glyph or a model-derived kind that the renderer can produce but
icon_uris() cannot supply. These checks close the class: they drive every
path that picks an icon (kind style, size, vendor glyph, maker->kind, the
unknown-but-known-maker fallback) and assert the icon exists, so a new kind
added without its svg, or a maker mapped to a glyph with no file, fails here
and not on a live map.

Run: python tests/test_icons.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder import render_topology as rt
from linkfinder import neighbors
from linkfinder.oui import MAKER_KIND, kind_from_maker

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-66s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


ICONS = rt.icon_uris()


def test_style_and_size_cover_the_same_kinds():
    # KIND_SIZE[kind] is indexed with no guard; a kind styled but not sized
    # crashes the render, so the two tables must list exactly the same kinds.
    check("every styled kind has a size (and vice versa)",
          set(rt.KIND_STYLE), set(rt.KIND_SIZE))


def test_every_kind_has_a_loadable_icon():
    for kind in rt.KIND_STYLE:
        check("icon present for kind %r" % kind, kind in ICONS, True)


def test_every_vendor_glyph_resolves():
    # glyph_for returns a VENDOR_GLYPH value verbatim; each must be drawable.
    for maker, glyph in rt.VENDOR_GLYPH.items():
        check("vendor glyph %r (from %r) has an icon" % (glyph, maker),
              glyph in ICONS, True)


def test_every_device_glyph_resolves():
    for glyph in rt.GLYPH_COLOUR:
        check("device glyph %r has an icon" % glyph, glyph in ICONS, True)


def test_maker_kind_only_yields_styled_kinds():
    # kind_from_maker rescues an unknown node by its maker; the kind it returns
    # is then styled and sized, so every kind in the table must be known.
    for kind, _names in MAKER_KIND:
        check("maker-derived kind %r is styled" % kind, kind in rt.KIND_STYLE, True)


def test_classify_kind_only_yields_styled_kinds():
    # Everything classify_kind can return must be a kind the map can draw.
    samples = ["bmc", "switch", "rf-switch", "linux", "suse", "esxi", "windows",
               "darwin", "", "weird-unseen-kind"]
    caps = ["", "wlan", "telephone", "bridge", "router", "station"]
    descs = ["", "Cisco IOS", "Arista EOS", "JunOS", "Cumulus Linux", "Ubuntu"]
    for rk in samples:
        for cp in caps:
            for ds in descs:
                k = neighbors.classify_kind(rk, cp, ds)
                check("classify_kind -> %r is styled" % k, k in rt.KIND_STYLE, True)


def _node(**kw):
    kw.setdefault("id", "n")
    return kw


def test_to_elements_icon_always_resolves():
    # The strongest guard: render one node of every kind, plus the maker-only
    # and the unknown-but-mini-circuits cases, and assert the icon each node
    # carries is a key the embedded icon map actually holds.
    nodes = [_node(id="k-%s" % k, kind=k) for k in rt.KIND_STYLE]
    # A maker-only neighbour (kind unknown) of every mapped maker: vendor_of
    # reads the maker from the board's DMI record, so set it there. This is the
    # secondary net that saved the RC-1SP6T when its kind was still unknown.
    want_glyph = {}
    for maker, glyph in rt.VENDOR_GLYPH.items():
        nid = "v-%s" % maker
        want_glyph[nid] = glyph
        nodes.append(_node(id=nid, kind="unknown",
                           extra={"virtual": {"sys_vendor": maker}}))
    out = rt.to_elements({"nodes": nodes, "edges": []})
    for el in out["nodes"]:
        nid = el["data"].get("id")
        icon = el["data"].get("icon")
        check("rendered node %r icon %r is in the icon map" % (nid, icon),
              icon in ICONS, True)
        if nid in want_glyph:
            check("maker-only node %r drew its vendor glyph" % nid,
                  icon, want_glyph[nid])


def test_rc_1sp6t_regression_specifically():
    # The bug as reported: a probed rf-switch must keep its kind and its glyph.
    check("classify_kind keeps a probed rf-switch",
          neighbors.classify_kind("rf-switch", "", "Mini-Circuits"), "rf-switch")
    g = rt.glyph_for({"kind": "unknown"}, "unknown", "Mini-Circuits RC-1SP6T-A12")
    check("an unknown mini-circuits box still draws the rf-switch glyph",
          g, "rf-switch")
    check("and that glyph has an icon", g in ICONS, True)


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

print()
if FAILURES:
    print("%d FAILURES" % len(FAILURES))
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all icon-contract checks passed")

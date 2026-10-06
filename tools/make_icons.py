"""Draw the device icon set carried by the topology page.

Every icon is original line art, generated here rather than taken from a
vendor stencil set, so the whole tool stays MIT with no third-party artwork.
Each drawing leaves an __ACCENT__ placeholder that the renderer fills with the
colour of the device kind, and a viewBox cropped to the drawing so Cytoscape
can scale it into a node without wasted margin.

Run: python tools/make_icons.py

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "linkfinder", "assets", "icons")
os.makedirs(OUT, exist_ok=True)

VIEWBOX = {
    "switch": (0, 16, 64, 31),
    "host": (6, 9, 52, 46),
    "router": (0, 14, 64, 36),
    "bmc": (10, 8, 44, 50),
    "ap": (7, 9, 50, 47),
    "phone": (6, 14, 52, 43),
    "unknown": (8, 10, 48, 44),
}


def head(kind):
    x, y, w, h = VIEWBOX[kind]
    return ('<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="%s %s %s %s" width="%s" height="%s" fill="none" '
            'stroke-linecap="round" stroke-linejoin="round">\n'
            % (x, y, w, h, w, h))


TAIL = "</svg>\n"
A = "__ACCENT__"


def body(x, y, w, h, rx=3, sw=2.4):
    return ('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="#ffffff" '
            'stroke="%s" stroke-width="%s"/>\n' % (x, y, w, h, rx, A, sw))


def fill(x, y, w, h, rx, op):
    return ('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="%s" '
            'fill-opacity="%s"/>\n' % (x, y, w, h, rx, A, op))


def led(cx, cy, r=1.9, op=1):
    return ('<circle cx="%s" cy="%s" r="%s" fill="%s" fill-opacity="%s"/>\n'
            % (cx, cy, r, A, op))


def line(x1, y1, x2, y2, sw=2, op=1):
    return ('<path d="M%s %sL%s %s" stroke="%s" stroke-width="%s" '
            'stroke-opacity="%s"/>\n' % (x1, y1, x2, y2, A, sw, op))


def switch():
    s = body(3, 21, 58, 22, 3)
    s += fill(3, 21, 58, 5.5, 3, 0.16)
    s += '<path d="M3 26.5h58" stroke="%s" stroke-width="1.2" stroke-opacity=".45"/>\n' % A
    for row, top in enumerate((30.0, 36.6)):
        for i in range(7):
            s += fill(8.5 + i * 6.4, top, 5.0, 4.4, 0.8, 0.62 if row == 0 else 0.42)
    s += led(55.5, 32.2, 2.0, 0.95)
    s += led(55.5, 38.8, 2.0, 0.5)
    s += '<path d="M9 21v-4M55 21v-4" stroke="%s" stroke-width="2" stroke-opacity=".0"/>\n' % A
    return s


def server():
    s = ""
    for top in (12.0, 26.0, 40.0):
        s += body(9, top, 46, 12, 2.2, 2.2)
        s += led(15, top + 6, 2.0, 0.9)
        s += led(21, top + 6, 2.0, 0.35)
        for i in range(4):
            s += line(32 + i * 5, top + 3, 32 + i * 5, top + 9, 1.6, 0.4)
    return s


def router():
    s = body(3, 18, 58, 28, 14, 2.5)
    s += fill(3, 18, 58, 28, 14, 0.07)
    s += ('<path d="M14 27h28m0 0l-6.5-5m6.5 5l-6.5 5" stroke="%s" '
          'stroke-width="2.6"/>\n' % A)
    s += ('<path d="M50 37H22m0 0l6.5-5m-6.5 5l6.5 5" stroke="%s" '
          'stroke-width="2.6" stroke-opacity=".55"/>\n' % A)
    return s


def bmc():
    s = body(5, 11, 54, 14, 2.4, 2.2)
    s += led(12, 18, 2.0, 0.9)
    s += led(18, 18, 2.0, 0.35)
    for i in range(4):
        s += line(36 + i * 5, 14.5, 36 + i * 5, 21.5, 1.6, 0.4)
    s += body(20, 33, 24, 22, 3, 2.3)
    s += fill(26, 39, 12, 10, 1.5, 0.5)
    for i in range(3):
        s += line(20, 38 + i * 7, 14, 38 + i * 7, 2, 0.75)
        s += line(44, 38 + i * 7, 50, 38 + i * 7, 2, 0.75)
    s += line(32, 25, 32, 33, 2, 0.5)
    return s


def ap():
    s = ""
    for r, op in ((22, 0.3), (15.5, 0.55), (9, 0.85)):
        s += ('<path d="M%s 34A%s %s 0 0 1 %s 34" stroke="%s" stroke-width="2.4" '
              'stroke-opacity="%s"/>\n' % (32 - r, r, r, 32 + r, A, op))
    s += body(14, 38, 36, 15, 4, 2.4)
    s += led(21, 45.5, 2.1, 0.9)
    s += fill(28, 43.5, 16, 4, 1.2, 0.35)
    return s


def phone():
    s = body(9, 33, 46, 21, 3.5, 2.4)
    s += fill(31, 38, 18, 11, 1.8, 0.3)
    for row in range(3):
        for col in range(3):
            s += led(16.5 + col * 6, 39 + row * 5.5, 1.7, 0.6)
    s += body(11, 17, 42, 11, 5.5, 2.4)
    s += fill(14, 19.5, 8, 6, 2, 0.45)
    s += fill(42, 19.5, 8, 6, 2, 0.45)
    s += line(32, 28, 32, 33, 2, 0.55)
    return s


def unknown():
    s = ('<rect x="11" y="13" width="42" height="38" rx="5" fill="#ffffff" '
         'stroke="%s" stroke-width="2.4" stroke-dasharray="6 4"/>\n' % A)
    s += ('<text x="32" y="42" text-anchor="middle" font-family="Segoe UI,'
          'Arial,sans-serif" font-size="26" font-weight="600" fill="%s" '
          'fill-opacity=".8">?</text>\n' % A)
    return s


ICONS = {"switch": switch, "host": server, "router": router, "bmc": bmc,
         "ap": ap, "phone": phone, "unknown": unknown}

for name, draw in ICONS.items():
    path = os.path.join(OUT, name + ".svg")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(head(name) + draw() + TAIL)
    print("%-10s %5d bytes  %s" % (name, os.path.getsize(path), path))

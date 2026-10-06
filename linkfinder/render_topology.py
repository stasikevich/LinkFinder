"""Turn a topology JSON into one self-contained HTML page.

Takes data, never a device: discovery -> topology JSON -> renderer -> HTML, so
the same JSON can feed another renderer or an external tool. The page carries
both the data and cytoscape.min.js inside it, because a file:// page may not
fetch a sibling file and must not depend on the network.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from typing import Any, Dict, Optional

from . import __author__, __title__, __version__

KIND_STYLE = {
    "switch": ["#1f77b4", "round-rectangle"],
    "router": ["#9467bd", "diamond"],
    "host": ["#2ca02c", "ellipse"],
    "bmc": ["#ff7f0e", "hexagon"],
    "ap": ["#17becf", "triangle"],
    "phone": ["#8c564b", "vee"],
    "unknown": ["#7f7f7f", "ellipse"],
}

KIND_SIZE = {
    "switch": [62, 30],
    "router": [56, 32],
    "host": [48, 43],
    "bmc": [42, 44],
    "ap": [46, 43],
    "phone": [48, 40],
    "unknown": [44, 40],
}

ICON_TINT = 0.13

STATE_BORDER = {
    "scanned": ["#1a1a1a", 2],
    "unreachable": ["#d62728", 3],
    "out-of-scope": ["#a0aec0", 3],
    "limit-reached": ["#a0aec0", 3],
    "unscanned": ["#999999", 1],
}

EDGE_STYLE = {
    "confirmed": ["#2b6cb0", "solid", 3],
    "reported": ["#4a5568", "dashed", 2],
    "inferred": ["#a0aec0", "dotted", 2],
}


def assets_dir() -> str:
    """PyInstaller unpacks bundled data beside the module under _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        packed = os.path.join(base, "linkfinder", "assets")
        if os.path.isdir(packed):
            return packed
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _asset(name: str) -> str:
    path = os.path.join(assets_dir(), name)
    if not os.path.exists(path):
        raise RuntimeError("missing asset %s in %s - the HTML has to carry it so the "
                           "page opens offline" % (name, assets_dir()))
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tint(colour: str, keep: float = ICON_TINT) -> str:
    """Wash a kind colour out towards white so an icon stays readable on it."""
    try:
        parts = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return "#ffffff"
    return "#%02x%02x%02x" % tuple(
        int(round(255 - (255 - c) * keep)) for c in parts)


_ICON_CACHE: Dict[str, str] = {}


def icon_uris() -> Dict[str, str]:
    """Each device drawing as a base64 data URI, coloured for its kind.

    Base64 rather than inline SVG text keeps the markup out of the JSON and
    keeps the xmlns URL out of the page, so the offline checks stay honest.
    """
    if not _ICON_CACHE:
        for kind, (colour, _shape) in KIND_STYLE.items():
            svg = _asset(os.path.join("icons", kind + ".svg"))
            svg = svg.replace("__ACCENT__", colour)
            packed = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            _ICON_CACHE[kind] = "data:image/svg+xml;base64," + packed
    return dict(_ICON_CACHE)


def node_label(node: Dict[str, Any]) -> str:
    ips = node.get("mgmt_ips") or []
    return (node.get("name") or (ips[0] if ips else "") or node.get("chassis_id")
            or node.get("id") or "?")


def to_elements(topology: Dict[str, Any]) -> Dict[str, Any]:
    """Topology JSON into the nodes/edges arrays Cytoscape expects."""
    nodes = []
    for node in topology.get("nodes", []):
        kind = node.get("kind") or "unknown"
        if kind not in KIND_STYLE:
            kind = "unknown"
        colour, shape = KIND_STYLE[kind]
        icon_w, icon_h = KIND_SIZE[kind]
        border, width = STATE_BORDER.get(node.get("state") or "unscanned",
                                         STATE_BORDER["unscanned"])
        nodes.append({"data": dict(node, label=node_label(node), colour=colour,
                                   shape=shape, border=border, borderWidth=width,
                                   icon=kind, tint=tint(colour), iconW=icon_w,
                                   iconH=icon_h)})
    edges = []
    for edge in topology.get("edges", []):
        colour, style, width = EDGE_STYLE.get(edge.get("confidence") or "reported",
                                              EDGE_STYLE["reported"])
        label = "%s - %s" % (edge.get("a_iface") or "?", edge.get("b_iface") or "?")
        edges.append({"data": dict(edge, source=edge.get("a"), target=edge.get("b"),
                                   label=label, colour=colour, lineStyle=style,
                                   width=width)})
    return {"nodes": nodes, "edges": edges}


def summarise(topology: Dict[str, Any]) -> str:
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    scanned = sum(1 for n in nodes if n.get("state") == "scanned")
    confirmed = sum(1 for e in edges if e.get("confidence") == "confirmed")
    return "%d devices (%d scanned), %d links (%d confirmed)" % (
        len(nodes), scanned, len(edges), confirmed)


JS_UNSAFE = (
    ("<", r"\u003c"),
    (">", r"\u003e"),
    ("&", r"\u0026"),
    (chr(0x2028), r"\u2028"),
    (chr(0x2029), r"\u2029"),
)


def embed_json(value: Any) -> str:
    """A device name holding a closing script tag would end the script block."""
    text = json.dumps(value, ensure_ascii=False)
    for raw, escaped in JS_UNSAFE:
        text = text.replace(raw, escaped)
    return text


TOKEN_RE = re.compile(r"__[A-Z_]+__")


def render_html(topology: Dict[str, Any], library: Optional[str] = None,
                template: Optional[str] = None) -> str:
    page = template if template is not None else _asset("topology.html")
    values = {
        "__BYLINE__": html.escape("%s %s by %s"
                                  % (__title__, __version__, __author__)),
        "__SEED__": html.escape(str(topology.get("seed") or "-")),
        "__GENERATED__": html.escape(str(topology.get("generated_at") or "-")),
        "__SUMMARY__": html.escape(summarise(topology)),
        "__KIND_STYLE__": embed_json(KIND_STYLE),
        "__EDGE_STYLE__": embed_json(EDGE_STYLE),
        "__ICONS__": embed_json(icon_uris()),
        "__ELEMENTS__": embed_json(to_elements(topology)),
        "__DATA__": embed_json(topology),
        "__CYTOSCAPE__": library if library is not None else _asset("cytoscape.min.js"),
    }
    return TOKEN_RE.sub(lambda m: values.get(m.group(0), m.group(0)), page)


def write_html(topology: Dict[str, Any], path: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_html(topology))
    return path


def render_file(json_path: str, html_path: str) -> str:
    """The published entry point: a topology JSON on disk becomes an HTML page."""
    from .topology import load_topology

    return write_html(load_topology(json_path), html_path)

"""Build the help page: one self-contained HTML file, from the real sources.

Two things are joined here. The prose is DOCUMENTATION.md, written by hand.
The command reference is read out of the argument parser itself, so a flag
that exists is documented and a flag that is documented exists - a page that
can drift from the program is worse than no page, because it is believed.

The page carries no script, no font and no address: it must open from a USB
stick on a machine with no network, like everything else this tool writes.

Usage:  python tools/make_help.py [output.html]

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import io
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CODE_MARK = "\x00CODE%d\x00"


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def inline(text: str) -> str:
    """Code spans first, and they keep everything else out of themselves."""
    spans = []

    def hold(match):
        spans.append(match.group(1))
        return "\x01%d\x01" % (len(spans) - 1)

    text = re.sub(r"`([^`]+)`", hold, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)),
                  text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    for index, held in enumerate(spans):
        text = text.replace("\x01%d\x01" % index, "<code>%s</code>" % held)
    return text


def slug(text: str) -> str:
    made = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return made or "section"


def table(rows):
    """A markdown table, with the separator row dropped."""
    cells = [[c.strip() for c in row.strip().strip("|").split("|")]
             for row in rows]
    body = [r for r in cells[1:] if not all(re.match(r"^:?-{2,}:?$", c) or not c
                                            for c in r)]
    out = ["<table><thead><tr>"]
    out += ["<th>%s</th>" % inline(c) for c in cells[0]]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in row)
                   + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def convert(markdown: str):
    """Return (html, [(level, title, anchor)]) for the body and the contents."""
    blocks = []

    def keep_code(match):
        blocks.append(match.group(2))
        return CODE_MARK % (len(blocks) - 1)

    text = re.sub(r"```([a-zA-Z0-9]*)\n(.*?)```", keep_code, markdown,
                  flags=re.S)
    lines = text.split("\n")
    html, contents = [], []
    index, seen = 0, set()
    while index < len(lines):
        line = lines[index]
        bare = line.strip()

        held = re.match(chr(0) + r"CODE(\d+)" + chr(0) + r"$", bare)
        if held:
            html.append("<pre><code>%s</code></pre>"
                        % escape(blocks[int(held.group(1))].rstrip("\n")))
            index += 1
            continue

        head = re.match(r"^(#{1,6})\s+(.*)$", line)
        if head:
            level, title = len(head.group(1)), head.group(2).strip()
            anchor = slug(title)
            while anchor in seen:
                anchor += "-x"
            seen.add(anchor)
            if level in (2, 3):
                contents.append((level, title, anchor))
            mark = ('<a class="anchor" href="#%s" '
                    'aria-label="link to this section">#</a>' % anchor
                    if level in (2, 3) else "")
            html.append('<h%d id="%s">%s%s</h%d>'
                        % (level, anchor, inline(escape(title)), mark, level))
            index += 1
            continue

        if bare.startswith("|") and bare.count("|") > 1:
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(escape(lines[index]))
                index += 1
            html.append(table(rows))
            continue

        if re.match(r"^-{3,}$", bare):
            html.append("<hr>")
            index += 1
            continue

        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        number = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if bullet or number:
            tag = "ul" if bullet else "ol"
            items = []
            pattern = r"^\s*[-*]\s+(.*)$" if bullet else r"^\s*\d+\.\s+(.*)$"
            while index < len(lines):
                got = re.match(pattern, lines[index])
                if got:
                    items.append(got.group(1))
                elif lines[index].startswith("  ") and lines[index].strip() and items:
                    items[-1] += " " + lines[index].strip()
                else:
                    break
                index += 1
            html.append("<%s>%s</%s>"
                        % (tag, "".join("<li>%s</li>" % inline(escape(i))
                                        for i in items), tag))
            continue

        if bare.startswith(">"):
            said = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                said.append(lines[index].strip().lstrip(">").strip())
                index += 1
            html.append("<blockquote>%s</blockquote>"
                        % inline(escape(" ".join(said))))
            continue

        if not bare:
            index += 1
            continue

        paragraph = []
        while index < len(lines) and lines[index].strip() \
                and not re.match(r"^(#{1,6}\s|\s*[-*]\s|\s*\d+\.\s|\||>|-{3,}$)",
                                 lines[index].strip()) \
                and "\x00CODE" not in lines[index]:
            paragraph.append(lines[index].strip())
            index += 1
        if paragraph:
            html.append("<p>%s</p>" % inline(escape(" ".join(paragraph))))
        else:
            index += 1
    return "\n".join(html), contents


def command_reference():
    """Every flag, read from the parser, so the page cannot invent or omit."""
    from linkfinder.cli import build_parser

    parser = build_parser()
    out = ["<h2 id=\"every-option\">Every command-line option</h2>",
           "<p>Read from the program itself when this page was built, so it "
           "lists exactly what this version accepts.</p>"]
    groups = [g for g in parser._action_groups if g._group_actions]
    for group in groups:
        rows = []
        for action in group._group_actions:
            shown = action.metavar or action.dest
            if isinstance(shown, (tuple, list)):
                shown = " ".join(shown)
            names = ", ".join(action.option_strings) or str(shown)
            if action.help == "==SUPPRESS==":
                continue
            takes = ""
            if action.option_strings and action.nargs != 0:
                # an option taking two values carries a tuple metavar
                shown = action.metavar or (action.dest or "").upper()
                takes = (" ".join(shown) if isinstance(shown, (tuple, list))
                         else str(shown))
            rows.append((names, takes, action.help or ""))
        if not rows:
            continue
        title = (group.title or "options").strip()
        out.append('<h3 id="opt-%s">%s</h3>' % (slug(title), escape(title)))
        out.append("<table><thead><tr><th>option</th><th>takes</th>"
                   "<th>what it does</th></tr></thead><tbody>")
        for names, takes, says in rows:
            out.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
                       % (escape(names), escape(takes), inline(escape(says))))
        out.append("</tbody></table>")
    return "\n".join(out), [(2, "Every command-line option", "every-option")]


STYLE = """
/* Type first. A help page is read, not admired: 16px body, 1.7 leading and a
   measure of about 70 characters, which is where reading speed peaks. */
:root {
  --ink:#1b1f24; --dim:#56606d; --faint:#7b8694;
  --line:#e3e7ec; --soft:#f7f9fb; --raise:#fff;
  --accent:#0b5cad; --accent-soft:#e8f0fa;
  --note:#0b5cad; --warn:#9a5b00; --warn-soft:#fff6e8;
  --mono:ui-monospace,"Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; scroll-padding-top:72px; }
body { margin:0; background:var(--raise); color:var(--ink);
  font:16px/1.7 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased; }

/* Skip link: the first thing a keyboard reaches should be a way past the nav */
.skip { position:absolute; left:-9999px; top:0; background:var(--accent);
  color:#fff; padding:10px 16px; z-index:50; border-radius:0 0 6px 0; }
.skip:focus { left:0; }

#top { position:sticky; top:0; z-index:30; display:flex; align-items:center;
  gap:14px; padding:10px 22px; background:rgba(255,255,255,.93);
  backdrop-filter:blur(6px); border-bottom:1px solid var(--line); }
#top .name { font-weight:650; letter-spacing:-.01em; }
#top .ver { color:var(--faint); font-size:13px; }
#top .spacer { flex:1; }
#top a.jump { font-size:13px; color:var(--accent); text-decoration:none;
  border:1px solid var(--line); border-radius:999px; padding:4px 12px; }
#top a.jump:hover { background:var(--accent-soft); }

#wrap { display:flex; align-items:flex-start; }
#contents { position:sticky; top:49px; width:298px; min-width:298px;
  height:calc(100vh - 49px); overflow:auto; padding:18px 14px 60px;
  border-right:1px solid var(--line); background:var(--soft); }
#find { width:100%; padding:7px 10px; font:13px inherit; margin-bottom:12px;
  border:1px solid var(--line); border-radius:6px; background:var(--raise); }
#find:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
#contents a { display:block; padding:4px 10px; margin:1px 0; font-size:13.5px;
  color:var(--dim); text-decoration:none; border-radius:5px;
  border-left:3px solid transparent; }
#contents a:hover { background:var(--accent-soft); color:var(--accent); }
#contents a.l3 { padding-left:24px; font-size:13px; color:var(--faint); }
#contents a.here { background:var(--accent-soft); color:var(--accent);
  border-left-color:var(--accent); font-weight:600; }
#contents .none { color:var(--faint); font-size:13px; padding:8px 10px; }

main { flex:1; min-width:0; padding:30px 48px 120px; }
/* The measure applies to prose, where it decides reading speed. A table or a
   command line is not prose: capping those to 72 characters wraps columns
   that were readable and wastes the width beside them. */
.measure { max-width:1120px; }
.measure > p, .measure > ul, .measure > ol, .measure > blockquote,
.measure > h1, .measure > h2, .measure > h3, .measure > h4 { max-width:72ch; }
.measure > table, .measure > pre { max-width:100%; }
h1 { font-size:34px; line-height:1.2; margin:6px 0 4px; letter-spacing:-.02em; }
h2 { font-size:25px; line-height:1.25; margin:52px 0 12px; padding-top:18px;
  border-top:1px solid var(--line); letter-spacing:-.01em; }
h3 { font-size:18.5px; margin:32px 0 8px; }
h4 { font-size:16px; margin:22px 0 6px; color:var(--dim); }
h2:first-of-type { border-top:0; margin-top:18px; }
/* An anchor on every heading, shown on hover - the way to link a colleague
   to one answer rather than to the whole page. */
.anchor { color:var(--faint); text-decoration:none; margin-left:8px;
  opacity:0; font-weight:400; font-size:.8em; }
h2:hover .anchor, h3:hover .anchor, .anchor:focus { opacity:1; }
p { margin:12px 0; }
a { color:var(--accent); text-decoration-thickness:1px;
  text-underline-offset:2px; }
ul,ol { margin:12px 0; padding-left:26px; }
li { margin:5px 0; }
code { font:13.5px/1.5 var(--mono); background:var(--soft);
  border:1px solid var(--line); border-radius:4px; padding:1px 5px; }
pre { background:#1b1f24; color:#e6edf3; border-radius:8px; padding:14px 16px;
  overflow:auto; margin:14px 0; max-width:none; }
pre code { background:none; border:0; padding:0; color:inherit;
  font-size:13px; }
/* Width by content: a two-column table stretched across the page puts
   a hand's width of nothing between a term and its meaning. */
table { border-collapse:collapse; width:auto; max-width:100%;
  min-width:min(100%,560px); margin:16px 0; font-size:14.5px; }
th,td { border-bottom:1px solid var(--line); padding:9px 12px; text-align:left;
  vertical-align:top; }
thead th { background:var(--soft); border-bottom:2px solid var(--line);
  font-weight:600; white-space:nowrap; }
tbody tr:hover { background:var(--soft); }
blockquote { margin:16px 0; padding:12px 16px; background:var(--warn-soft);
  border-left:4px solid var(--warn); border-radius:0 6px 6px 0; }
blockquote p:first-child { margin-top:0; }
blockquote p:last-child { margin-bottom:0; }
hr { border:0; border-top:1px solid var(--line); margin:30px 0; }

#up { position:fixed; right:26px; bottom:26px; display:none; z-index:40;
  background:var(--raise); border:1px solid var(--line); border-radius:999px;
  padding:9px 16px; font:13px inherit; color:var(--accent); cursor:pointer;
  box-shadow:0 2px 10px rgba(0,0,0,.09); }
#up:hover { background:var(--accent-soft); }
#up.on { display:block; }
.stamp { color:var(--faint); font-size:13px; margin-top:56px;
  border-top:1px solid var(--line); padding-top:16px; }

@media (max-width:980px) {
  #contents { display:none; }
  main { padding:22px 20px 90px; }
}
@media print {
  #contents, #top, #up, .anchor { display:none; }
  main { padding:0; } .measure { max-width:none; }
  pre { background:#f4f4f4; color:#000; border:1px solid #ccc; }
}
"""

# Inline only: the page must work from a disk with no network, so nothing is
# fetched - but navigation a reader expects on a long page is not a luxury.
SCRIPT = """
(function () {
  var side = document.getElementById('contents');
  var links = [].slice.call(side.querySelectorAll('a[href^="#"]'));
  var find = document.getElementById('find');
  var up = document.getElementById('up');

  find.addEventListener('input', function () {
    var want = find.value.toLowerCase().trim();
    var shown = 0;
    links.forEach(function (a) {
      var hit = !want || a.textContent.toLowerCase().indexOf(want) >= 0;
      a.style.display = hit ? '' : 'none';
      if (hit) { shown++; }
    });
    document.getElementById('nohit').style.display = shown ? 'none' : 'block';
  });

  var marks = links.map(function (a) {
    return document.getElementById(a.getAttribute('href').slice(1));
  }).filter(Boolean);

  function here() {
    var best = 0;
    for (var i = 0; i < marks.length; i++) {
      if (marks[i].getBoundingClientRect().top <= 90) { best = i; }
    }
    links.forEach(function (a, i) { a.className =
      a.className.replace(/ ?here/, '') + (i === best ? ' here' : ''); });
    if (links[best] && links[best].offsetTop < side.scrollTop) {
      side.scrollTop = links[best].offsetTop - 60;
    }
    up.className = window.scrollY > 500 ? 'on' : '';
  }
  var waiting = false;
  window.addEventListener('scroll', function () {
    if (waiting) { return; }
    waiting = true;
    window.requestAnimationFrame(function () { here(); waiting = false; });
  });
  up.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  here();
})();
"""



def build(destination: str) -> str:
    from linkfinder import __version__

    prose = io.open(os.path.join(ROOT, "DOCUMENTATION.md"),
                    encoding="utf-8").read()
    body, contents = convert(prose)
    reference, more = command_reference()
    contents += more

    nav = "\n".join('<a class="l%d" href="#%s">%s</a>' % (level, anchor,
                                                          escape(title))
                    for level, title, anchor in contents)
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkFinder %s - help</title>
<style>%s</style></head>
<body>
<a class="skip" href="#start">Skip to the guide</a>
<header id="top">
  <span class="name">LinkFinder</span>
  <span class="ver">%s &middot; help</span>
  <span class="spacer"></span>
  <a class="jump" href="#contents">Contents</a>
  <a class="jump" href="#start">Top</a>
</header>
<div id="wrap">
<nav id="contents" aria-label="Contents">
  <input id="find" type="search" placeholder="Filter these sections"
         aria-label="Filter sections">
%s
  <div class="none" id="nohit" style="display:none">nothing matches that</div>
</nav>
<main id="start"><div class="measure">
%s
%s
<p class="stamp">Built from DOCUMENTATION.md and the program's own options on
%s. This page carries no address and fetches nothing - it opens from a disk
with the network off.<br>Idea, design and architecture by Stas Mitin.</p>
</div></main></div>
<button id="up" type="button">&uarr; Top</button>
<script>%s</script>
</body></html>
""" % (escape(__version__), STYLE, escape(__version__), nav, body, reference,
       time.strftime("%d %b %Y"), SCRIPT)

    folder = os.path.dirname(os.path.abspath(destination))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(destination, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(page)
    return destination


def main() -> int:
    # The page lives in assets so it is inside the binary, and a copy goes to
    # docs/ for anyone reading the source tree.
    out = (sys.argv[1] if len(sys.argv) > 1
           else os.path.join(ROOT, "linkfinder", "assets", "help.html"))
    where = build(out)
    print("help page written to %s (%d KB)"
          % (where, os.path.getsize(where) // 1024))
    if len(sys.argv) <= 1:
        import shutil

        docs = os.path.join(ROOT, "docs")
        if not os.path.isdir(docs):
            os.makedirs(docs)
        shutil.copyfile(where, os.path.join(docs, "help.html"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

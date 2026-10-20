"""What a host already recorded about the radios attached to it.

When a radio's M-plane is down there is no NETCONF to ask it, but the host it
is attached to logged everything it ever learned from the radio - often the
same device-info the radio would return over NETCONF (model, serial, revision)
and the MAC it answers on. That costs no packets and needs no login to the
radio. Where to look and how each fact is written is site-specific, so the
rules are handed in (the internal build carries them in its bundle); this
reader holds no path and no site knowledge of its own, it applies whatever
globs and patterns it is given.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any, Callable, Dict, List, Optional

Run = Callable[[str], str]


def read(run: Run, rules: List[Dict[str, str]],
         log: Optional[Callable[[str], None]] = None) -> List[str]:
    """Serials lifted off a host's files, per the rules, newest-looking first.

    Only filenames are read - never the contents - so this is as cheap as one
    `ls` per rule and tells the radio nothing.
    """
    serials: List[str] = []
    seen = set()
    for rule in rules or []:
        glob = str(rule.get("glob") or "")
        if not glob:
            continue
        prefix = str(rule.get("prefix") or "")
        try:
            out = run("sh -c %s" % shlex.quote("ls -1 %s 2>/dev/null" % glob)) or ""
        except Exception:
            continue
        for line in out.splitlines():
            base = os.path.basename(line.strip())
            if prefix:
                if not base.startswith(prefix):
                    continue              # a name the prefix does not fit is not ours
                base = base[len(prefix):]
            match = re.match(r"[A-Za-z0-9]{4,}", base)   # the serial-ish head
            if match and match.group(0) not in seen:
                seen.add(match.group(0))
                serials.append(match.group(0))
    if serials and log:
        log("  radios in this host's logs: %s" % ", ".join(serials))
    return serials


def _macs(value: str) -> List[str]:
    """A MAC as colon-lower, plus its U/L-flipped twin.

    A radio logs the MAC it answers on in whichever form the firmware chose:
    the burned-in address, or the EUI-64 form with the locally-administered bit
    flipped (the same address a fe80 link-local derives from). A node is keyed
    by one of them; returning both lets the join match either.
    """
    hexes = re.findall(r"[0-9a-fA-F]{2}", str(value or ""))
    if len(hexes) != 6:
        return []
    low = [h.lower() for h in hexes]
    out = [":".join(low)]
    try:
        flipped = low[:]
        flipped[0] = "%02x" % (int(low[0], 16) ^ 0x02)
        if flipped != low:
            out.append(":".join(flipped))
    except ValueError:
        pass
    return out


def details(run: Run, rules: List[Dict[str, Any]],
            log: Optional[Callable[[str], None]] = None
            ) -> Dict[str, Dict[str, str]]:
    """What the host's own files say about each radio, keyed by its serial.

    Two kinds of rule are applied, both site-specific and handed in:
      - `fields`: read a per-radio file and lift its device-info (model,
        serial, revisions) off the lines a `fields` map describes.
      - `macmap`: read a log and pair each serial with the MAC the radio
        answers on, so the facts can later be hung on the right node.
    Each returned record carries its serial and, when known, `macs` (the forms
    a node might be keyed by).
    """
    say = log or (lambda _m: None)
    by_serial: Dict[str, Dict[str, Any]] = {}
    macs_of: Dict[str, List[str]] = {}
    for rule in rules or []:
        glob = str(rule.get("glob") or "")
        if not glob:
            continue
        fields = rule.get("fields")
        macmap = rule.get("macmap")
        if isinstance(fields, dict):
            block = str(rule.get("block") or "")
            prefix = str(rule.get("prefix") or "")
            try:
                names = (run("sh -c %s"
                             % shlex.quote("ls -1 %s 2>/dev/null" % glob)) or "")
            except Exception:
                names = ""
            for path in names.splitlines():
                path = path.strip()
                if not path:
                    continue
                # One block only: a file holds the same device-info block each
                # time the radio re-registers, and reading all of them at once
                # let one block's Model land next to another's Version. -m1 -A
                # takes the first block whole and nothing after it.
                reader = ("grep -aA11 -m1 %s %s" % (shlex.quote(block), shlex.quote(path))
                          if block else "head -c 4000 %s" % shlex.quote(path))
                try:
                    text = run("sh -c %s" % shlex.quote("%s 2>/dev/null" % reader)) or ""
                except Exception:
                    continue
                rec = {}
                for key, pat in fields.items():
                    found = re.search(str(pat), text)
                    if found:
                        rec[key] = found.group(1).strip()
                serial = rec.pop("serial", "")
                if not re.match(r"[A-Za-z0-9]{6,}$", serial or ""):
                    # Not a serial - a mis-read, or the block's Serial line was
                    # absent. Fall back to the serial in the file's own name.
                    base = os.path.basename(path)
                    serial = base[len(prefix):] if prefix and base.startswith(prefix) else ""
                if re.match(r"[A-Za-z0-9]{6,}$", serial or ""):
                    by_serial.setdefault(serial, {}).update(
                        {k: v for k, v in rec.items() if v})
        if isinstance(macmap, dict):
            pattern = str(macmap.get("re") or "")
            keyword = str(macmap.get("keyword") or "")
            if not pattern:
                continue
            # Only the tail of each matched file: a radio's current MAC mapping
            # is in the recent log, and grepping a multi-gigabyte `messages` end
            # to end for a sparse MAC line was the bulk of a BBU scan's time. The
            # glob is the (trusted) rule's own, so it is expanded by the shell.
            sel = shlex.quote(keyword) if keyword else shlex.quote(pattern)
            flag = "-a" if keyword else "-aE"
            grep = ('for f in %s; do tail -c 4000000 "$f" 2>/dev/null; done | '
                    'grep %s %s' % (glob, flag, sel))
            try:
                text = run("sh -c %s"
                           % shlex.quote("%s 2>/dev/null | head -4000" % grep)) or ""
            except Exception:
                text = ""
            for line in text.splitlines():
                m = re.search(pattern, line)
                if m:
                    serial, forms = m.group(1).strip(), _macs(m.group(2))
                    if serial and forms:
                        macs_of.setdefault(serial, forms)
    for serial, rec in by_serial.items():
        rec["serial"] = serial
        if serial in macs_of:
            rec["macs"] = macs_of[serial]
    if by_serial:
        say("  radio device-info from this host's logs: %d radio(s)"
            % len(by_serial))
    return by_serial

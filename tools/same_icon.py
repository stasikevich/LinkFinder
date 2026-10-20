"""Both front ends must wear the same icon, and it must be the one on disk.

This has gone wrong three times, always the same way: the two binaries are
built a minute apart by the same command, so nobody thinks to check, and
Windows then shows them with different icons. Twice that was the shell's
icon cache lying; a gate that measures the files says so in a second
instead of costing an afternoon.

Comparing one extracted size is not enough. Explorer picks whichever size
fits the view, so two files can agree at 32x32 and differ on screen. Every
image in the group is hashed.

Run: python tools/same_icon.py dist/a.exe dist/b.exe [branding/icon.ico]

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import hashlib
import io
import os
import struct
import sys
from typing import List, Optional, Tuple

RT_ICON, RT_GROUP_ICON = 3, 14

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _at(raw: bytes, off: int, fmt: str) -> Tuple:
    return struct.unpack(fmt, raw[off:off + struct.calcsize(fmt)])


def _sections(raw: bytes) -> Tuple[Optional[int], List[Tuple[int, int, int]]]:
    pe = struct.unpack("<I", raw[0x3C:0x40])[0]
    if raw[pe:pe + 4] != b"PE\0\0":
        return None, []
    count, = _at(raw, pe + 6, "<H")
    opt_size, = _at(raw, pe + 20, "<H")
    magic, = _at(raw, pe + 24, "<H")
    directory = pe + 24 + (112 if magic == 0x20B else 96)
    res_rva, _size = _at(raw, directory + 2 * 8, "<II")
    first = pe + 24 + opt_size
    found = []
    for i in range(count):
        head = first + i * 40
        found.append((_at(raw, head + 12, "<I")[0],
                      _at(raw, head + 16, "<I")[0],
                      _at(raw, head + 20, "<I")[0]))
    return res_rva, found


def _offset(rva: int, sections: List[Tuple[int, int, int]]) -> Optional[int]:
    for start, size, on_disk in sections:
        if start <= rva < start + size:
            return on_disk + (rva - start)
    return None


def _walk(raw, base, off, sections, level=0, wanted=None, out=None):
    out = [] if out is None else out
    named, numbered = _at(raw, off + 12, "<HH")
    for i in range(named + numbered):
        name, child = _at(raw, off + 16 + i * 8, "<II")
        if child & 0x80000000:
            if level == 0 and wanted is not None \
                    and not name & 0x80000000 and name != wanted:
                continue
            _walk(raw, base, base + (child & 0x7FFFFFFF), sections,
                  level + 1, wanted, out)
        else:
            rva, size = _at(raw, base + child, "<II")
            start = _offset(rva, sections)
            if start is not None:
                out.append(raw[start:start + size])
    return out


def group_of(path: str) -> List[Tuple]:
    """The directory Explorer actually reads to choose a size.

    The images can be identical while the group that indexes them is not,
    and the shell believes the group. Its resource ids are left out: two
    builds may number the same artwork differently and still be right.
    """
    raw = io.open(path, "rb").read()
    res_rva, sections = _sections(raw)
    if res_rva is None:
        return []
    base = _offset(res_rva, sections)
    out = []
    for blob in _walk(raw, base, base, sections, 0, RT_GROUP_ICON):
        count, = struct.unpack("<H", blob[4:6])
        for i in range(count):
            at = 6 + i * 14
            out.append(struct.unpack("<BBBBHHI", blob[at:at + 12]))
    return sorted(out)


def from_exe(path: str) -> List[bytes]:
    """Every RT_ICON image stored in a PE file."""
    raw = io.open(path, "rb").read()
    res_rva, sections = _sections(raw)
    if res_rva is None:
        return []
    base = _offset(res_rva, sections)
    return _walk(raw, base, base, sections, 0, RT_ICON)


def from_ico(path: str) -> List[bytes]:
    """Every image in an .ico, as the linker would copy them in."""
    raw = io.open(path, "rb").read()
    _zero, kind, count = struct.unpack("<HHH", raw[:6])
    if kind != 1:
        return []
    out = []
    for i in range(count):
        size, where = struct.unpack("<II", raw[6 + i * 16 + 8:6 + i * 16 + 16])
        out.append(raw[where:where + size])
    return out


def describe(blob: bytes) -> str:
    """The size and depth a reader would recognise, for the failure message."""
    if blob[:8] == PNG_MAGIC:
        wide, high = struct.unpack(">II", blob[16:24])
        return "%dx%d png" % (wide, high)
    if len(blob) >= 16:
        wide, high, _planes, bits = struct.unpack("<iiHH", blob[4:16])
        return "%dx%d %dbpp" % (wide, high // 2, bits)
    return "unreadable"


def fingerprint(images: List[bytes]) -> List[str]:
    return sorted("%-14s %s" % (describe(b), hashlib.sha256(b).hexdigest()[:16])
                  for b in images)


def report(label: str, images: List[bytes]) -> None:
    print("%-26s %d image(s)" % (label, len(images)))
    for line in fingerprint(images):
        print("    " + line)


def main() -> int:
    paths = sys.argv[1:]
    if len(paths) < 2:
        print(__doc__.strip().splitlines()[-4])
        return 2
    want = None
    sets = []
    for path in paths:
        if not os.path.exists(path):
            print("missing: %s" % path)
            return 1
        images = from_ico(path) if path.lower().endswith(".ico") \
            else from_exe(path)
        if not images:
            print("no icon at all in %s" % path)
            return 1
        sets.append((os.path.basename(path), fingerprint(images), images))
        want = want or fingerprint(images)

    groups = {os.path.basename(p): group_of(p) for p in paths
              if not p.lower().endswith(".ico")}
    listed = sorted(set(map(tuple, groups.values())))
    if len(listed) > 1:
        print("the icon directory differs, so the shell will pick "
              "differently: %s" % ", ".join(sorted(groups)))
        return 1

    bad = [name for name, marks, _raw in sets if marks != want]
    if bad:
        print("the icon differs between builds: %s" % ", ".join(bad))
        for name, _marks, images in sets:
            report(name, images)
        return 1
    print("icon matches across %d file(s), %d image(s) each"
          % (len(sets), len(want)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

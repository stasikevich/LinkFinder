"""Build branding/icon.ico from a single source image.

Windows picks a different image out of an .ico for every surface - 16 in a
details list, 32 on the desktop, 48 in a tile, 256 in a preview - so an icon
is a set, not a picture. Two rules make the set behave:

  - small sizes are stored as a classic DIB, not as PNG. Windows accepts
    PNG at any size since Vista, but the shell's own caches and older
    surfaces are reliable only with DIB, and a size that fails to render
    falls back to a blank square.
  - 256 is stored as PNG, because a 256 DIB costs a quarter of a megabyte
    and every Windows that reads 256 at all reads PNG.

Run: python tools/make_icon.py <source image> [output.ico]

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import os
import struct
import sys

from PIL import Image

DIB_SIZES = (16, 20, 24, 32, 40, 48, 64, 128)
PNG_SIZES = (256,)

TIGHT_UP_TO = 32
TIGHT_FILL = 0.86


def tight_crop(image):
    """The artwork with its margin taken off, for the sizes that need it.

    Logos are drawn with air around them, which is right at 256 and fatal at
    16: a mark filling 60% of a 200px tile is nine pixels across once it is
    scaled down, and nine pixels of anything is a smudge. Cropping to the
    mark buys back about a third of the width, which is the difference
    between a recognisable shape and a coloured square.

    The mark is found as the pixels that are far lighter than the plate
    behind them, which holds for a light mark on any solid or gradient
    background. Where no such mark exists the artwork is returned untouched.
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    xs, ys = [], []
    for y in range(height):
        for x in range(width):
            red, green, blue = pixels[x, y]
            if red > 200 and green > 200 and blue > 200:
                xs.append(x)
                ys.append(y)
    if len(xs) < (width * height) // 400:
        return image

    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    side = max(right - left + 1, bottom - top + 1) / TIGHT_FILL
    mid_x, mid_y = (left + right) / 2.0, (top + bottom) / 2.0
    side = min(side, width, height)
    box_x = max(0, min(width - side, mid_x - side / 2.0))
    box_y = max(0, min(height - side, mid_y - side / 2.0))
    return image.crop((int(box_x), int(box_y),
                       int(box_x + side), int(box_y + side)))


def as_dib(image):
    """One image in the shape an .ico wants: 32bpp bottom-up, then a mask.

    The header lies about the height on purpose - the format stores the
    colour rows and the transparency mask in one block and asks for their
    combined height.
    """
    width, height = image.size
    rows = []
    pixels = image.load()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            row += bytes((blue, green, red, alpha))
        rows.append(bytes(row))
    colour = b"".join(rows)

    stride = ((width + 31) // 32) * 4
    mask = b"\x00" * (stride * height)

    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                         len(colour) + len(mask), 0, 0, 0, 0)
    return header + colour + mask


def as_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build(source, target):
    original = Image.open(source).convert("RGBA")
    tight = tight_crop(original)

    def plate(size):
        art = tight if size <= TIGHT_UP_TO else original
        return art.resize((size, size), Image.LANCZOS)

    images = []
    for size in DIB_SIZES:
        images.append((size, as_dib(plate(size))))
    for size in PNG_SIZES:
        images.append((size, as_png(plate(size))))

    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    for size, blob in images:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                 1, 32, len(blob), offset)
        offset += len(blob)

    with io.open(target, "wb") as handle:
        handle.write(directory)
        for _size, blob in images:
            handle.write(blob)
    return images


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-4])
        return 2
    source = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "branding", "icon.ico")
    if not os.path.isfile(source):
        print("no such image: %s" % source)
        return 2
    made = build(source, target)
    print("%s <- %s" % (target, source))
    for size, blob in made:
        kind = "png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "dib"
        print("  %3dx%-3d %-4s %6d bytes%s"
              % (size, size, kind, len(blob),
                 "  cropped to the mark" if size <= TIGHT_UP_TO else ""))
    print("  %d bytes total" % os.path.getsize(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())

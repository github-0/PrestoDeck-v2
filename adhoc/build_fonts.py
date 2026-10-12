#!/usr/bin/env python3
"""Builds the font modules in src/fonts/ from Noto Sans and Noto Sans CJK.

Each module holds one script's glyph outlines, in the glyph format of Alright Fonts (.af), the vector
font format the Presto's PicoVector draws. The Presto copies the glyphs a screen needs from the
modules into a small .af font (see src/vectortext.py), so the modules can be any size and any of
them can be left out.

All modules share one scale, so glyphs from different modules match in size.

Usage: pip install fonttools, then python3 adhoc/build_fonts.py
The source fonts are downloaded to adhoc/font-sources/ on the first run.
"""
import math
import os
import struct
import sys
import unicodedata
import urllib.request

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "font-sources")
OUT = os.path.join(HERE, "..", "src", "fonts")

LATIN_FONT = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSans/unhinted/ttf/NotoSans-Medium.ttf"
CJK_FONT = "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJKjp-Medium.otf"
LICENSE = "https://github.com/notofonts/noto-cjk/raw/main/Sans/LICENSE"  # SIL OFL 1.1, as for Noto Sans

MAGIC = b"PDGF"  # PrestoDeck glyph file
VERSION = 1
TOLERANCE = 0.5  # max distance, in output units, between a curve and the lines that replace it
MIN_EM = 100     # fail rather than make glyphs coarser than this many units per em


def rng(*pairs):
    return [c for a, b in pairs for c in range(a, b + 1)]


def from_codec(codec, leads, trails):
    """Characters of a two-byte legacy encoding, which lists the commonly used ones."""
    chars = []
    for lead in range(*leads):
        for trail in range(*trails):
            try:
                chars.append(ord(bytes([lead, trail]).decode(codec)))
            except UnicodeDecodeError:
                pass
    return chars


def han_chars():
    """Chinese characters: GB2312 level 1 (simplified), Big5 frequent (traditional), JIS level 1 (Japanese)."""
    chars = from_codec("gb2312", (0xB0, 0xD8), (0xA1, 0xFF))
    chars += from_codec("big5", (0xA4, 0xC7), (0x40, 0xFF))
    chars += from_codec("euc_jp", (0xB0, 0xD0), (0xA1, 0xFF))
    return [c for c in chars if 0x4E00 <= c <= 0x9FFF]


# Fullwidth ASCII (U+FF01–FF5E) is mapped to plain ASCII on the Presto, so no module needs it
MODULES = {
    "latin": rng((0x20, 0x7E), (0xA0, 0x17F), (0x218, 0x21B),   # incl. Romanian ș ț
                 (0x1A0, 0x1A1), (0x1AF, 0x1B0), (0x1EA0, 0x1EF9),   # Vietnamese
                 (0x2010, 0x2027), (0x2032, 0x2033), (0x2039, 0x203A),
                 (0x20AC, 0x20AC), (0x2122, 0x2122), (0x25A1, 0x25A1),  # € ™ □
                 (0x2605, 0x2606), (0x2661, 0x2661), (0x2665, 0x2665), (0x266A, 0x266B)),  # ★ ☆ ♡ ♥ ♪ ♫
    "greek": rng((0x384, 0x3CE)),
    "cyrillic": rng((0x400, 0x45F), (0x490, 0x491)),          # incl. Ukrainian Ґ ґ
    "kana": rng((0x3041, 0x3096), (0x3099, 0x30FF), (0x31F0, 0x31FF), (0xFF61, 0xFF9F)),
    "hangul": rng((0x3131, 0x318E)) + [c for c in from_codec("euc_kr", (0xB0, 0xC9), (0xA1, 0xFF))
                                        if 0xAC00 <= c <= 0xD7A3],
    # CJK punctuation, used by Chinese and Japanese. Left out: combining tone marks and vertical repeat
    # marks, which are rare and so tall that they'd make every glyph coarser (‰ was left out for the same reason)
    "han": rng((0x3001, 0x3029), (0x3030, 0x3030), (0x3033, 0x303F)) + han_chars(),
}


def download(url):
    path = os.path.join(SOURCES, os.path.basename(url))
    if not os.path.exists(path):
        os.makedirs(SOURCES, exist_ok=True)
        print("Downloading", url)
        urllib.request.urlretrieve(url, path + ".part")
        os.rename(path + ".part", path)
    return path


class FlatteningPen(BasePen):
    """Collects a glyph's contours as polygons in font units, with curves split into lines."""

    def __init__(self, glyph_set, units_per_unit):
        super().__init__(glyph_set)
        self.tolerance = TOLERANCE * units_per_unit  # in font units
        self.contours = []

    def _moveTo(self, p):
        self.contours.append([p])

    def _lineTo(self, p):
        self.contours[-1].append(p)

    def _qCurveToOne(self, p1, p2):
        p0 = self.contours[-1][-1]
        # A quadratic curve strays at most |p0 - 2p1 + p2| / (4n²) from n equal chords
        d = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
        n = max(1, math.ceil(math.sqrt(d / (4 * self.tolerance))))
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            self.contours[-1].append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                                      u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))

    def _curveToOne(self, p1, p2, p3):
        p0 = self.contours[-1][-1]
        # A cubic curve strays at most 3/4 · max|second difference| / n² from n equal chords
        d = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
                math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
        n = max(1, math.ceil(math.sqrt(0.75 * d / self.tolerance)))
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            self.contours[-1].append(tuple(u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * e
                                           for a, b, c, e in zip(p0, p1, p2, p3)))

    def _closePath(self):
        pass

    def _endPath(self):
        pass


def to_grid(contour, scale):
    """Scales a contour to output units (y down) and drops points that add nothing on the grid."""
    pts = []
    for x, y in contour:
        p = (round(x * scale), round(-y * scale))
        if not pts or p != pts[-1]:
            pts.append(p)
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    changed = True
    while changed and len(pts) >= 3:  # remove points on a straight line between their neighbours
        changed = False
        for i in range(len(pts)):
            (ax, ay), (bx, by), (cx, cy) = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            if (bx - ax) * (cy - ay) == (by - ay) * (cx - ax):
                del pts[i]
                changed = True
                break
    return pts if len(pts) >= 3 else None


class Source:
    def __init__(self, path):
        self.font = TTFont(path, fontNumber=0)
        self.cmap = self.font.getBestCmap()
        self.glyph_set = self.font.getGlyphSet()
        self.upem = self.font["head"].unitsPerEm

    def contours(self, codepoint):
        """The glyph's contours in font units, or None if the font doesn't have it."""
        name = self.cmap.get(codepoint)
        if name is None:
            return None
        pen = FlatteningPen(self.glyph_set, self.upem / 128)
        self.glyph_set[name].draw(pen)
        return pen.contours

    def advance(self, codepoint):
        return self.font["hmtx"][self.cmap[codepoint]][0]


def glyph_record(codepoint, contours, advance, scale):
    """One glyph in .af format: its dictionary entry, its path point counts, then its points."""
    paths = [p for p in (to_grid(c, scale) for c in contours) if p]
    xs = [x for p in paths for x, _ in p] or [0]
    ys = [y for p in paths for _, y in p] or [0]
    record = struct.pack(">HbbBBBB", codepoint, min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys),
                         round(advance * scale), len(paths))
    record += b"".join(struct.pack(">H", len(p)) for p in paths)
    record += b"".join(struct.pack(">bb", x, y) for p in paths for x, y in p)
    return record


def main():
    latin, cjk = Source(download(LATIN_FONT)), Source(download(CJK_FONT))
    glyphs = {}  # module -> [(codepoint, source, contours)]
    for module, codepoints in MODULES.items():
        glyphs[module] = []
        for cp in sorted(set(codepoints)):
            if unicodedata.category(chr(cp)) == "Cn":
                continue  # unassigned, e.g. the gaps in the Greek block
            for source in (latin, cjk) if module in ("latin", "greek", "cyrillic") else (cjk,):
                contours = source.contours(cp)
                if contours is not None:
                    glyphs[module].append((cp, source, contours))
                    break
            else:
                print("  missing U+{:04X} {}".format(cp, chr(cp)))

    # One scale for all modules: as fine as the largest glyph allows, as points are stored as signed bytes
    extent = max(max(abs(v) for c in contours for p in c for v in p) / source.upem
                 for g in glyphs.values() for _, source, contours in g if contours)
    em = min(128, math.floor(127 / extent))
    if em < MIN_EM:
        sys.exit("A glyph is too large: only {} units per em would fit".format(em))
    print("Scale: {} units per em".format(em))

    os.makedirs(OUT, exist_ok=True)
    for module, entries in glyphs.items():
        records = [glyph_record(cp, contours, source.advance(cp), em / source.upem)
                   for cp, source, contours in entries]
        offsets, pos = [], 0
        for r in records:
            offsets.append(pos)
            pos += len(r)
        offsets.append(pos)
        data = MAGIC + struct.pack(">BBH", VERSION, em, len(records))
        data += b"".join(struct.pack(">H", cp) for cp, _, _ in entries)
        data += b"".join(struct.pack(">I", o) for o in offsets)
        data += b"".join(records)
        with open(os.path.join(OUT, module + ".glyphs"), "wb") as f:
            f.write(data)
        print("{:<9} {:>5} glyphs {:>6} KB".format(module, len(records), len(data) // 1024))

    # The licence requires the fonts' copyright notices to go along with them
    with urllib.request.urlopen(LICENSE) as r:
        license_text = r.read().decode()
    with open(os.path.join(OUT, "OFL.txt"), "w") as f:
        f.write("The .glyphs files contain glyph outlines converted from Noto Sans and Noto Sans CJK:\n\n")
        for source in (latin, cjk):
            f.write("{}: {}\n".format(source.font["name"].getDebugName(4), source.font["name"].getDebugName(0)))
        f.write("\n" + license_text)


if __name__ == "__main__":
    main()

"""Draws text in any script the installed font modules cover, using PicoVector.

The font modules in fonts/ (built by adhoc/build_fonts.py) hold glyph outlines, one script each. Any
module but latin can be left out. PicoVector can only load a whole font file, so the glyphs the shown
texts need are copied into a small Alright Fonts (.af) file on a RAM disk, which it then loads.

Characters no installed module has are transliterated where possible (Greek, Cyrillic, Hangul, kana),
emoji are dropped, and anything else becomes PLACEHOLDER.
"""
import os
import struct

FONT_DIR = "fonts"
RAMFS = "/ramfs"
FONT_FILE = RAMFS + "/text.af"
PLACEHOLDER = "□"  # □
MAX_GLYPHS = 250        # beyond this the font starts over with only what's needed now, to fit the RAM disk
CAP_HEIGHT = 0.714      # of Noto Sans, in ems

# Always in the font: ASCII for the app's own messages, and the placeholder
BASE = set(range(0x20, 0x7F)) | {ord(PLACEHOLDER)}


class _Module:
    """One font module file: a sorted codepoint index, offsets, then each glyph's .af record."""

    def __init__(self, path):
        self.file = open(path, "rb")
        magic, version, self.em, count = struct.unpack(">4sBBH", self.file.read(8))
        self.codepoints = self.file.read(2 * count)  # big-endian uint16 each
        self.offsets = self.file.read(4 * (count + 1))
        self.data_start = 8 + 6 * count + 4
        self.count = count

    def find(self, cp):
        """Returns the glyph's index, or -1."""
        lo, hi = 0, self.count - 1
        cps = self.codepoints
        while lo <= hi:
            mid = (lo + hi) // 2
            c = cps[2 * mid] << 8 | cps[2 * mid + 1]
            if c < cp:
                lo = mid + 1
            elif c > cp:
                hi = mid - 1
            else:
                return mid
        return -1

    def record(self, i):
        start, end = struct.unpack_from(">II", self.offsets, 4 * i)
        self.file.seek(self.data_start + start)
        return self.file.read(end - start)


class VectorText:
    def __init__(self, display):
        import picovector
        import psram
        self.vector = picovector.PicoVector(display)
        self.vector.set_antialiasing(picovector.ANTIALIAS_X4)
        self.vector.set_font_letter_spacing(100)  # the defaults squeeze letters to 95% and stretch spaces to 200%
        self.vector.set_font_word_spacing(100)
        self.modules = [_Module(FONT_DIR + "/" + name) for name in sorted(os.listdir(FONT_DIR))
                        if name.endswith(".glyphs")]
        self.em = self.modules[0].em
        try:
            os.stat(RAMFS)
        except OSError:
            psram.mkramfs(mount_point=RAMFS)
        self.advances = {}  # codepoint -> advance, for the glyphs in the loaded font
        self.size = None

    def has(self, cp):
        return any(m.find(cp) >= 0 for m in self.modules)

    def clean(self, text):
        """Returns text with every character replaced by one the modules have (see the module docstring)."""
        out = []
        for ch in text:
            cp = ord(ch)
            if 0xFF01 <= cp <= 0xFF5E:          # fullwidth ASCII
                cp -= 0xFEE0
            elif 0x1D400 <= cp <= 0x1D6A3:      # "fancy" math letters 𝐀𝓐𝔸…, 52 per style
                i = (cp - 0x1D400) % 52
                cp = (0x41 if i < 26 else 0x47) + i
            elif 0x1D7CE <= cp <= 0x1D7FF:      # math digits
                cp = 0x30 + (cp - 0x1D7CE) % 10
            elif cp == 0x3000 or 0x2000 <= cp <= 0x200A:
                cp = 0x20
            if 0x20 <= cp < 0x7F or self.has(cp):
                out.append(chr(cp))
            elif cp > 0xFFFF or cp < 0x20 or 0x300 <= cp <= 0x36F or 0x200B <= cp <= 0x200F or \
                    0xFE00 <= cp <= 0xFE0F or 0x2600 <= cp <= 0x27BF:
                pass                            # emoji and their joiners/variants, combining marks, controls
            else:
                _transliterate(cp, out)
        # Dropped characters can leave double spaces behind
        return " ".join("".join(s for s in out if s != _SMALL_TSU).split())

    def prepare(self, *texts):
        """Makes sure the loaded font has the glyphs for these (cleaned) texts."""
        needed = {ord(c) for t in texts for c in t}
        if all(c in self.advances for c in needed):
            return
        loaded = set(self.advances)
        if len(loaded | needed) > MAX_GLYPHS:
            loaded = set()
        self._load(BASE | loaded | needed)

    def _load(self, codepoints):
        dicts, counts, points = [], [], []
        path_total = point_total = 0
        self.advances = {}
        for cp in sorted(codepoints):
            for m in self.modules:
                i = m.find(cp)
                if i >= 0:
                    r = m.record(i)
                    paths = r[7]
                    dicts.append(r[:8])
                    counts.append(r[8:8 + 2 * paths])
                    points.append(r[8 + 2 * paths:])
                    path_total += paths
                    point_total += (len(r) - 8 - 2 * paths) // 2
                    self.advances[cp] = r[6]
                    break
        try:
            os.remove(FONT_FILE)  # littlefs copies on write, so make room before writing the new one
        except OSError:
            pass
        with open(FONT_FILE, "wb") as f:
            f.write(b"af!?" + struct.pack(">HHHH", 1, len(dicts), path_total, point_total))  # 1: 16-bit point counts
            for part in (dicts, counts, points):
                for b in part:
                    f.write(b)
        self.vector.set_font(FONT_FILE, 1)
        self.size = None

    def _set_size(self, px):
        """px: the em size in pixels. Returns the scale from font units to pixels."""
        size = round(px * 128 / self.em)  # PicoVector's size is the pixels per 128 font units
        if size != self.size:
            self.vector.set_font_size(size)
            self.size = size
        return size / 128

    def width(self, text, px):
        """Width of a cleaned text at em size px."""
        self.prepare(text)
        scale = self._set_size(px)
        return int(sum(self.advances.get(ord(c), 0) for c in text) * scale + 0.5)

    def draw(self, text, x, y, px):
        """Draws a cleaned text with the current pen, the middle of its capital letters at y."""
        self.prepare(text)
        self._set_size(px)
        self.vector.text(text, int(x), int(y + px * CAP_HEIGHT / 2 + 0.5))


# --- Transliteration, for characters whose module isn't installed ---

_GREEK = "A V G D E Z I Th I K L M N X O P R S S T Y F Ch Ps O".split()  # Α (U+0391) to Ω, incl. the gap at U+03A2
_GREEK_TONOS = {0x386: "A", 0x388: "E", 0x389: "I", 0x38A: "I", 0x38C: "O", 0x38E: "Y", 0x38F: "O",
                0x390: "i", 0x3AA: "I", 0x3AB: "Y", 0x3AC: "a", 0x3AD: "e", 0x3AE: "i", 0x3AF: "i",
                0x3B0: "y", 0x3CA: "i", 0x3CB: "y", 0x3CC: "o", 0x3CD: "y", 0x3CE: "o"}
_CYRILLIC = "A B V G D E Zh Z I Y K L M N O P R S T U F Kh Ts Ch Sh Shch - Y - E Yu Ya".split()  # А (U+0410) to Я
_CYRILLIC_EXTRA = "E Yo Dj G Ye Dz I Yi J Lj Nj C K I U Dz".split()  # Ѐ (U+0400) to Џ
_HANGUL_INITIAL = "g kk n d tt r m b pp s ss - j jj ch k t p h".split()
_HANGUL_VOWEL = "a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i".split()
_HANGUL_FINAL = "- k k k n n n t l k m l l l p l m p p t t ng t t k t p t".split()
# Hiragana from U+3041; katakana are the same 0x60 higher. "*": small tsu, "_x": small vowel or ya/yu/yo
_SMALL_TSU = "\x00"  # stands for a small tsu in the output until the next kana doubles its consonant
_KANA = ("_a a _i i _u u _e e _o o ka ga ki gi ku gu ke ge ko go sa za shi ji su zu se ze so zo ta da chi ji * "
         "tsu zu te de to do na ni nu ne no ha ba pa hi bi pi fu bu pu he be pe ho bo po ma mi mu me mo _ya ya "
         "_yu yu _yo yo ra ri ru re ro wa wa i e o n vu ka ke").split()


def _transliterate(cp, out):
    """Appends a Latin spelling of cp to out, or PLACEHOLDER. May change the previous part (for kana)."""
    if 0x391 <= cp <= 0x3A9:
        out.append(_GREEK[cp - 0x391])
    elif 0x3B1 <= cp <= 0x3C9:
        out.append(_GREEK[cp - 0x3B1].lower())
    elif cp in _GREEK_TONOS:
        out.append(_GREEK_TONOS[cp])
    elif 0x410 <= cp <= 0x44F:
        s = _CYRILLIC[(cp - 0x410) % 32]
        out.append("" if s == "-" else s if cp < 0x430 else s.lower())
    elif 0x400 <= cp <= 0x40F or 0x450 <= cp <= 0x45F:
        s = _CYRILLIC_EXTRA[(cp - 0x400) % 0x50]
        out.append(s if cp < 0x450 else s.lower())
    elif cp in (0x490, 0x491):
        out.append("G" if cp == 0x490 else "g")
    elif 0xAC00 <= cp <= 0xD7A3:
        i = cp - 0xAC00
        parts = (_HANGUL_INITIAL[i // 588], _HANGUL_VOWEL[i // 28 % 21], _HANGUL_FINAL[i % 28])
        out.append("".join(p for p in parts if p != "-"))
    elif 0x3041 <= cp <= 0x3096 or 0x30A1 <= cp <= 0x30F6:
        _kana(_KANA[cp - (0x3041 if cp < 0x30A1 else 0x30A1)], out)
    elif cp == 0x30FC and out and out[-1] and out[-1][-1] in "aeiou":  # ー lengthens the vowel before it
        out.append(out[-1][-1])
    elif cp == 0x30FB:                                       # ・ separates words
        out.append(" ")
    else:
        out.append(PLACEHOLDER)


def _kana(s, out):
    if s == "*":
        out.append(_SMALL_TSU)
        return
    prev = out[-1] if out else ""
    if prev == _SMALL_TSU:  # small tsu doubles the next consonant
        out.pop()
        prev = out[-1] if out else ""
        s = s[0] + s if s[0] not in "aeiou_" else s
    if s[0] == "_":
        if s[1] == "y" and prev[-1:] == "i" and len(prev) > 1:   # き + ゃ = kya, し + ゃ = sha
            out[-1] = prev[:-1] + ("" if prev[-2] in "hj" else "y") + s[2]
            return
        if prev and prev[-1] in "aeiou" and s[1] in "aeiou" and len(prev) > 1:  # フ + ァ = fa
            out[-1] = prev[:-1] + s[1]
            return
        s = s[1:]
    out.append(s)

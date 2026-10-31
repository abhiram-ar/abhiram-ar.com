#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fonttools==4.68.0", "brotli==1.2.1"]
# ///
"""Build the self-hosted web fonts in assets/fonts/ and the @font-face rules that load them.

Run by hand and commit what it changes (assets/fonts/ and the pages in PAGES):

    uv run scripts/build-fonts.py
    # or without uv:
    python3 -m venv /tmp/site-fonts && /tmp/site-fonts/bin/pip install fonttools==4.68.0 brotli==1.2.1
    /tmp/site-fonts/bin/python scripts/build-fonts.py

    uv run scripts/build-fonts.py --check   # exit 1 if a rerun would change anything

Each font is cut to what the site uses and split in two files:
  core  ASCII, Latin-1 and ‘ ’ “ ” – — …, i.e. what shared Google's "latin" file with
        ASCII (preloaded on every page)
  ext   Latin Extended-A and anything else kept, behind unicode-range, so it only
        downloads on pages that contain one of its characters.

Rerun when:
  - page text gains a character outside Latin-1 / Latin Extended-A (e.g. €, ™, •, Greek,
    Cyrillic). The HTML is scanned automatically; text set from JavaScript is not, so put
    such characters in EXTRA_CHARS (the footer clock only ever prints ASCII).
  - CSS starts using something the fonts were cut down for: an Inter weight outside
    400-600, real bold JetBrains Mono, Instrument Serif without letter-spacing (needs liga),
    an Inter italic, or font-feature-settings / font-variant-* turning on a feature that
    isn't in the font's `features`.
  - a new page uses these fonts: add it to PAGES so it gets the @font-face rules and preloads.
  - you want newer font versions: bump GOOGLE_FONTS_COMMIT and the sha256 sums.
Output is deterministic, so a rerun with nothing changed leaves git clean.
"""

import argparse
import hashlib
import html
import io
import re
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from fontTools import subset
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets" / "fonts"
URL_PREFIX = "assets/fonts/"
# Pages that use the web fonts. 404.html deliberately uses system fonts only.
PAGES = ["index.html", "unmin-index.html"]

GOOGLE_FONTS_COMMIT = "6e4b84c976cadb3c49a40fd9a1c203e4f7fcf2da"
SOURCES = {
    "ofl/instrumentserif/InstrumentSerif-Regular.ttf": "498efd461f6ddfcb7a111bf9a565709d2085d48201d501ead960d93e84ffbb88",
    "ofl/instrumentserif/InstrumentSerif-Italic.ttf": "08939b8bdf534afec24ae0ef5e03f948940cd9a8fe08e7fecbad040e62327385",
    "ofl/instrumentserif/OFL.txt": "129ed7618959716959f2941fdd5b49e0ad6e6c1d78726761786a00253d865521",
    "ofl/inter/Inter[opsz,wght].ttf": "29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031",
    "ofl/inter/OFL.txt": "5b9321a4298cfeb6b34354164a1c3afc3db114569984c502b9b35d988fd58c57",
    "ofl/jetbrainsmono/JetBrainsMono[wght].ttf": "48715a42ec242c21e9f02692891e147d022299a52e48d5e413e1a942193ffeda",
    "ofl/jetbrainsmono/OFL.txt": "b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d",
}


def ranges(spec):
    """'U+0020-007E, U+00A0' -> set of code points."""
    out = set()
    for part in spec.replace(" ", "").split(","):
        lo, _, hi = part.removeprefix("U+").partition("-")
        out.update(range(int(lo, 16), int(hi or lo, 16) + 1))
    return out


# What Chrome could draw from each family's Google Fonts files: a file's characters that
# are also inside its unicode-range (Chrome only looks a character up in a face if the
# face's unicode-range lists it). Split into the "latin" file, shared with ASCII, and all
# the others (U+0304/U+0308 were in both). This is the reference for "renders the same as
# before self-hosting": e.g. no file had U+2010, so it fell through to a system font, and
# Ă was drawn from latin-ext although the latin file contained it.
GOOGLE_FONTS_FILES = {
    "Instrument Serif": {
        "latin": ranges(
            "0020-007E,00A0-00A3,00A5,00A7-00AB,00AE-00B0,00B4,00B6-00B8,00BA-00BB,00BF-00FF,0131,"
            "0152-0153,02C6,02DA,02DC,0304,0308,2013-2014,2018-201A,201C-201E,2022,2026,2039-203A,20AC,"
            "2122,2212"),
        "other": ranges(
            "0100-0107,010A-0113,0116-011B,011E-0123,0126-0127,012A-012B,012E-0130,0132-0133,0136-0137,"
            "0139-013E,0141-0148,014A-014D,0150-0151,0154-015B,015E-0161,0164-0165,016A-017E,01CD-01CE,"
            "0218-021B,0237,02C7,02DD,0304,0308,1E80-1E85,1E9E,1EF2-1EF3"),
    },
    "Inter": {
        "latin": ranges(
            "0020-007E,00A0-00AC,00AE-00FF,0131,0152-0153,02BB-02BC,02C6,02DA,02DC,0304,0308,2002,2009,"
            "200B,2013-2014,2018-201A,201C-201E,2022,2026,2032-2033,2039-203A,2044,20AC,2122,2191,2193,"
            "2212,FEFF"),
        "other": ranges(
            "0100-0130,0132-0148,014A-0151,0154-01C3,01C5-0254,0256-027B,027E-0284,0286-0290,0292-02A4,"
            "02A6-02BA,02BD-02C5,02C7-02CC,02CE-02D7,02DD-0301,0303-0304,0308-0309,0323,0374-0376,"
            "037A-037F,0384-038A,038C,038E-03A1,03A3-03D7,03DC-03DD,03F0-03F6,03F9-03FA,03FC-0479,"
            "0480-049D,04A0-04FF,052F,1D00,1D0D,1D1B,1D43,1D47-1D49,1D4D,1D4F-1D50,1D52,1D56-1D58,1D5B,"
            "1D62-1D65,1D9C,1DA0,1DBB,1DBF,1E00-1E9B,1E9D-1F15,1F18-1F1D,1F20-1F45,1F48-1F4D,1F50-1F57,"
            "1F59,1F5B,1F5D,1F5F-1F7D,1F80-1FB4,1FB6-1FC4,1FC6-1FD3,1FD6-1FDB,1FDD-1FEF,1FF2-1FF4,"
            "1FF6-1FFE,2020,20A0-20AB,20AD-20AF,20B1-20B5,20B8-20BA,20BC-20BF,2113,2116,2C7C,2C7F,2DFF,"
            "A69F,A7FF"),
    },
    "JetBrains Mono": {
        "latin": ranges(
            "0020-007E,00A0-00FF,0131,0152-0153,02BC,02C6,02DA,02DC,0304,0308,2013-2014,2018-201A,"
            "201C-201E,2022,2026,2032-2033,2039-203A,2044,20AC,2122,2191,2193,2212,2215,FEFF"),
        "other": ranges(
            "0100-0130,0134-0151,0154-017F,018F,0192,01A0-01A1,01AF-01B0,01CD-01CE,01E6-01E7,01EA-01EB,"
            "01F4-01F5,01FC-01FF,0218-021B,0232-0233,0237,0259,02B9-02BA,02C7,02C9,02DD,02F3,02F7,"
            "0300-0301,0303-0304,0308-0309,0323,0374-0375,037E,0384-038A,038C,038E-03A1,03A3-03CF,03D7,"
            "0401-040C,040E-044F,0451-045C,045E-045F,0490-0491,04AE-04AF,04E8-04E9,1E80-1E85,1E9E,"
            "1EA0-1EF9,2020,20AB,20AE,20BD,20BF,2113,2116"),
    },
}

LATIN_1 = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100))
LATIN_EXT_A = set(range(0x100, 0x180))
PUNCTUATION = {ord(c) for c in "‘’“”–—…"}
# Characters that only appear via JavaScript and so can't be found by scanning the HTML.
EXTRA_CHARS = ""

# OpenType features browsers turn on by default; everything else (tnum, frac, ss01, ...)
# is never enabled by the site's CSS and is dropped.
DEFAULT_FEATURES = ("ccmp", "locl", "mark", "mkmk", "kern", "liga", "clig", "calt", "rlig", "rclt", "rvrn", "curs", "dist", "abvm", "blwm")
# Every Instrument Serif element has non-zero letter-spacing, which makes browsers skip
# liga/clig, so its fi/fl ligatures are never drawn.
SERIF_FEATURES = tuple(f for f in DEFAULT_FEATURES if f not in ("liga", "clig"))

# Same name records Google serves (0-6 and the license URL); the full licenses ship as
# assets/fonts/LICENSES.txt.
NAME_IDS = [0, 1, 2, 3, 4, 5, 6, 14]


@dataclass
class Font:
    slug: str  # output file name prefix
    family: str  # CSS font-family
    style: str
    weights: tuple  # one @font-face per weight, all pointing at the same file, as Google does
    source: str
    axes: dict  # varLib.instancer limits
    features: tuple
    # Instrument Serif carries ttfautohint instructions and Inter/JetBrains Mono an 8-byte
    # `prep` (dropout control). Google serves them to Windows, where DirectWrite uses them;
    # Mac and Linux Chrome ignore them. Setting False for Instrument Serif saves ~6 KB per
    # style per file but may change how it rasterizes on Windows.
    hinting: bool = True
    # Internal name to use if the license declares a Reserved Font Name (none of these do).
    rename: str | None = None


FONTS = [
    Font("instrument-serif", "Instrument Serif", "normal", (400,), "ofl/instrumentserif/InstrumentSerif-Regular.ttf",
         {}, SERIF_FEATURES),
    Font("instrument-serif-italic", "Instrument Serif", "italic", (400,), "ofl/instrumentserif/InstrumentSerif-Italic.ttf",
         {}, SERIF_FEATURES),
    # opsz pinned at 14 as Google does (font-optical-sizing would otherwise vary it with
    # font-size); wght cut to the 400-600 the CSS uses (<strong> asks for 700 and gets 600).
    Font("inter", "Inter", "normal", (400, 500, 600), "ofl/inter/Inter[opsz,wght].ttf",
         {"opsz": 14, "wght": (400, 600)}, DEFAULT_FEATURES),
    # Google served only the 400 instance; the badge's weight-600 "ONLINE" is synthesized
    # bold from it. Keeping a wght range would draw a real 600 instead.
    Font("jetbrains-mono", "JetBrains Mono", "normal", (400,), "ofl/jetbrainsmono/JetBrainsMono[wght].ttf",
         {"wght": 400}, DEFAULT_FEATURES),
]

# Largest outline/advance difference from the source tolerated at the weights in use, in
# font units. Narrowing a variable axis re-rounds its deltas (Inter: about 1.4/2048 em).
MAX_DRIFT = 2.0


def fetch_sources():
    out = {}
    for path, sha in SOURCES.items():
        url = f"https://raw.githubusercontent.com/google/fonts/{GOOGLE_FONTS_COMMIT}/{path.replace('[', '%5B').replace(']', '%5D')}"
        with urllib.request.urlopen(url) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            sys.exit(f"sha256 mismatch for {path}")
        out[path] = data
    return out


def page_text(doc):
    """Text a browser would draw from an HTML document (plus input placeholders/values)."""
    doc = re.sub(r"<!--.*?-->", "", doc, flags=re.S)
    doc = re.sub(r"<(script|style|title|template)\b.*?</\1\s*>", "", doc, flags=re.S | re.I)
    attrs = re.findall(r"""\b(?:placeholder|value)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", doc, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", doc)
    return html.unescape(text + " ".join("".join(a) for a in attrs))


def css_text(doc):
    """Characters CSS draws: content: strings."""
    out = []
    for _, s in re.findall(r"""content\s*:\s*(["'])(.*?)\1""", doc):
        out.append(re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda m: chr(int(m.group(1), 16)), s))
    return "".join(out)


def used_chars():
    text = EXTRA_CHARS
    for p in sorted(ROOT.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix == ".html":
            doc = p.read_text(encoding="utf-8")
            text += page_text(doc)
            text += "".join(css_text(s) for s in re.findall(r"<style\b[^>]*>(.*?)</style>", doc, flags=re.S | re.I))
        elif p.suffix == ".css":
            text += css_text(p.read_text(encoding="utf-8"))
    text += text.upper() + text.lower()  # text-transform
    return {ord(c) for c in text if ord(c) >= 0x20 and not c.isspace()} | {0x20}


def reserved_font_names(license_text, tt):
    """Reserved Font Names declared in the copyright header of the OFL or the font's copyright."""
    header = license_text.split("This Font Software is licensed under", 1)[0]
    header += " " + (tt["name"].getDebugName(0) or "")
    return re.findall(r"""Reserved\s+Font\s+Names?\s*[“"']?([^”"'\n,.]+)""", header)


def rename(tt, family):
    """OFL: a modified font must not use a Reserved Font Name; give it a new internal name."""
    style = tt["name"].getDebugName(2) or "Regular"
    ps = re.sub(r"[^A-Za-z0-9]", "", family)
    for rec in tt["name"].names:
        new = {1: family, 3: f"{ps}-{style}", 4: f"{family} {style}", 6: f"{ps}-{style}", 16: family}.get(rec.nameID)
        if new:
            rec.string = new


def build_file(font, instanced_bytes, unicodes, license_text):
    tt = TTFont(io.BytesIO(instanced_bytes), recalcTimestamp=False)
    opts = subset.Options()
    opts.layout_features = list(font.features)
    opts.hinting = font.hinting
    opts.name_IDs = NAME_IDS
    # Browsers draw the primary font's .notdef (the tofu box) for characters no font has;
    # pyftsubset empties it by default, which would make those characters invisible.
    opts.notdef_outline = True
    opts.drop_tables = opts.drop_tables + ["STAT", "DSIG"]
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=unicodes)
    sub.subset(tt)
    rfn = [n.strip() for n in reserved_font_names(license_text, tt)]
    if rfn:
        if not font.rename or any(n.lower() in font.rename.lower() for n in rfn):
            sys.exit(f"{font.source} declares Reserved Font Name {rfn}; set Font.rename to a name without it")
        rename(tt, font.rename)
        left = [r.toUnicode() for r in tt["name"].names if r.nameID not in (0, 13, 14)
                and any(n.lower() in r.toUnicode().lower() for n in rfn)]
        if left:
            sys.exit(f"{font.slug}: Reserved Font Name still in name table: {left}")
    tt.flavor = "woff2"
    buf = io.BytesIO()
    tt.save(buf)
    return tt.getGlyphOrder(), buf.getvalue()


def instance(font, data):
    tt = TTFont(io.BytesIO(data), recalcTimestamp=False)
    if font.axes:
        tt = instancer.instantiateVariableFont(tt, font.axes)
        tt.recalcTimestamp = False
    if "fvar" in tt:
        tt["fvar"].instances = []  # named instances are unused on the web
    buf = io.BytesIO()
    tt.save(buf)
    return buf.getvalue()


def unicode_range(cps):
    parts, cps = [], sorted(cps)
    i = 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        parts.append(f"U+{cps[i]:04X}" + (f"-{cps[j]:04X}" if j > i else ""))
        i = j + 1
    return ",".join(parts)


def check(font, part, src, names, data, unicodes):
    """Verify a generated file instead of trusting the tools."""
    out = TTFont(io.BytesIO(data))
    problems = []
    cmap = set(out.getBestCmap())
    if cmap != unicodes:
        problems.append(f"cmap differs from plan: missing {sorted(unicodes - cmap)[:10]} extra {sorted(cmap - unicodes)[:10]}")
    feats = {fr.FeatureTag for t in ("GSUB", "GPOS") if t in out and out[t].table.FeatureList
             for fr in out[t].table.FeatureList.FeatureRecord}
    if feats - set(font.features):
        problems.append(f"unexpected features {sorted(feats - set(font.features))}")
    instr = any(getattr(out["glyf"][g], "program", None) and out["glyf"][g].program.getBytecode() for g in out.getGlyphOrder())
    if not font.hinting and (instr or {"fpgm", "prep", "cvt "} & set(out.keys())):
        problems.append("hinting left in")
    for tag, attrs in (("head", ["unitsPerEm"]), ("hhea", ["ascent", "descent", "lineGap"]),
                       ("OS/2", ["sTypoAscender", "sTypoDescender", "sTypoLineGap", "usWinAscent", "usWinDescent", "fsSelection"])):
        for a in attrs:
            if getattr(out[tag], a) != getattr(src[tag], a):
                problems.append(f"{tag}.{a} changed")
    # Outlines and advances against the untouched source, at every weight the CSS uses.
    pinned = {k: v for k, v in font.axes.items() if not isinstance(v, tuple)}
    ranged = [k for k, v in font.axes.items() if isinstance(v, tuple)]
    drift = 0.0
    for w in font.weights:
        loc_out = {"wght": w} if "wght" in ranged else None
        loc_src = {**pinned, "wght": w} if "fvar" in src else None
        gs_out, gs_src = out.getGlyphSet(location=loc_out), src.getGlyphSet(location=loc_src)
        order = out.getGlyphOrder()
        for i, name in enumerate(names):
            a, b = DecomposingRecordingPen(gs_out), DecomposingRecordingPen(gs_src)
            gs_out[order[i]].draw(a)
            gs_src[name].draw(b)
            if [op for op, _ in a.value] != [op for op, _ in b.value]:
                problems.append(f"outline of {name} differs at wght {w}")
                break
            for (_, pa), (_, pb) in zip(a.value, b.value):
                for (xa, ya), (xb, yb) in zip(pa, pb):
                    drift = max(drift, abs(xa - xb), abs(ya - yb))
            drift = max(drift, abs(gs_out[order[i]].width - gs_src[name].width))
    if drift > MAX_DRIFT:
        problems.append(f"outlines drift {drift:.2f} units from source")
    if problems:
        sys.exit(f"{font.slug}-{part}: " + "; ".join(problems))
    return drift


def font_face_rules(font, url, cps):
    return [f"@font-face{{font-family:'{font.family}';font-style:{font.style};font-weight:{w};font-display:block;"
            f"src:url({url}) format('woff2');unicode-range:{unicode_range(cps)}}}" for w in font.weights]


def rewrite_page(doc, preloads, rules):
    """Swap in new preloads and @font-face rules where the previous generated ones were.

    Generated content is recognised by pointing at assets/fonts/, so no markers are needed
    and re-minifying a page doesn't break reruns. On the first run the preloads go right
    before the first <style> and the rules at its start. Pages laid out one tag per line
    get one per line, minified pages stay minified.
    """
    gen_link = re.compile(r"[ \t]*<link\b[^>]*" + re.escape(URL_PREFIX) + r"[^>]*>\n?")
    gen_rule = re.compile(r"[ \t]*@font-face\s*\{[^}]*" + re.escape(URL_PREFIX) + r"[^}]*\}\n?")
    doc = gen_link.sub("", gen_link.sub("\0L", doc, count=1))
    doc = gen_rule.sub("", gen_rule.sub("\0R", doc, count=1))
    style = re.search(r"<style\b[^>]*>", doc)
    if not style:
        sys.exit("page has no <style> to put @font-face rules in")
    line_start = doc.rfind("\n", 0, style.start()) + 1
    indent = doc[line_start:style.start()].replace("\0L", "")
    pretty = indent.strip(" \t") == ""
    if "\0R" not in doc:
        at = style.end() + (1 if pretty and doc[style.end()] == "\n" else 0)
        doc = doc[:at] + "\0R" + doc[at:]
    if "\0L" not in doc:
        at = line_start if pretty else style.start()
        doc = doc[:at] + "\0L" + doc[at:]
    if pretty:
        links = "".join(f'{indent}<link rel="preload" href="{u}" as="font" type="font/woff2" crossorigin>\n' for u in preloads)
        css = "".join(f"{indent}    {r}\n" for r in rules)
    else:
        links = "".join(f"<link as=font crossorigin href={u} rel=preload type=font/woff2>" for u in preloads)
        css = "".join(rules)
    return doc.replace("\0L", links).replace("\0R", css)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a rerun would change anything; write nothing")
    args = ap.parse_args()

    sources = fetch_sources()
    used = used_chars()
    outputs, preloads, rules, report = {}, [], [], []
    licenses = []
    for font in FONTS:
        src = TTFont(io.BytesIO(sources[font.source]))
        license_path = font.source.rsplit("/", 1)[0] + "/OFL.txt"
        license_text = sources[license_path].decode("utf-8")
        if license_text not in licenses:
            licenses.append(license_text)
        # Keep Latin-1, Latin Extended-A, the common punctuation and whatever the pages use,
        # but only what Google's files had: anything else was drawn by a system font before
        # (e.g. ↗ in Inter) and still is.
        gf = GOOGLE_FONTS_FILES[font.family]
        # The combining marks in the latin file (U+0304, U+0308) let Chrome draw Ā ī Ō Ÿ ...
        # as base + mark from the same file as their ASCII neighbours, kerning with them.
        marks = {c for c in gf["latin"] if unicodedata.category(chr(c)) == "Mn"}
        keep = (LATIN_1 | LATIN_EXT_A | PUNCTUATION | marks | used) & (gf["latin"] | gf["other"]) & set(src.getBestCmap())
        system = sorted(c for c in used - keep if c not in LATIN_1 | LATIN_EXT_A)
        inst = instance(font, sources[font.source])
        face_rules = []
        # Kerning and contextual alternates can't act across two font files, so the core
        # holds what shared Google's latin file with ASCII. Putting Latin-1 in the extended
        # file would make the core ~18 KB smaller but stop pairs like "Té" from kerning.
        core = keep & gf["latin"]
        # The marks go in both files, as on Google Fonts, so "Ă" + U+0304 still works.
        ext = (keep & gf["other"]) - (core - marks)
        # Also map code points that point at a glyph the file already has (no new glyphs):
        # FreeType's auto-hinter, which Chrome uses on Linux, picks how to hint a glyph from
        # the code points reaching it, e.g. Inter's ’ hints differently without U+02BC.
        cmap = src.getBestCmap()
        for cps, group in ((core, gf["latin"]), (ext, gf["other"])):
            glyphs = {cmap[c] for c in cps}
            cps |= {c for c in group if c in cmap and cmap[c] in glyphs}
        for part, cps in (("core", core), ("ext", ext)):
            if not cps:
                continue
            names, data = build_file(font, inst, cps, license_text)
            drift = check(font, part, src, names, data, cps)
            name = f"{font.slug}-{part}.{hashlib.sha256(data).hexdigest()[:8]}.woff2"
            outputs[name] = data
            if part == "core":
                preloads.append(URL_PREFIX + name)
            face_rules = font_face_rules(font, URL_PREFIX + name, cps) + face_rules
            report.append(f"  {name:44} {len(data):6} B  {len(cps):3} chars  {len(names):4} glyphs  max drift {drift:.2f} u")
        rules += face_rules
        if system:
            report.append(f"  {font.slug}: left to system fonts as before: " + " ".join(f"{chr(c)} U+{c:04X}" for c in system))
    outputs["LICENSES.txt"] = "\n\n".join(licenses).encode("utf-8")

    pages = {}
    for page in PAGES:
        p = ROOT / page
        old = p.read_text(encoding="utf-8")
        pages[p] = (old, rewrite_page(old, preloads, rules))

    existing = {p.name: p.read_bytes() for p in OUT_DIR.glob("*")} if OUT_DIR.exists() else {}
    stale = sorted(set(existing) - set(outputs))
    changed = sorted(n for n, d in outputs.items() if existing.get(n) != d)
    changed_pages = [p for p, (old, new) in pages.items() if old != new]
    print("\n".join(report))
    if args.check:
        if stale or changed or changed_pages:
            print("out of date:", *changed, *stale, *(p.name for p in changed_pages))
            sys.exit(1)
        print("up to date")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for n in stale:
        (OUT_DIR / n).unlink()
    for n in changed:
        (OUT_DIR / n).write_bytes(outputs[n])
    for p in changed_pages:
        p.write_text(pages[p][1], encoding="utf-8")
    print(f"wrote {len(changed)} files, removed {len(stale)}, updated {len(changed_pages)} pages")


if __name__ == "__main__":
    main()

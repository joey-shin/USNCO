#!/usr/bin/env python3
r"""
Build data/questions.js from the USNCO exam PDFs (format: data/FORMAT.md).

Question segmentation (which lines of which page/column belong to which
question, including split questions and shared subtext) is reused verbatim
from extract_questions.extract_document(), so every entry lines up exactly
with the PNGs that script already wrote to assets/<exam type>/.

Text conversion:
  - span-level font size / superscript flags recover sub- and superscripts
  - Symbol / MathTechnical / Wingdings / etc. glyphs are mapped to Unicode
  - chemical formulas -> \ce{...} (mhchem, short charges: Ag+, SO4^2-)
  - equation lines -> \[ \ce{...} \]; several in a row -> align* on the arrows
  - other sub/superscripted or italic-variable expressions -> $...$
  - stacked fractions (text over a drawn rule) -> \frac{}{}
  - data tables -> \[ \begin{array} \] (no header) or center/tabular with
    \hline and a bold header row; I./II./III. statements -> enumerate
  - paragraphs are separated by blank lines
The conventions follow the hand-corrected entries in question_overrides.json.
Pictures/figures are NOT transcribed; they are only flagged via
question_image / answer_image.

Hand corrections live in data/question_overrides.json ({id: {field: value}})
and are applied last, so re-running this script never loses them.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_questions import FOOTER_MARGIN, HEADER_Y, extract_document  # noqa: E402
from extract_solutions import QNUM_RE as SOLUTION_QNUM_RE  # noqa: E402
from extract_solutions import extract_document as extract_solution_document  # noqa: E402

TOPICS = [
    "Stoichiometry/Solutions",
    "Descriptive/Laboratory",
    "States of Matter",
    "Thermodynamics",
    "Kinetics",
    "Equilibrium",
    "Oxidation - Reduction",
    "Atomic Structure/Periodicity",
    "Bonding/Molecular Structure",
    "Organic/Biochemistry",
]

ELEMENTS = set(
    """H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As
    Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb
    Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk
    Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og""".split()
)
# Element-symbol strings that are far more often ordinary words in exam prose.
NOT_CHEM_WORDS = {"As", "At", "In", "Be", "Am", "IS", "OF", "ON", "BY", "BE", "IN", "I", "A", "No", "SO"}
ROMAN_RE = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)$")

ARROW = "→"
EQARROW = "⇌"
UNKNOWN = "�"  # glyph from a custom-encoded font we cannot decode

# ---------------------------------------------------------------- glyph maps

# Adobe Symbol encoding (only what shows up / is plausible in these exams).
SYMBOL_LETTERS = {
    "A": "Α", "B": "Β", "C": "Χ", "D": "Δ", "E": "Ε", "F": "Φ", "G": "Γ", "H": "Η", "I": "Ι",
    "K": "Κ", "L": "Λ", "M": "Μ", "N": "Ν", "O": "Ο", "P": "Π", "Q": "Θ", "R": "Ρ", "S": "Σ",
    "T": "Τ", "U": "Υ", "W": "Ω", "X": "Ξ", "Y": "Ψ", "Z": "Ζ",
    "a": "α", "b": "β", "c": "χ", "d": "δ", "e": "ε", "f": "φ", "g": "γ", "h": "η", "i": "ι",
    "k": "κ", "l": "λ", "m": "μ", "n": "ν", "o": "ο", "p": "π", "q": "θ", "r": "ρ", "s": "σ",
    "t": "τ", "u": "υ", "w": "ω", "x": "ξ", "y": "ψ", "z": "ζ",
}
SYMBOL_HIGH = {
    "\xae": ARROW, "\xac": "←", "\xad": "↑", "\xaf": "↓", "\xab": "↔", "\xdb": EQARROW, "\xde": "⇒",
    "\xb0": "°", "\xb1": "±", "\xb4": "×", "\xb3": "≥", "\xa3": "≤", "\xb9": "≠", "\xba": "≡",
    "\xbb": "≈", "\xb7": "•", "\xa2": "′", "\xb2": "″", "\xd7": "⋅", "\xa5": "∞", "\xd6": "√",
    # big-parenthesis pieces from equation editors
    "\xe6": "(", "\xe7": "", "\xe8": "", "\xf6": ")", "\xf7": "", "\xf8": "",
    "\xe9": "[", "\xea": "", "\xeb": "", "\xf9": "]", "\xfa": "", "\xfb": "",
}
SYMBOL_ASCII = {"-": "−", "*": "*", "+": "+", "=": "=", "(": "(", ")": ")", "[": "[", "]": "]",
                "<": "<", ">": ">", " ": " ", ",": ",", ".": ".", "/": "/", "|": "|", "_": "_",
                "0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7",
                "8": "8", "9": "9", ":": ":", ";": ";"}
BIG_PAREN = {"": "(", "": "", "": "", "": ")", "": "", "": "",
             "⎛": "(", "⎜": "", "⎝": "", "⎞": ")", "⎟": "", "⎠": "", "": "", "": "[",
             "": "", "": "", "": "]", "": "", "": ""}

FONT_MAPS = [
    (re.compile(r"MathTechnical"), {"r": ARROW, "j": ARROW, "s": EQARROW}),
    (re.compile(r"CourierNew"), {"r": ARROW, "s": EQARROW}),
    (re.compile(r"Chembook"), {"\xe6": EQARROW, "\xbe": EQARROW}),
    (re.compile(r"Untitled"), {"=": EQARROW}),
    (re.compile(r"Wingdings ?3"), {"": EQARROW, "D": EQARROW}),
    (re.compile(r"Wingdings"), {"\xc6": ARROW, "\xa7": "•", "": "•", "▪": "•", "\x83": "•",
                                "!": "•", "\x01": "•"}),
    (re.compile(r"MS.?Mincho"), {"⇄": EQARROW}),
    (re.compile(r"ZapfDingbats"), {"❘": "|"}),
    (re.compile(r"MT-?Extra"), {"o": "°"}),
    (re.compile(r"Webdings"), {"\xb4": "×"}),
    (re.compile(r"Mistral|BrushScript|MonotypeCorsiva"), {"l": "ℓ"}),
]

GENERIC_MAP = {"": ARROW, "⇄": EQARROW, "⇆": EQARROW, "ﬁ": "fi", "ﬂ": "fl",
               "ﬀ": "ff", "ﬃ": "ffi", "‒": "–", " ": " ", "\t": " ",
               "½": "1/2", "¼": "1/4", "¾": "3/4", "˚": "°", "º": "°"}


def map_char(c, font):
    if c in BIG_PAREN:
        return BIG_PAREN[c]
    if "Symbol" in font:
        if 0xF000 <= ord(c) <= 0xF0FF:  # raw Symbol code point in the private-use area
            r = chr(ord(c) - 0xF000)
            return SYMBOL_LETTERS.get(r) or SYMBOL_HIGH.get(r) or SYMBOL_ASCII.get(r, UNKNOWN)
        if c in SYMBOL_LETTERS:
            return SYMBOL_LETTERS[c]
        if c in SYMBOL_HIGH and c not in "\xd7\xb0\xb1\xb7":  # these four already decode correctly
            return SYMBOL_HIGH[c]
        if c in SYMBOL_ASCII or ord(c) > 0x7F:
            return SYMBOL_ASCII.get(c, c)
        return UNKNOWN  # e.g. 2007-2009 local MathType subsets: '"' is Δ in one, − in another
    for pat, m in FONT_MAPS:
        if pat.search(font):
            if c in m:
                return m[c]
            break
    if 0x1D400 <= ord(c) <= 0x1D7FF:  # math alphanumerics (Cambria Math)
        name = unicodedata.name(c, "")
        m = re.search(r"(CAPITAL|SMALL) ([A-Z])$", name)
        if m:
            return m.group(2) if m.group(1) == "CAPITAL" else m.group(2).lower()
    return GENERIC_MAP.get(c, c)


# ------------------------------------------------------------- page geometry

class Ch:
    """One character with its script position ('b', 'sub', 'sup') and style.
    s == 'tex' marks a pre-rendered math-mode LaTeX fragment (e.g. \\frac)."""
    __slots__ = ("c", "s", "it", "bd")

    def __init__(self, c, s="b", it=False, bd=False):
        self.c, self.s, self.it, self.bd = c, s, it, bd

    def __repr__(self):
        return self.c if self.s == "b" else f"{'_' if self.s == 'sub' else '^'}{self.c}"


SPACE = Ch(" ")


def page_lines(page):
    """Content lines with per-character x positions, after merging detached
    sub/superscript lines into the line they belong to."""
    page_h = page.rect.height
    raw = []
    for bi, b in enumerate(page.get_text("rawdict")["blocks"]):
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            spans = []
            for s in l["spans"]:
                s["text"] = "".join(ch["c"] for ch in s["chars"])
                if s["text"]:
                    spans.append(s)
            if not "".join(s["text"] for s in spans).strip():
                continue
            x0, y0, x1, y1 = l["bbox"]
            if y0 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
                continue
            raw.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "spans": spans, "block": bi})
    if not raw:
        return []
    for l in raw:
        sizes = Counter()
        for s in l["spans"]:
            sizes[round(s["size"], 1)] += len(s["text"].strip()) or 0.1
        l["size"] = sizes.most_common(1)[0][0]
    # merge script-only lines into an adjacent base line
    page_size = Counter(l["size"] for l in raw).most_common(1)[0][0]
    base = [l for l in raw if l["size"] >= 0.8 * page_size]
    small = [l for l in raw if l["size"] < 0.8 * page_size]
    for sl in small:
        best = None
        for bl in base:
            if sl["y0"] < bl["y1"] + 3 and sl["y1"] > bl["y0"] - 3 and bl["x0"] - 3 <= sl["x0"] <= bl["x1"] + 8:
                d = abs((sl["y0"] + sl["y1"]) / 2 - (bl["y0"] + bl["y1"]) / 2)
                if best is None or d < best[0]:
                    best = (d, bl)
        if best:
            bl = best[1]
            bl["spans"] = sorted(bl["spans"] + sl["spans"], key=lambda s: s["bbox"][0])
            bl["x1"] = max(bl["x1"], sl["x1"])
        else:
            sl["size"] = page_size  # free-standing small text: treat as ordinary text
            base.append(sl)
    for l in base:
        l["xs"] = []
        l["chars"] = spans_to_chars(l["spans"], l["size"], l["xs"])
        l["text"] = ctext(l["chars"])
    return base


def spans_to_chars(spans, base_size, xs):
    """Spans -> [Ch]; xs receives each Ch's (x0, x1) on the page."""
    base_origin = [s["origin"][1] for s in spans if abs(s["size"] - base_size) < 0.6]
    by = sorted(base_origin)[len(base_origin) // 2] if base_origin else None
    out = []
    for s in spans:
        font = re.sub(r"^[A-Z]{6}\+", "", s["font"])
        small = s["size"] < 0.85 * base_size
        if small and s["text"].strip():
            if s["flags"] & 1 or (by is not None and s["origin"][1] < by - 0.8):
                kind = "sup"
            else:
                kind = "sub"
        else:
            kind = "b"
        it = bool(s["flags"] & 2) or "Italic" in font or "Oblique" in font
        bd = bool(s["flags"] & 16) or "Bold" in font
        raised_dot = not small and s["flags"] & 1 and s["text"].strip() == "."
        for rc in s["chars"]:
            c = "⋅" if raised_dot and rc["c"] == "." else rc["c"]
            for mc in map_char(c, font):
                out.append(Ch(mc, "b" if mc == " " else kind, it, bd))
                xs.append((rc["bbox"][0], rc["bbox"][2]))
    return out


def col_of(x0, x1, page_w):
    return 0 if (x0 + x1) / 2 < page_w / 2 else 1


def lines_in(lines, page_w, col, top, bottom):
    return sorted(
        (l for l in lines if (col is None or col_of(l["x0"], l["x1"], page_w) == col)
         and top <= (l["y0"] + l["y1"]) / 2 < bottom),
        key=lambda l: (l["y0"], l["x0"]),
    )


def visuals_in(page, col, top, bottom):
    pw = page.rect.width
    out = []
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if x1 - x0 > 0.8 * pw or (x1 - x0) * (y1 - y0) < 20:
            continue
        if (col is None or col_of(x0, x1, pw) == col) and top <= y0 < bottom:
            out.append(("img", x0, y0, x1, y1))
    for dr in DRAW_CACHE[page.number]:
        x0, y0, x1, y1 = dr
        if x1 - x0 > 0.8 * pw:
            continue
        if (col is None or col_of(x0, x1, pw) == col) and top <= y0 < bottom:
            out.append(("draw", x0, y0, x1, y1))
    return out


# ------------------------------------------------------------ chem detection

PHASE_RE = re.compile(r"^\((s|l|g|aq|c|cr|soln|sol|graphite|diamond|rhombic|monoclinic|white|red|gray|grey)(,[^()]*)?\)$")
NUM_RE = re.compile(r"^[–−-]?(\d[\d,]*(\.\d*)?|\.\d+)$")


def ctext(chars):
    return "".join(c.c for c in chars)


def strip_punct(chars):
    """Split a word into (prefix, core, suffix) peeling off sentence punctuation
    and unbalanced brackets."""
    pre, suf = [], []
    core = list(chars)
    changed = True
    while core and changed:
        changed = False
        if (core[-1].s == "b" or (len(core) > 1 and core[-2].c == ")")) and core[-1].c in ".,;:?!\"”’'":
            suf.insert(0, core.pop())
            changed = True
            continue
        if core and core[0].s == "b" and core[0].c in "\"“‘'":
            pre.append(core.pop(0))
            changed = True
            continue
        txt = ctext(core)
        for o, cl in (("(", ")"), ("[", "]")):
            if core and core[0].c == o and txt.count(o) > txt.count(cl):
                pre.append(core.pop(0))
                changed = True
                break
            if core and core[-1].c == cl and txt.count(cl) > txt.count(o):
                suf.insert(0, core.pop())
                changed = True
                break
    return pre, core, suf


def parse_chem(chars):
    """Return (mhchem_string, info) if chars spell a chemical species, else None."""
    n = len(chars)
    if n == 0 or any(c.s == "tex" for c in chars):
        return None
    txt = ctext(chars)
    m = ELECTRON_RE.fullmatch(txt)
    if m and all(c.s == "b" for c in chars[: len(m.group(1)) + 1]):
        # electron, optionally with a coefficient and phase: "e–", "2e–", "e–(g)"
        coef = m.group(1) + " " if m.group(1) else ""
        return coef + "e-" + (m.group(2) or ""), {"elements": 1, "script": True, "phase": bool(m.group(2))}
    i = 0
    out = []
    elements = 0
    script = False
    phase = False
    if chars[0].s in ("sup", "sub"):
        # isotope / mass-number prefix ^{14}_{6}C
        pre_sup, pre_sub = "", ""
        while i < n and chars[i].s in ("sup", "sub"):
            if chars[i].s == "sup":
                pre_sup += chars[i].c
            else:
                pre_sub += chars[i].c
            i += 1
        if not re.fullmatch(r"\d+", pre_sup + pre_sub):
            return None
        if pre_sup and pre_sub and int(pre_sup) < int(pre_sub) and i < n and chars[i].c.isupper():
            # a stacked fraction coefficient (½ O2), not an isotope label
            out.append("%s/%s " % (pre_sup, pre_sub))
        elif pre_sup:
            out.append("^{%s}" % pre_sup)
        if pre_sub and not (out and "/" in out[-1]):
            out.append("_{%s}" % pre_sub)
        script = True
    else:
        # coefficient: digits (or fraction) directly before the formula
        m = re.match(r"^(\d+(/\d+)?)(?=[A-Z(\[])", txt)
        if m and all(c.s == "b" for c in chars[: len(m.group(1))]):
            out.append(m.group(1) + " ")
            i = len(m.group(1))
    depth = 0
    last = None  # 'el', 'close', 'num', 'charge', 'phase', 'dot', 'bond', 'open'
    while i < n:
        c = chars[i]
        ch = c.c
        if c.s == "b" and ch.isupper() and ch.isascii():
            two = ch + chars[i + 1].c if i + 1 < n and chars[i + 1].s == "b" and chars[i + 1].c.islower() else None
            if two and two in ELEMENTS:
                out.append(two)
                i += 2
            elif ch in ELEMENTS:
                out.append(ch)
                i += 1
            else:
                return None
            elements += 1
            last = "el"
            continue
        if c.s == "b" and ch in "([":
            j = i
            depth_p = 0
            while j < n:
                if chars[j].c in "([":
                    depth_p += 1
                elif chars[j].c in ")]":
                    depth_p -= 1
                    if depth_p == 0:
                        break
                j += 1
            grp = ctext(chars[i : j + 1])
            if PHASE_RE.match(grp) and last in ("el", "close", "num", "charge") and j >= n - 3:
                out.append(grp.replace(" ", ""))
                phase = True
                i = j + 1
                last = "phase"
                continue
            out.append(ch)
            depth += 1
            i += 1
            last = "open"
            continue
        if c.s == "b" and ch in ")]":
            if depth == 0:
                return None
            depth -= 1
            out.append(ch)
            i += 1
            last = "close"
            continue
        if c.s in ("sub", "sup") and ch == "(" and last in ("el", "close", "num", "charge"):
            j = i
            while j < n and chars[j].s == c.s and chars[j].c != ")":
                j += 1
            grp = ctext(chars[i : j + 1])
            if j < n and PHASE_RE.match(grp.replace(" ", "")):
                out.append(grp.replace(" ", ""))
                phase = True
                i = j + 1
                last = "phase"
                continue
            return None
        if ch.isdigit() and last in ("el", "close", "num") and c.s in ("b", "sub"):
            out.append(ch)
            script = script or c.s == "sub"
            i += 1
            last = "num"
            continue
        if c.s == "sub" and ch in "xyn" and last in ("el", "close"):
            out.append("_{%s}" % ch)
            script = True
            i += 1
            last = "num"
            continue
        if c.s == "sup" and last in ("el", "close", "num", "phase"):
            j = i
            charge = ""
            while j < n and chars[j].s == "sup":
                charge += chars[j].c
                j += 1
            charge = charge.replace("–", "-").replace("−", "-").replace("•", "*").replace(" ", "")
            if re.fullmatch(r"\d+", charge) and last != "phase":
                # a superscripted count with no sign is a mis-set subscript
                out.append(charge)
            elif re.fullmatch(r"\d*[+-]|\d*[+-]?\*|\+\d*|-|\*|[IVX]+|[+-]{2,3}", charge):  # "-1" is a unit exponent
                out.append("^{%s}" % charge)
            else:
                return None
            script = True
            i = j
            last = "charge"
            continue
        if c.s == "b" and ch in "–−-+" and last in ("el", "close", "num"):
            rest = ctext(chars[i:]).replace("–", "-").replace("−", "-")
            m = re.fullmatch(r"([+-]{1,3})(\(.*\))?", rest)
            if m and elements >= 1 and (not m.group(2) or PHASE_RE.match(m.group(2))):
                out.append("^{%s}" % m.group(1))  # base-line charge sign, e.g. "Cl–", "HS–(aq)"
                if m.group(2):
                    out.append(m.group(2))
                    phase = True
                script = True
                i = n
                last = "charge"
                continue
        if c.s == "b" and ch in "–−-=≡" and last in ("el", "close", "num") and i + 1 < n and (
            chars[i + 1].c.isupper() or chars[i + 1].c in "(["
        ):
            out.append({"=": "=", "≡": "#"}.get(ch, "-"))
            i += 1
            last = "bond"
            continue
        if c.s == "b" and ch in "·⋅•∙*" and last in ("el", "close", "num", "phase") and i + 1 < n:
            out.append("*")
            i += 1
            m = re.match(r"\d+(\.\d+)?", ctext(chars[i:]))
            if m and all(x.s == "b" for x in chars[i : i + len(m.group(0))]):
                out.append(m.group(0))
                i += len(m.group(0))
            last = "dot"
            continue
        return None
    if depth != 0 or elements == 0 or last in ("bond", "dot", "open"):
        return None
    return "".join(out), {"elements": elements, "script": script, "phase": phase}


ELECTRON_RE = re.compile(r"(\d*)e[–\-−](\(g\))?")


def is_chem_word(core_chars):
    txt = ctext(core_chars)
    if ELECTRON_RE.fullmatch(txt):
        r = parse_chem(core_chars)  # the e of e⁻ is often set in italic
        if r:
            return r[0]
    if not txt or ROMAN_RE.match(txt) or txt in NOT_CHEM_WORDS or re.fullmatch(r"\([A-D]\)", txt):
        return None
    phase_at = txt.rfind("(")
    body = core_chars[:phase_at] if phase_at > 0 and PHASE_RE.match(txt[phase_at:]) else core_chars
    if any(c.it for c in body if c.c.isalpha() and c.s == "b"):
        return None  # italic letters are variables (K, P, V ...), not element symbols
    r = parse_chem(core_chars)
    if not r:
        return None
    ce, info = r
    letters = re.sub(r"[^A-Za-z]", "", txt)
    if info["script"] or info["phase"]:
        return ce
    if re.search(r"\d", txt):
        return ce
    if info["elements"] >= 2:
        if txt.isupper() and len(txt) >= 3 and txt in ENGLISH_CAPS:
            return None
        return ce
    if len(letters) == 2 and letters in ELEMENTS:  # bare two-letter symbol, e.g. Na, Cl
        return ce
    if "(" in txt or "-" in ce or "=" in ce or "#" in ce:
        return ce
    return None


# capitalised emphasis words that happen to spell valid element sequences
ENGLISH_CAPS = {"NOW", "SON", "CON", "NOR", "BOTH", "HIS", "CAN"}

# ------------------------------------------------------------------- tokens

GREEK_TEX = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\varepsilon",
    "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau",
    "φ": r"\phi", "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "∆": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega", "ℓ": r"\ell", "µ": r"\mu", "Α": "A", "Β": "B", "Ε": "E",
    "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Χ": "X", "Υ": r"\Upsilon", "Ξ": r"\Xi", "ο": "o", "ι": r"\iota",
}
MATH_SYM = {
    "×": r"\times", "⋅": r"\cdot", "·": r"\cdot", "∙": r"\cdot", "°": r"^\circ",
    "–": "-", "−": "-", "≥": r"\geq", "≤": r"\leq", "≈": r"\approx", "≠": r"\neq", "±": r"\pm",
    ARROW: r"\rightarrow", EQARROW: r"\rightleftharpoons", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "↑": r"\uparrow", "↓": r"\downarrow", "√": r"\surd ", "∞": r"\infty", "%": r"\%", "#": r"\#",
    "&": r"\&", "_": r"\_", "$": r"\$", "{": r"\{", "}": r"\}", "′": "'", "≡": r"\equiv",
    "|": r"\mid ", "‡": r"\ddagger", "⇒": r"\Rightarrow", "•": r"\cdot", "∝": r"\propto",
    "‘": "`", "’": "'", "“": "``", "”": "''", "—": "-", "…": r"\ldots",
    UNKNOWN: r"\,?\,",
}
MATH_TRIGGER = set(GREEK_TEX) | {"×", "°", "≥", "≤", "≈", "≠", "±", "‡", "∝", "√"}
OPERATORS = {"=", "×", "+", "–", "−", "-", "<", ">", "≥", "≤", "≈", "≠", "/", "±", ARROW, EQARROW,
             "⋅", "·", "∝", "<<", ">>"}
ARROW_CE = {ARROW: "->", EQARROW: "<=>", "←": "<-", "↔": "<->"}


PHASE_WORD_RE = re.compile(r"\((s|l|g|aq)\)[.,;:?]?")


GENERIC_SPECIES_RE = re.compile(r"(\d*)([A-Z](?:[A-Z]|\d(?=[A-Z]|$)){0,3})([,;.?]?)")


UNIT_SYMBOLS = {"V", "K", "C", "N", "W", "F", "S", "Pa"}


def segments(s):
    """Split rendered text into [(kind, str)]: 'text', 'math' (the inside of
    $...$) and 'ce' (a whole \\ce{...} group, braces balanced)."""
    out, buf, i, n = [], [], 0, len(s)

    def flush():
        if buf:
            out.append(("text", "".join(buf)))
            buf.clear()

    while i < n:
        if s[i] == "\\" and i + 1 < n and s[i + 1] in "$%&#_":
            buf.append(s[i : i + 2])
            i += 2
            continue
        if s[i] == "$":
            j = i + 1
            while j < n and not (s[j] == "$" and s[j - 1] != "\\"):
                j += 1
            flush()
            out.append(("math", s[i + 1 : j]))
            i = j + 1
            continue
        if s.startswith("\\ce{", i):
            j, depth = i + 4, 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            flush()
            out.append(("ce", s[i:j]))
            i = j
            continue
        buf.append(s[i])
        i += 1
    flush()
    return out


def join_segments(segs):
    return "".join("$%s$" % t if k == "math" else t for k, t in segs)


def ce_compact(ce):
    """mhchem's short charge form, as in the hand-corrected entries:
    Ag^{+} -> Ag+, SO4^{2-} -> SO4^2-."""
    return re.sub(r"\^\{(\d*)([+-])\}(?![A-Za-z0-9])",
                  lambda m: ("^" + m.group(1) if m.group(1) else "") + m.group(2), ce)


def bracket_ce(m):
    """[\\mathrm{H}_{2}] -> [\\ce{H2}]: a concentration in a rate law or K expression."""
    plain = re.sub(r"\\mathrm\{([A-Za-z]+)\}", r"\1", m.group(1))
    plain = re.sub(r"_\{(\d+)\}", r"\1", plain)
    plain = re.sub(r"\^\{(\d*[+-])\}", r"^\1", plain)
    syms = re.findall(r"[A-Z][a-z]?", plain)
    if re.fullmatch(r"[A-Z][A-Za-z0-9()^+-]*", plain) and (all(x in ELEMENTS for x in syms) or re.fullmatch(r"[A-Z]", plain)):
        return r"[\ce{%s}]" % plain
    return m.group(0)


def tidy_math(m):
    m = "".join(ce_compact(t) if k == "ce" else t for k, t in segments(m))
    m = re.sub(r"\[((?:\\mathrm\{[A-Za-z]+\}|_\{\d+\}|\^\{\d*[+-]\}|[()])+)\]", bracket_ce, m)
    m = re.sub(r"\\mathrm\{k\}(?=[\[_ ]|$)", "k", m)  # the rate constant
    m = re.sub(r"\\Delta \\mathrm\{([A-Za-z])\}", r"\\Delta \1", m)  # ΔH, ΔS, ΔT are variables
    m = re.sub(r"\\Delta \\mathrm\{([GHSE])(rxn|f|vap|fus|sub|soln|comb|r)\}", r"\\Delta \1_{\\mathrm{\2}}", m)
    m = re.sub(r"\\mathrm\{([A-Z])\}(?=\^\\circ|\^\{\\circ)", r"\1", m)  # E°, H°, S°
    m = re.sub(r"(\d) ?\^\\circ", r"\1\\,^\\circ", m)  # 25\,^\circ C
    return m


def finalize(s):
    s = "".join(ce_compact(t) if k == "ce" else "$%s$" % tidy_math(t) if k == "math" else t
                for k, t in segments(s))
    # neighbouring math groups are one expression: "$n = 3$ $\rightarrow$ $n = 4$"
    return re.sub(r"(?<!\\)\$ *([=<>]?) *\$", lambda m: " %s " % m.group(1) if m.group(1) else " ", s)


UNIT_RE = re.compile(r"(?<![\w.])([–−-]?\d[\d,]*(?:\.\d+)?) (K|V|mV|atm|torr|mmHg|Pa|kPa|bar|g|mg|kg|mL|L|mol|mmol|M|mM|"
                     r"m|kJ|J|cal|kcal|s|min|h|nm|pm|cm|mm|Hz|amu)(?![\w])")


def nbsp_units(s):
    """'298 K' -> '298~K' in the prose (text-mode) parts of a rendering."""
    return join_segments([(k, UNIT_RE.sub(r"\1~\2", t) if k == "text" else t) for k, t in segments(s)])


MATH_OP_RE = re.compile(r"(?:(?<=\s)|^)([=+<>×])(?=\s|$)|^([+–−-]?\d[\d,]*(?:\.\d*)?)(?=\s|$)")


def text_to_math(t):
    """A text-mode run inside math mode: numbers and operators stay math,
    words go in \\text{}: '42.0 g' -> 42.0\\text{ g}, 'MM = ' -> \\text{MM} = ."""
    t = re.sub(r"\\([$%&#_])", r"\1", t)

    def txt(p):
        return r"\text{%s}" % re.sub(r"([%&#_{}$])", r"\\\1", p) if p else ""

    out, pos = [], 0
    for m in MATH_OP_RE.finditer(t):
        piece = t[pos : m.start()]
        if m.group(1):
            lone = re.fullmatch(r"\s*[A-Za-z]\s*", piece) and not (out and re.match(r"[+–−-]?\d", out[-1]))
            out.append(piece.strip() if lone else txt(piece.rstrip()))  # "K = ..." is a variable
            out.append(" %s " % MATH_SYM.get(m.group(1), m.group(1)))
            pos = m.end()
            while pos < len(t) and t[pos] == " ":
                pos += 1
        else:
            out.append(txt(piece))
            out.append(m.group(2).replace("–", "-").replace("−", "-"))
            pos = m.end()
    out.append(txt(t[pos:]))
    return "".join(out)


def split_words(chars):
    """Split on base-line spaces; arrows and tex fragments are their own word.
    A phase label with a comment, e.g. 'C(s, graphite)', stays one word."""
    words, cur = [], []
    for c in chars:
        if c.c == " " and c.s == "b":
            if cur:
                words.append(cur)
                cur = []
            continue
        if (c.c in ARROW_CE and c.s == "b") or c.s == "tex":
            if cur:
                words.append(cur)
                cur = []
            words.append([c])
            continue
        cur.append(c)
    if cur:
        words.append(cur)
    split = []
    for w in words:
        # a balancing blank set flush against its formula: "__Mn2+"
        m = re.match(r"_{2,}", ctext(w))
        if m and m.end() < len(w) and all(c.s == "b" for c in w[: m.end()]):
            split.extend([w[: m.end()], w[m.end() :]])
        else:
            split.append(w)
    words = split
    glued = []
    for w in words:
        if glued and all(c.s in ("sub", "sup") for c in w) and glued[-1][-1].s != "tex" and glued[-1][-1].c not in ARROW_CE:
            glued[-1] = glued[-1] + w  # a detached sub/superscript belongs to the word before it
        else:
            glued.append(w)
    words = glued
    out = []
    k = 0
    while k < len(words):
        w = words[k]
        if re.search(r"\((s|l|g|aq|c|cr),$", ctext(w)) and k + 1 < len(words) and re.match(r"^[A-Za-z0-9. ]*\)", ctext(words[k + 1])):
            out.append(w + [Ch(" ")] + words[k + 1])
            k += 2
            continue
        detached = PHASE_WORD_RE.fullmatch(ctext(w)) or (w[0].s == "sup" and w[0].c in "+-–−")
        if out and detached and out[-1][-1].c not in ARROW_CE and out[-1][-1].s != "tex":
            # a phase label or charge set off by a space: "H+ (aq)", "I –(aq)"
            cand = out[-1] + w
            if is_chem_word(strip_punct(cand)[1]):
                out[-1] = cand
                k += 1
                continue
        out.append(w)
        k += 1
    return out


def math_escape(s):
    out = []
    for ch in s:
        if ch in GREEK_TEX:
            g = GREEK_TEX[ch]
            out.append(g + (" " if g.startswith("\\") else ""))
        elif ch in MATH_SYM:
            out.append(MATH_SYM[ch])
        else:
            out.append(ch)
    return "".join(out)


def math_group(grp):
    """Letters: italic stay math-italic, upright runs become \\mathrm{}."""
    out = []
    i = 0
    while i < len(grp):
        c = grp[i]
        if c.s == "tex":
            out.append(c.c)
            i += 1
            continue
        if c.c.isascii() and c.c.isalpha() and not c.it:
            j = i
            while j < len(grp) and grp[j].c.isascii() and grp[j].c.isalpha() and not grp[j].it:
                j += 1
            out.append(r"\mathrm{%s}" % ctext(grp[i:j]))
            i = j
            continue
        out.append(math_escape(c.c))
        i += 1
    return "".join(out).strip()


def math_word(chars):
    """Render a non-chemical word that needs math mode."""
    parts = []
    i, n = 0, len(chars)
    while i < n:
        s = "b" if chars[i].s == "tex" else chars[i].s
        j = i
        while j < n and ("b" if chars[j].s == "tex" else chars[j].s) == s:
            j += 1
        body = math_group(chars[i:j])
        if s == "b":
            parts.append(body)
        elif s == "sub":
            parts.append("_{%s}" % body)
        else:
            parts.append("^{%s}" % body)
        i = j
    out = "".join(parts)
    out = re.sub(r"(?<![A-Za-z0-9}])\^\{(\d+)\}_\{(\d+)\}", r"\\tfrac{\1}{\2}", out)  # stacked ½
    out = re.sub(r"\^\{(?:\\mathrm\{[oO]\}|[oO])\}", r"^\\circ", out)
    out = out.replace("^{^\\circ}", "^\\circ")
    out = re.sub(r"\^\\circ\^\{([^{}]*)\}", r"^{\\circ \1}", out)
    out = re.sub(r"\^\{([^{}]*)\}\^\\circ", r"^{\1\\circ}", out)
    return out.strip()


def frac_coefficient(core):
    """'^{5}/_{2}' typeset fraction -> '5/2', else None."""
    t = ctext(core)
    if "/" in t:
        k = t.index("/")
        a, b = core[:k], core[k + 1 :]
        if a and b and all(c.s == "sup" and c.c.isdigit() for c in a) and all(
            c.s == "sub" and c.c.isdigit() for c in b
        ):
            return ctext(a) + "/" + ctext(b)
    return None


def classify(word):
    """-> (kind, payload). kinds: chem, arrow, op, num, blank, math, tex, text"""
    txt = ctext(word)
    if len(word) == 1 and word[0].s == "tex":
        return "tex", word[0].c
    if len(word) == 1 and word[0].c in ARROW_CE and word[0].s == "b":
        return "arrow", ARROW_CE[word[0].c]
    if txt in OPERATORS and all(c.s == "b" for c in word):
        return "op", txt
    if re.fullmatch(r"_{2,}", txt):
        return "blank", txt
    pre, core, suf = strip_punct(word)
    if len(core) >= 3 and core[0].c == "l" and core[1].c == "0" and core[2].s == "sup":
        core = [Ch("1", "b", core[0].it, core[0].bd)] + core[1:]  # source typo: "l0" for "10"
    fc = frac_coefficient(core)
    if fc:
        return "num", (pre, [Ch(x) for x in fc], suf)
    if all(c.s == "b" for c in core) and NUM_RE.match(ctext(core)):
        return "num", (pre, core, suf)
    if all(c.s == "b" for c in core) and re.fullmatch(r"\d+/\d+", ctext(core)):
        return "num", (pre, core, suf)
    ce = is_chem_word(core)
    if ce is None and re.search(r"[/|]", ctext(core)):
        # e.g. "Cu/Cu2+" or "Zn|Zn2+": every piece must be chemical
        pieces, seps, cur = [], [], []
        for c in core:
            if c.c in "/|" and c.s == "b":
                pieces.append(cur)
                seps.append(c.c)
                cur = []
            else:
                cur.append(c)
        pieces.append(cur)
        ces = [is_chem_word(p) for p in pieces]
        if all(ces) and len(ces) > 1:
            joined = ces[0]
            for sep, p in zip(seps, ces[1:]):
                joined += " " + sep + " " + p
            return "chem", (pre, joined, suf)
    if ce is not None:
        return "chem", (pre, ce, suf)
    has_script = any(c.s != "b" for c in core)
    has_trig = any(c.c in MATH_TRIGGER or c.c == UNKNOWN for c in core)
    letters = [c for c in core if c.s == "b" and c.c.isalpha()]
    single_italic = len(letters) == 1 and letters[0].it and core[0] is letters[0]
    if has_script or has_trig or (single_italic and len(core) <= 3):
        return "math", (pre, core, suf)
    return "text", txt


def text_escape(s):
    return re.sub(r"([$%&#_])", r"\\\1", s)


def wrap_pre_suf(pre, body, suf):
    return text_escape(ctext(pre)) + body + text_escape(ctext(suf))


SENT_PUNCT = set(".?!;:,")


def normalize_arrows(chars):
    """Long arrows typeset from pieces: '−−→' -> '→', '⇀↽' / '−−⇀↽−−' -> '⇌'.
    A shaft is two or more base-line dashes (a single one may be a charge)."""
    chars = list(chars)
    for k in range(len(chars) - 1):
        if chars[k].c == "⇀" and chars[k + 1].c == "↽":
            chars[k], chars[k + 1] = Ch(EQARROW), Ch(" ")
    out = []
    k = 0
    while k < len(chars):
        c = chars[k]
        if c.s == "b" and c.c in ARROW_CE:
            j = len(out)
            while j and out[j - 1].c == " ":
                j -= 1
            d = j
            while d and out[d - 1].c in "−–-" and out[d - 1].s == "b":
                d -= 1
            if j - d >= 2:
                del out[d:]
            out.extend([SPACE, c, SPACE])
            k += 1
            e = k
            while e < len(chars) and chars[e].c in "−–-" and chars[e].s == "b":
                e += 1
            if e - k >= 2:
                k = e
            continue
        out.append(c)
        k += 1
    return out


def render(chars):
    """Text-mode rendering: prose stays prose; chemistry -> \\ce{}, math -> $...$."""
    toks = [classify(w) for w in split_words(normalize_arrows(chars))]
    n = len(toks)
    # an element symbol that reads as a word ("Be", "B") is chemical when it
    # sits in a list next to other species, e.g. "Li, Be, B, Na"
    changed = True
    while changed:
        changed = False
        for k, (kind, pay) in enumerate(toks):
            if kind != "text":
                continue
            m = re.fullmatch(r"([A-Z][a-z]?)([,;.?]?)", pay)
            if not m or m.group(1) not in ELEMENTS or m.group(1) in ("I", "A"):
                continue
            nb = [toks[j] for j in (k - 1, k + 1) if 0 <= j < len(toks)]
            if any(t[0] == "chem" for t in nb):
                toks[k] = ("chem", ([], m.group(1), [Ch(m.group(2))] if m.group(2) else []))
                changed = True

    def species_side(j, step):
        """Is the token at j (looking in direction step) an equation context?"""
        if step < 0 and 0 <= j < n and toks[j][0] == "num" and not toks[j][1][2]:
            j -= 1  # skip a coefficient
        if not 0 <= j < n:
            return False
        if toks[j][0] == "arrow":
            return True
        if toks[j] == ("op", "+"):
            jj = j + step
            if step < 0 and 0 <= jj < n and toks[jj][0] == "num":
                return False
            return 0 <= jj < n and toks[jj][0] in ("chem", "arrow")
        return False

    # generic species in a schematic equation: "A → 2B", "2A + B → C"
    changed = True
    while changed:
        changed = False
        for k, (kind, pay) in enumerate(toks):
            if kind != "text":
                continue
            m = GENERIC_SPECIES_RE.fullmatch(pay)
            if m and (species_side(k - 1, -1) or species_side(k + 1, 1)):
                ce = (m.group(1) + " " if m.group(1) else "") + m.group(2)
                toks[k] = ("chem", ([], ce, [Ch(m.group(3))] if m.group(3) else []))
                changed = True
    # "1.23 V", "298 K": a unit, unless the number is a coefficient in an equation
    for k, (kind, pay) in enumerate(toks):
        if kind == "chem" and pay[1] in UNIT_SYMBOLS and not pay[0] and k and toks[k - 1][0] == "num" and not toks[k - 1][1][2]:
            near = [toks[j] for j in (k + 1, k - 2) if 0 <= j < n]
            if not any(t[0] == "arrow" or t == ("op", "+") for t in near):
                toks[k] = ("text", pay[1] + ctext(pay[2]))
    out = []
    i = 0

    def chemish(k):
        kind = toks[k][0]
        if kind in ("chem", "arrow", "blank"):
            return True
        if kind == "op":
            return toks[k][1] in ("+", "=")
        if kind == "num":
            return k + 1 < n and toks[k + 1][0] in ("chem", "blank")
        return False

    while i < n:
        kind, pay = toks[i]
        # chemical equation: maximal chem/op/arrow run containing an arrow
        if chemish(i) and kind != "op":
            j = i
            while j < n and chemish(j):
                t = toks[j]
                if j > i and t[0] == "chem" and toks[j - 1][0] == "chem" and any(x[0] == "arrow" for x in toks[i:j]):
                    break  # two species with nothing between: the next equation starts
                j += 1
                if t[0] == "chem" and any(c.c in SENT_PUNCT for c in t[1][2]):
                    break  # sentence punctuation ends the equation
                if t[0] in ("num",) and any(c.c in SENT_PUNCT for c in t[1][2]):
                    break
            run = toks[i:j]
            while run and run[-1][0] == "op":
                run.pop()
                j -= 1
            n_chem = sum(1 for t in run if t[0] == "chem")
            has_arrow = any(t[0] == "arrow" for t in run)
            if has_arrow and n_chem >= 1 and len(run) >= 3 and run[0][0] != "arrow":
                out.append(render_equation(run))
                i = j
                continue
        if kind == "chem":
            pre, ce, suf = pay
            out.append(wrap_pre_suf(pre, r"\ce{%s}" % ce, suf))
            i += 1
            continue
        # math run: math words joined with numbers/operators
        if kind in ("math", "num", "op", "tex"):
            j = i
            while j < n and toks[j][0] in ("math", "num", "op", "tex"):
                t = toks[j]
                j += 1
                if t[0] in ("math", "num") and any(c.c in ".?!;:," for c in t[1][2]) and j < n:
                    break
            run = toks[i:j]
            has_math = any(t[0] in ("math", "tex") for t in run) or any(
                t[0] == "op" and t[1] in ("×", "⋅", "·") for t in run
            )
            if has_math:
                lead = []
                while run and run[0][0] == "op" and run[0][1] not in ("–", "−", "-"):
                    lead.append(run.pop(0))
                trail = []
                while run and run[-1][0] == "op":
                    trail.insert(0, run.pop())
                for t in lead:
                    out.append(render_plain(t))
                if run:
                    out.append(render_math_run(run))
                for t in trail:
                    out.append(render_plain(t))
                i = j
                continue
        out.append(render_plain(toks[i]))
        i += 1
    return finalize(tidy(" ".join(x for x in out if x != "")))


def render_plain(tok):
    kind, pay = tok
    if kind == "num":
        pre, core, suf = pay
        return wrap_pre_suf(pre, ctext(core).replace("–", "-").replace("−", "-"), suf)
    if kind == "math":
        pre, core, suf = pay
        return wrap_pre_suf(pre, "$%s$" % math_word(core), suf)
    if kind == "tex":
        return "$%s$" % pay
    if kind == "arrow":
        return "$%s$" % {"->": r"\rightarrow", "<=>": r"\rightleftharpoons", "<-": r"\leftarrow",
                         "<->": r"\leftrightarrow"}[pay]
    if kind == "op":
        if pay in ("≥", "≤", "≈", "≠", "±", "×", "∝", "⋅", "·"):
            return "$%s$" % MATH_SYM[pay]
        return text_escape(pay)
    if kind == "blank":
        return r"$\underline{\qquad}$"
    t = pay
    if re.fullmatch(r"[–−]\d.*", t):
        t = "-" + t[1:]
    if UNKNOWN in t:
        t = t.replace(UNKNOWN, "?")
    return text_escape(t)


def render_math_run(run):
    """One $...$ for a run of math words / numbers / operators; sentence
    punctuation at either end stays outside the delimiters."""
    lead_txt = trail_txt = ""
    parts = []
    for k, (kind, pay) in enumerate(run):
        if kind in ("num", "math"):
            pre, core, suf = pay
            if k == 0:
                lead_txt, pre = ctext(pre), []
            if k == len(run) - 1:
                trail_txt, suf = ctext(suf), []
            p, s = math_escape(ctext(pre)), math_escape(ctext(suf))
            if kind == "num":
                body = ctext(core).replace("–", "-").replace("−", "-").replace(",", "{,}")
            else:
                body = math_word(core)
            parts.append(p + body + s)
        elif kind == "tex":
            parts.append(pay)
        elif kind == "op":
            parts.append(MATH_SYM.get(pay, pay))
    body = " ".join(parts).strip()
    return text_escape(lead_txt) + "$%s$" % body + text_escape(trail_txt)


def render_equation(run):
    """One \\ce{...} for the whole equation; with balancing blanks the pieces
    are separate \\ce{} groups inside $...$."""
    lead = trail = ""
    pieces = []
    blanks = any(t[0] == "blank" for t in run)
    for k, (kind, pay) in enumerate(run):
        if kind == "chem":
            pre, ce, suf = pay
            p, s = ctext(pre), ctext(suf)
            if k == 0:
                lead, p = p, ""
            if k == len(run) - 1:
                trail, s = s, ""
            piece = (p + ce + s).strip()
            pieces.append(r"\ce{%s}" % piece if blanks else piece)
        elif kind == "num":
            pieces.append(ctext(pay[1]))
        elif kind == "arrow":
            pieces.append(r"\ce{%s}" % pay if blanks else pay)
        elif kind == "op":
            pieces.append({"–": "-", "−": "-"}.get(pay, pay))
        elif kind == "blank":
            pieces.append(r"\underline{\qquad}\,")
    body = " ".join(pieces).replace(r"\, ", r"\,")
    if blanks:
        return text_escape(lead) + "$%s$" % body + text_escape(trail)
    return text_escape(lead) + r"\ce{%s}" % body + text_escape(trail)


def tidy(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:?!])(?=\s|$)", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def to_math_mode(rendered):
    """Turn a text-mode rendering into something valid inside math mode."""
    out = []
    segs = segments(rendered)
    for k, (kind, seg) in enumerate(segs):
        if kind in ("math", "ce"):
            out.append(seg)
        elif k == len(segs) - 1 and k and seg.strip() in (".", ","):
            continue  # sentence punctuation after a displayed formula
        elif seg.strip():
            out.append(text_to_math(seg))
    return "".join(out).strip()


# ----------------------------------------------------------- layout -> text

def join_lines(lines):
    """Join consecutive prose lines into one char list. Line-final hyphens are
    kept (these exams are not auto-hyphenated, so they are real hyphens)."""
    out = []
    for l in lines:
        ch = list(l["chars"])
        while ch and ch[0].c == " ":
            ch.pop(0)
        while ch and ch[-1].c == " ":
            ch.pop()
        if not ch:
            continue
        if out and not (out[-1].c in "-‐" and out[-1].s == "b" and len(out) >= 2 and out[-2].c.isalpha()):
            out.append(SPACE)
        out.extend(ch)
    return out


def group_rows(lines):
    rows = []
    for l in sorted(lines, key=lambda l: (l["y0"] + l["y1"]) / 2):
        cy = (l["y0"] + l["y1"]) / 2
        h = max(4.0, l["y1"] - l["y0"])
        if rows and abs(cy - rows[-1]["cy"]) < 0.45 * h:
            rows[-1]["cells"].append(l)
        else:
            rows.append({"cy": cy, "cells": [l]})
    for r in rows:
        r["cells"].sort(key=lambda l: l["x0"])
        r["y0"] = min(c["y0"] for c in r["cells"])
        r["y1"] = max(c["y1"] for c in r["cells"])
    return rows


LIST_LABEL_RE = re.compile(r"^\s*([IVX]+\.|\d+\.|[a-d]\.|\([a-d]\)|\([ivx]+\)|\([IVX]+\)|•|[a-d]\))\s*$")
LIST_START_RE = re.compile(r"^\s*([IVX]+\.|[a-d]\.|\([a-d]\)|\([ivx]+\)|\([IVX]+\)|\d\.|•)\s")


def row_chars(row, visuals):
    """Merge one row's cells into a single char list (arrows drawn between
    cells were already spliced in by merge_inline)."""
    out = []
    for k, c in enumerate(row["cells"]):
        if k:
            out.append(SPACE)
        out.extend(join_lines([c]))
    return out




def row_text(row):
    return " ".join(ctext(c["chars"]) for c in row["cells"])


EQ_WORDS = {"mol", "atm", "min", "torr", "rxn", "kcal", "cal", "graphite", "diamond", "rhombic", "monoclinic",
            "white", "red", "conc", "dil", "heat", "light", "energy", "catalyst"}


def prose_words(txt):
    t = re.sub(r"\([^()]*\)", " ", txt)
    return [w for w in re.findall(r"[A-Za-z]{3,}", t) if w.islower() and w not in EQ_WORDS]


def is_equation_row(row):
    """A line that is a chemical equation (possibly followed by ΔH°, E° or K),
    not prose that mentions one."""
    txt = row_text(row)
    if not any(a in txt for a in ARROW_CE) or LIST_START_RE.match(txt) or len(row["cells"]) > 2:
        return False
    return len(prose_words(txt)) <= 1


def ends_open(txt):
    """An equation line that continues on the next line ('... +' or '... →')."""
    t = txt.rstrip()
    return bool(t) and (t[-1] in "+=" or t[-1] in ARROW_CE)


ANN_START_RE = re.compile(r"^([Δ∆]|E\s*°|[A-Za-z]\s*[_°]|K\b|k\b)")


def eq_split(rows):
    """Chars of an equation display (one or more wrapped rows) -> (equation,
    annotation): a trailing 'ΔH° = ...', 'E° = ...' or 'K1' is set apart from
    the equation by a wide gap (or a separate cell) after the last arrow."""
    flat = []  # (Ch, x0, x1); None marks a cell boundary
    for r in rows:
        for c in r["cells"]:
            ch, xs = c["chars"], c.get("xs")
            ok = bool(xs) and len(xs) == len(ch)
            if flat:
                flat.append(None)
            flat.extend((x, xs[k][0], xs[k][1]) if ok else (x, None, None) for k, x in enumerate(ch))

    def chars(fs):
        out = []
        for f in fs:
            c = SPACE if f is None else f[0]
            if not (c.c == " " and (not out or out[-1].c == " ")):
                out.append(c)
        while out and out[-1].c == " ":
            out.pop()
        return out

    def annotation_ok(fs):
        t = ctext(chars(fs)).strip()
        if not t:
            return False
        if "=" in t or ANN_START_RE.match(t):
            return True
        w = split_words(chars(fs))
        return classify(w[0])[0] not in ("chem", "num", "op", "arrow", "blank")

    arrows = [i for i, f in enumerate(flat) if f and f[0].s == "b" and f[0].c in ARROW_CE]
    if not arrows:
        return chars(flat), []
    seen, last_x1 = False, None
    for i in range(arrows[-1] + 1, len(flat)):
        f = flat[i]
        if f is None:
            if seen and annotation_ok(flat[i + 1 :]):
                return chars(flat[:i]), chars(flat[i + 1 :])
            last_x1 = None
            continue
        ch, x0, x1 = f
        if ch.c == " ":
            continue
        if seen and last_x1 is not None and x0 is not None and x0 - last_x1 >= 15 and annotation_ok(flat[i:]):
            return chars(flat[:i]), chars(flat[i:])
        seen = True
        if x1 is not None:
            last_x1 = x1
    # no geometry: split before the first word after the right-hand side that
    # starts an annotation ("... 2 H2O(l) E° = 1.23 V")
    words = split_words(chars(flat[arrows[-1] + 1 :]))
    for k in range(1, len(words)):
        t = ctext(words[k])
        nxt = ctext(words[k + 1]) if k + 1 < len(words) else ""
        if t[:1] in "Δ∆" or t.startswith("E°") or nxt == "=" or (re.fullmatch(r"[a-z]{3,}", t) and t not in EQ_WORDS):
            head = chars(flat[: arrows[-1] + 1])
            left = []
            for w in words[:k]:
                left += [SPACE] + w
            right = []
            for w in words[k:]:
                right += ([SPACE] if right else []) + w
            return head + left, right
    return chars(flat), []


def single_ce(rendered):
    """The \\ce{...} group if a rendering is just one (bar end punctuation)."""
    segs = [s for s in segments(rendered) if not (s[0] == "text" and not s[1].strip(" .,;"))]
    return segs[0][1] if len(segs) == 1 and segs[0][0] == "ce" else None


def format_equations(group):
    """[(equation rendering, annotation in math mode)] -> display math; two or
    more equations are aligned on their arrows like the hand-corrected entries."""
    if len(group) == 1:
        e, a = group[0]
        body = single_ce(e) or to_math_mode(e)
        return r"\[ %s \]" % (body + (r" \qquad " + a if a else ""))
    lines = []
    for e, a in group:
        ce = single_ce(e)
        if ce:
            body = re.sub(r" (->|<=>|<-|<->) ", r" &\1 ", ce, count=1)
        else:
            body = to_math_mode(e)
        lines.append(body + (" & " + a if a else ""))
    return "\\begin{align*}\n" + " \\\\\n".join(lines) + "\n\\end{align*}"


def has_arrow_ce(ce):
    return bool(re.search(r"(->|<=>|<-|<->)", ce))


def math_like(segs):
    """A line that is a formula, not a sentence: some math/chemistry and at
    most three words of plain text, no sentence-ending punctuation."""
    if not any(k in ("math", "ce") for k, _ in segs):
        return False
    text = " ".join(t for k, t in segs if k == "text")
    if re.search(r"[.?!:]\s*$", text):
        return False
    return len(re.findall(r"[A-Za-z]{2,}", text)) <= 3


def emit_text(text, allow_display):
    """Rendered prose -> blocks. A lone formula line becomes display math, and
    an equation ending (or opening) a paragraph after a full sentence is set
    as its own display."""
    if not text:
        return []
    segs = segments(text)
    if allow_display and math_like(segs):
        return [("math", r"\[ %s \]" % to_math_mode(text))]
    core = segs[:-1] if segs and segs[-1][0] == "text" and segs[-1][1].strip() in (".", "") else segs
    if (len(core) >= 2 and core[-1][0] == "ce" and has_arrow_ce(core[-1][1]) and core[-2][0] == "text"
            and re.search(r"[.?!:]\s*$", core[-2][1])):
        head = join_segments(core[:-1]).rstrip()
        return emit_text(head, False) + [("math", r"\[ %s \]" % core[-1][1])]
    if (len(segs) >= 2 and segs[0][0] == "ce" and has_arrow_ce(segs[0][1]) and segs[1][0] == "text"
            and re.match(r"\s*[A-Z]", segs[1][1])):
        return [("math", r"\[ %s \]" % segs[0][1])] + emit_text(join_segments(segs[1:]).strip(), False)
    return [("para", nbsp_units(text))]


LABEL_SEQS = [["I.", "II.", "III.", "IV.", "V."], ["1.", "2.", "3.", "4.", "5."], ["a.", "b.", "c.", "d."],
              ["(a)", "(b)", "(c)", "(d)"], ["(i)", "(ii)", "(iii)", "(iv)"], ["(I)", "(II)", "(III)", "(IV)"]]


def format_list(items):
    """List items (chars, label stripped below) -> enumerate/itemize."""
    lines, bullet = [], False
    split = []
    for ch in items:
        t = ctext(ch)
        m0 = LIST_START_RE.match(t)
        cuts = []
        seq = next((q for q in LABEL_SEQS if m0 and m0.group(1) in q), None)
        if seq:
            want = seq.index(m0.group(1)) + 1
            for m in re.finditer(r"(?<=\s)(\S{1,5})(?=\s)", t):
                if want < len(seq) and m.group(1) == seq[want]:
                    cuts.append(m.start())
                    want += 1
        prev = 0
        for c in cuts + [len(ch)]:
            split.append(ch[prev:c])
            prev = c
    for ch in split:
        m = LIST_START_RE.match(ctext(ch)) or LIST_LABEL_RE.match(ctext(ch))
        if m:
            bullet = bullet or m.group(1) == "•"
            ch = ch[m.end() :]
        t = nbsp_units(render(ch))
        if t:
            lines.append("  \\item " + t)
    if not lines:
        return None
    env = "itemize" if bullet else "enumerate"
    return "\\begin{%s}\n%s\n\\end{%s}" % (env, "\n".join(lines), env)


def join_blocks(blocks):
    """Blocks -> text: paragraphs and environments are separated by a blank
    line; display math sits on its own lines inside the paragraph."""
    out = ""
    for k, (kind, t) in enumerate(blocks):
        if k:
            out += "\n" if kind == "math" or blocks[k - 1][0] == "math" else "\n\n"
        out += t
    return out


def format_table(rows):
    ncol = len(rows[0])
    head = rows[0]
    has_header = (len(rows) >= 2 and any(ctext(ch).strip() for ch, _ in head)
                  and not any(c.s == "b" and c.c.isdigit() for ch, _ in head for c in ch)
                  and not any(single_ce(r) for _, r in head))
    if not has_header:
        body = " \\\\\n".join(" & ".join(to_math_mode(r) for _, r in row) for row in rows)
        return ("math", "\\[\n\\begin{array}{%s}\n%s\n\\end{array}\n\\]" % ("l" * ncol, body))

    def head_cell(r):
        return r"\textbf{%s}" % r if r and all(k == "text" for k, _ in segments(r)) else r

    def body_cell(r):
        return "$%s$" % r if NUM_RE.match(r) or re.fullmatch(r"\+\d[\d,]*(\.\d*)?", r) else nbsp_units(r)

    lines = [" & ".join(head_cell(r) for _, r in head) + r" \\", r"\hline"]
    lines += [" & ".join(body_cell(r) for _, r in row) + r" \\" for row in rows[1:]]
    return ("env", "\\begin{center}\n\\begin{tabular}{%s}\n\\hline\n%s\n\\hline\n\\end{tabular}\n\\end{center}"
            % ("l" + "c" * (ncol - 1), "\n".join(lines)))


def layout_blocks(lines, visuals, force_float=False):
    """-> [(kind, text)] with kind 'para', 'math' (display) or 'env'."""
    if not lines:
        return []
    indent = min(round(l["x0"]) for l in (lines[1:] or lines))
    rows = group_rows(lines)

    if not force_float:
        side = find_sidebar(rows, indent)
        if side:
            ids = {id(c) for c in side}
            rest = [l for l in lines if id(l) not in ids]
            sy1 = max(c["y1"] for c in side)
            top = [l for l in rest if l["y0"] <= sy1]
            bot = [l for l in rest if l["y0"] > sy1]
            tail = ctext(top[-1]["chars"]).rstrip() if top else ""
            if bot and tail and tail[-1] not in ".?!:" and ctext(bot[0]["chars"]).lstrip()[:1].islower():
                # the sentence continues below the sidebar: keep it whole
                return layout_blocks(top + bot, visuals) + layout_blocks(side, visuals, True)
            return layout_blocks(top, visuals) + layout_blocks(side, visuals, True) + layout_blocks(bot, visuals)

    blocks = []
    para = []
    float_rows = []

    def flush_para():
        if para:
            blocks.append(("para", (join_lines(para), len(para))))
            para.clear()

    def flush_float():
        if not float_rows:
            return
        grp = []

        def flush_grp():
            if not grp:
                return
            # an equation heading or closing a table is not a table row
            head, tail = [], []
            while grp and is_equation_row(grp[0]) and len(grp[0]["cells"]) == 1:
                head.append(grp.pop(0))
            while grp and is_equation_row(grp[-1]) and len(grp[-1]["cells"]) == 1:
                tail.insert(0, grp.pop())
            for r in head:
                blocks.append(("eq", [r]))
            multi = [r for r in grp if len(r["cells"]) >= 2]
            if len(grp) >= 2 and (len(multi) >= 2 or (multi and len(grp) <= 3)):
                blocks.append(("table", build_table(grp)))
            else:
                for r in grp:
                    blocks.append(("display", row_chars(r, visuals)))
            for r in tail:
                blocks.append(("eq", [r]))
            grp.clear()

        for r in float_rows:
            first = ctext(r["cells"][0]["chars"])
            if LIST_LABEL_RE.match(first) or LIST_START_RE.match(first):
                flush_grp()
                blocks.append(("item", row_chars(r, visuals)))
            elif not grp and blocks and blocks[-1][0] == "eq" and len(prose_words(row_text(r))) <= 1 and (
                ends_open(row_text(blocks[-1][1][-1])) or row_text(r).lstrip()[:1] in "+" + "".join(ARROW_CE)
            ):
                blocks[-1][1].append(r)  # an equation wrapped onto the next line
            elif is_equation_row(r) and not (grp and len(r["cells"]) >= 2):
                flush_grp()
                blocks.append(("eq", [r]))
            elif blocks and blocks[-1][0] == "item" and not grp and len(r["cells"]) == 1 and (
                r["cells"][0]["x0"] > indent + 4 or force_float
            ):
                blocks[-1] = ("item", blocks[-1][1] + [SPACE] + join_lines(r["cells"]))  # wrapped item
            else:
                grp.append(r)
        flush_grp()
        float_rows.clear()

    prev_y1 = None
    for r in rows:
        cells = r["cells"]
        first = cells[0]
        is_prose = not force_float and first["x0"] <= indent + 4 and len(cells) == 1
        if is_prose and (LIST_START_RE.match(first["text"]) or is_equation_row(r)):
            is_prose = False
        if is_prose and float_rows and is_equation_row(float_rows[-1]) and ends_open(row_text(float_rows[-1])):
            is_prose = False  # the rest of a wrapped equation
        gap = (r["y0"] - prev_y1) if prev_y1 is not None else 0
        if not is_prose and para and not float_rows and ends_open(ctext(para[-1]["chars"])) and (
                len(prose_words(row_text(r))) <= 1 and not LIST_START_RE.match(first["text"])):
            # an equation begun at the end of a prose line continues here
            para.extend(cells)
            prev_y1 = r["y1"]
            continue
        if is_prose:
            if float_rows:
                tail = ctext(para[-1]["chars"]).rstrip() if para else ""
                if (tail and tail[-1] not in ".?!:" and ctext(first["chars"]).lstrip()[:1].islower()
                        and not all(is_equation_row(fr) for fr in float_rows)):
                    # a table floated into the middle of a sentence: finish the
                    # sentence first, emit the table after the paragraph (an
                    # equation stays in place, displayed within the sentence)
                    para.append(first)
                    prev_y1 = r["y1"]
                    continue
                flush_para()
                flush_float()
            elif para and gap > 0.9 * (first["y1"] - first["y0"]):
                flush_para()
            para.append(first)
        else:
            float_rows.append(r)
        prev_y1 = r["y1"]
    flush_para()
    flush_float()

    out = []
    k = 0
    while k < len(blocks):
        kind, payload = blocks[k]
        if kind == "eq":
            grp = []
            while k < len(blocks) and blocks[k][0] == "eq":
                eq, ann = eq_split(blocks[k][1])
                grp.append((render(eq), to_math_mode(render(ann)) if ann else ""))
                k += 1
            out.append(("math", format_equations(grp)))
            continue
        if kind == "item":
            grp = []
            while k < len(blocks) and blocks[k][0] == "item":
                grp.append(blocks[k][1])
                k += 1
            env = format_list(grp)
            if env:
                out.append(("env", env))
            continue
        if kind == "table":
            for b in payload:
                if b[0] == "para" and out and out[-1][0] == "para" and b[1][:1].islower():
                    out[-1] = ("para", out[-1][1] + " " + b[1])  # the sentence beside the table
                else:
                    out.append(b)
        elif kind == "para":
            chars, nlines = payload
            out.extend(emit_text(render(chars), nlines == 1))
        else:
            out.extend(emit_text(render(payload), True))
        k += 1
    return out


def build_table(rows):
    """Assign each cell to a column; -> ('math', \\[ array \\]) for a plain
    grid, or ('env', center/tabular) when the first row is a header."""
    width = Counter(len(r["cells"]) for r in rows)
    ncol = max(width)
    ref = [r for r in rows if len(r["cells"]) == ncol]
    spans = sorted([c["x0"], c["x1"]] for r in ref for c in r["cells"])
    cols = []
    for x0, x1 in spans:
        if cols and x0 <= cols[-1][1] - 1:
            cols[-1][1] = max(cols[-1][1], x1)
        else:
            cols.append([x0, x1])

    def col_index(c):
        cx = (c["x0"] + c["x1"]) / 2
        for k, (a, b) in enumerate(cols):
            if a - 2 <= cx <= b + 2:
                return k
        return min(range(len(cols)), key=lambda k: min(abs(cx - cols[k][0]), abs(cx - cols[k][1])))

    # rows as {col: [lines]}, then merge wrapped cells back together
    grid = []
    for r in rows:
        g = defaultdict(list)
        for c in r["cells"]:
            g[col_index(c)].append(c)
        grid.append({"g": g, "y0": r["y0"], "y1": r["y1"]})
    h = sorted(r["y1"] - r["y0"] for r in rows)[len(rows) // 2]
    merged = []
    for r in grid:
        if merged:
            p = merged[-1]
            close = r["y0"] - p["y1"] < 0.6 * h
            if close and not (set(p["g"]) & set(r["g"])):
                for k, v in r["g"].items():
                    p["g"][k].extend(v)
                p["y1"] = max(p["y1"], r["y1"])
                continue
            if close and len(r["g"]) == 1:
                k = next(iter(r["g"]))
                prev = ctext(join_lines(p["g"].get(k, []))).rstrip()
                nxt = ctext(join_lines(r["g"][k])).lstrip()
                if prev and (prev[-1] in "+=,(–-" + ARROW + EQARROW or nxt[:1].islower()):
                    p["g"][k].extend(r["g"][k])
                    p["y1"] = max(p["y1"], r["y1"])
                    continue
        merged.append(r)
    cells = []
    for r in merged:
        row = []
        for k in range(len(cols)):
            ch = join_lines(sorted(r["g"].get(k, []), key=lambda l: l["y0"]))
            row.append((ch, render(ch)))
        cells.append(row)
    # prose or an equation set beside the table lands in column 0 (with gaps);
    # it is text before the table, not a column of it
    col0 = [row[0][0] for row in cells]
    col0_txt = " ".join(ctext(ch) for ch in col0)
    if (len(cols) >= 3 and any(not ctext(ch).strip() for ch in col0)
            and all(any(ctext(ch).strip() for ch, _ in row[1:]) for row in cells)
            and (ARROW in col0_txt or EQARROW in col0_txt or len(prose_words(col0_txt)) >= 2)):
        side = []
        for ch in col0:
            if ctext(ch).strip():
                side += ([SPACE] if side else []) + ch
        return emit_text(render(side), True) + [format_table([row[1:] for row in cells])]
    return [format_table(cells)]


def find_sidebar(rows, indent):
    """Detect a table/list set beside wrapped prose (e.g. 2007 local Q24): rows
    whose first cell is prose at the indent and whose other cells sit right of
    a common gutter, where the prose visibly continues line to line."""
    cand = [r for r in rows if r["cells"][0]["x0"] <= indent + 4 and len(r["cells"]) >= 2]
    if len(cand) < 2:
        return None
    gutter = min(c["x0"] for r in cand for c in r["cells"][1:])
    cand_ids = {id(r["cells"][0]) for r in cand}
    prose = [r["cells"][0] for r in rows if r["cells"][0]["x0"] <= indent + 4 and r["cells"][0]["x1"] < gutter + 1]
    cont = 0
    for a, b in zip(prose, prose[1:]):
        if (id(a) in cand_ids or id(b) in cand_ids) and ctext(b["chars"]).lstrip()[:1].islower():
            cont += 1
    if cont < max(1, len(cand) // 2):
        return None
    y0 = min(r["y0"] for r in cand)
    y1 = max(r["y1"] for r in cand)
    side = []
    for r in rows:
        for c in r["cells"]:
            if c["x0"] >= gutter - 2 and y0 - 30 <= c["y0"] <= y1 + 30:
                side.append(c)
    return side if side else None


def layout_stem(lines, visuals, force_float=False):
    """Lines of a question stem (number already removed) -> LaTeX text."""
    return join_blocks(layout_blocks(lines, visuals, force_float))


# ---------------------------------------------------------- inline arrows

def arrow_candidates(vis):
    out = []
    for v in vis:
        kind, x0, y0, x1, y1 = v
        w, h = x1 - x0, y1 - y0
        if kind == "draw" and 7 <= w <= 45 and h <= 9:
            out.append(v)
        elif kind == "img" and 8 <= w <= 45 and 2 <= h <= 18:
            out.append(v)
    return out


def classify_arrow(page, boxes):
    """'→', '⇌', or None if this is not an arrow: count separate long
    horizontal strokes in the rendering and require an arrowhead at an end."""
    if all(b[0] == "draw" and b[4] - b[2] < 1.2 for b in boxes):
        return None  # a bare horizontal rule
    x0 = min(b[1] for b in boxes) - 0.5
    y0 = min(b[2] for b in boxes) - 0.5
    x1 = max(b[3] for b in boxes) + 0.5
    y1 = max(b[4] for b in boxes) + 0.5
    pix = page.get_pixmap(matrix=fitz.Matrix(6, 6), clip=fitz.Rect(x0, y0, x1, y1), colorspace=fitz.csGRAY)
    w, h, buf = pix.width, pix.height, pix.samples
    runs, inrun, ends, mid = 0, False, 0, 0
    for yy in range(h):
        row = buf[yy * pix.stride : yy * pix.stride + w]
        dark = [i for i in range(w) if row[i] < 140]
        line = len(dark) >= 0.5 * w
        if line and not inrun:
            runs += 1
        elif not line:
            for i in dark:
                if i < 0.35 * w or i > 0.65 * w:
                    ends += 1
                else:
                    mid += 1
        inrun = line
    if runs == 0 or ends < 0.008 * w * h or mid > ends:
        return None
    return EQARROW if runs >= 2 else ARROW


def cut_index(l, x):
    """Index of the first character of line l starting at/after page x."""
    xs = l.get("xs")
    if not xs or len(xs) != len(l["chars"]):
        return None
    for i, (a, b) in enumerate(xs):
        if (a + b) / 2 >= x:
            return i
    return len(xs)


def sub_line(l, i, j):
    chars, xs = l["chars"][i:j], l["xs"][i:j]
    solid = [x for c, x in zip(chars, xs) if c.c != " "]
    x0 = min(a for a, _ in solid) if solid else l["x0"]
    x1 = max(b for _, b in solid) if solid else l["x1"]
    return dict(l, chars=chars, xs=xs, text=ctext(chars), x0=x0, x1=x1)


def split_at_rules(lines, vis):
    """Split a PDF line wherever a vertical rule (box/table border) crosses it."""
    vr = [v for v in vis if v[0] == "draw" and v[3] - v[1] <= 2.5 and v[4] - v[2] > 4]
    out = []
    for l in lines:
        cuts = set()
        for v in vr:
            x = (v[1] + v[3]) / 2
            if l["x0"] + 2 < x < l["x1"] - 2 and v[2] < l["y1"] - 2 and v[4] > l["y0"] + 2:
                k = cut_index(l, x)
                if k:
                    cuts.add(k)
        if not cuts:
            out.append(l)
            continue
        prev = 0
        for k in sorted(cuts) + [len(l["chars"])]:
            if ctext(l["chars"][prev:k]).strip():
                out.append(sub_line(l, prev, k))
            prev = k
    return out


def merge_inline(lines, vis, page):
    """Splice drawn/raster reaction arrows into the text they sit in, and join
    the cells of a row that the arrow connects. Returns (lines, used)."""
    arrows = arrow_candidates(vis)
    used = []
    lines = split_at_rules([dict(l) for l in lines], vis)
    # arrows inside a single line (sitting over a run of spaces)
    for v in arrows:
        cx, cy = (v[1] + v[3]) / 2, (v[2] + v[4]) / 2
        for l in lines:
            if not (l["x0"] + 1 < cx < l["x1"] - 1 and l["y0"] - 2 <= cy <= l["y1"] + 2):
                continue
            k = cut_index(l, cx)
            if k is None:
                continue
            sym = classify_arrow(page, [v])
            if sym:
                l["chars"] = l["chars"][:k] + [SPACE, Ch(sym), SPACE] + l["chars"][k:]
                l["xs"] = l["xs"][:k] + [(cx, cx)] * 3 + l["xs"][k:]
                l["text"] = ctext(l["chars"])
                used.append(v)
            break
    rows = group_rows(lines)
    out = []
    for r in rows:
        cells = r["cells"]
        merged = [cells[0]]
        for c in cells[1:]:
            a = merged[-1]
            ya, yb = min(a["y0"], c["y0"]) - 3, max(a["y1"], c["y1"]) + 3
            hits = [v for v in arrows if v not in used and v[1] >= a["x1"] - 3 and v[3] <= c["x0"] + 3
                    and v[2] >= ya and v[4] <= yb]
            sym = classify_arrow(page, hits) if hits else None
            if not sym and c["x0"] - a["x1"] < 2.5:
                # touching fragments (e.g. a stacked sub/superscript set as its own line)
                chars = list(a["chars"]) + list(c["chars"])
                merged[-1] = dict(a, chars=chars, text=ctext(chars), x1=max(a["x1"], c["x1"]),
                                  y0=min(a["y0"], c["y0"]), y1=max(a["y1"], c["y1"]), xs=None)
                continue
            if sym:
                used.extend(hits)
                chars = list(a["chars"]) + [SPACE, Ch(sym), SPACE] + list(c["chars"])
                merged[-1] = dict(a, chars=chars, text=ctext(chars), x1=c["x1"], y0=min(a["y0"], c["y0"]),
                                  y1=max(a["y1"], c["y1"]), xs=None)
            else:
                merged.append(c)
        out.extend(merged)
    out.sort(key=lambda l: (l["y0"], l["x0"]))
    return out, used


# ---------------------------------------------------------- fractions

def merge_fractions(lines, visuals):
    """Replace 'numerator over a drawn rule over denominator' with a single
    synthetic line holding a \\frac fragment. Returns (lines, used_rules)."""
    vrules = [v for v in visuals if v[0] == "draw" and v[3] - v[1] <= 2.5 and v[4] - v[2] > 5]
    rules = sorted(
        (v for v in visuals if v[0] == "draw" and v[4] - v[2] <= 1.6 and v[3] - v[1] >= 6
         # a rule touching a vertical rule is a table border, not a fraction bar
         and not any(abs(u[1] - x) < 3 and u[2] - 2 <= v[2] <= u[4] + 2 for u in vrules for x in (v[1], v[3]))),
        key=lambda v: v[2],
    )
    used = []
    lines = list(lines)
    for rule in rules:
        _, rx0, ry0, rx1, ry1 = rule
        ry = (ry0 + ry1) / 2

        rw = rx1 - rx0

        def under(l):
            # centred on the bar and not wider than it
            cx = (l["x0"] + l["x1"]) / 2
            return abs(cx - (rx0 + rx1) / 2) <= 0.2 * rw + 3 and l["x1"] - l["x0"] <= rw + 8

        num = [l for l in lines if under(l) and ry - 6 <= l["y1"] <= ry + 2.5 and l["y0"] < ry]
        den = [l for l in lines if under(l) and ry - 2.5 <= l["y0"] <= ry + 6 and l["y1"] > ry]
        num = [l for l in num if l not in den]
        if not num or not den:
            continue
        if max(l["x1"] - l["x0"] for l in num + den) < 0.45 * rw:
            continue  # bar much wider than its text: an underline / table rule
        n = render(join_lines(sorted(num, key=lambda l: (l["y0"], l["x0"]))))
        d = render(join_lines(sorted(den, key=lambda l: (l["y0"], l["x0"]))))
        tex = r"\frac{%s}{%s}" % (to_math_mode(n), to_math_mode(d))
        for l in num + den:
            lines.remove(l)
        y0 = min(l["y0"] for l in num)
        y1 = max(l["y1"] for l in den)
        h = 0.5 * ((num[0]["y1"] - num[0]["y0"]) + (den[0]["y1"] - den[0]["y0"])) / 2
        synth = {"x0": rx0, "x1": rx1, "y0": ry - h, "y1": ry + h, "chars": [Ch(tex, "tex")],
                 "text": "FRAC", "frac_box": (y0, y1)}
        # glue onto a line on the same baseline immediately left/right, if any
        mate = [l for l in lines if l["y0"] - 2 <= ry <= l["y1"] + 2 and (0 <= rx0 - l["x1"] <= 14 or 0 <= l["x0"] - rx1 <= 14)]
        if mate:
            m = min(mate, key=lambda l: min(abs(rx0 - l["x1"]), abs(l["x0"] - rx1)))
            lines.remove(m)
            left = m["x1"] <= rx0 + 1
            chars = (m["chars"] + [SPACE, synth["chars"][0]]) if left else ([synth["chars"][0], SPACE] + m["chars"])
            m = dict(m, chars=chars, text=ctext(chars), x0=min(m["x0"], rx0), x1=max(m["x1"], rx1))
            lines.append(m)
        else:
            lines.append(synth)
        used.append(rule)
    lines.sort(key=lambda l: (l["y0"], l["x0"]))
    return lines, used


# --------------------------------------------------------------- choices

MARK_RE = re.compile(r"\(([A-D])\)")


def split_wide(chars, xs, gap=10):
    """Split a run of characters into cells wherever blank space spans >= gap pt."""
    cells, cur = [], []
    k = 0
    while k < len(chars):
        if chars[k].c == " ":
            j = k
            while j < len(chars) and chars[j].c == " ":
                j += 1
            left = xs[k - 1][1] if k > 0 else None
            right = xs[j][0] if j < len(chars) else None
            if cur and left is not None and right is not None and right - left >= gap:
                cells.append(cur)
                cur = []
            elif cur:
                cur.append(SPACE)
            k = j
            continue
        cur.append(chars[k])
        k += 1
    if cur:
        cells.append(cur)
    return cells or [[]]


def split_choices(lines):
    """Choice-region lines -> {letter: {"cells": [chars...], "below": [lines]}}."""
    pieces = []
    conts = []
    for l in lines:
        txt = l["text"]
        ms = list(MARK_RE.finditer(txt))
        if not ms:
            conts.append(l)
            continue
        if ms[0].start() > 0 and txt[: ms[0].start()].strip():
            conts.append({**l, "chars": l["chars"][: ms[0].start()], "x1": l["x0"] + 1})
        for k, m in enumerate(ms):
            end = ms[k + 1].start() if k + 1 < len(ms) else len(txt)
            seg = l["chars"][m.end() : end]
            xs = l.get("xs")
            if xs and len(xs) == len(l["chars"]):
                pieces.append((m.group(1), l["x0"], (l["y0"] + l["y1"]) / 2, split_wide(seg, xs[m.end() : end]), True))
                pieces[-1] = (pieces[-1][0], xs[m.start()][0], pieces[-1][2], pieces[-1][3], True)
                continue
            # approximate the marker's own x position within the line
            frac = m.start() / max(1, len(txt))
            mx = l["x0"] + frac * (l["x1"] - l["x0"])
            pieces.append((m.group(1), mx, (l["y0"] + l["y1"]) / 2, [l["chars"][m.end() : end]], True))
    choices = {}
    for letter, mx, cy, cells, _ in pieces:
        if letter not in choices:
            choices[letter] = {"cy": cy, "x0": mx, "cells": [list(c) for c in cells], "extra": []}
    for l in conts:
        cy = (l["y0"] + l["y1"]) / 2
        best = None
        for letter, ch in choices.items():
            if ch["cy"] - 4 <= cy and ch["x0"] - 5 <= l["x0"]:
                score = (cy - ch["cy"]) + 0.3 * (l["x0"] - ch["x0"])
                if best is None or score < best[0]:
                    best = (score, letter)
        if best:
            choices[best[1]]["extra"].append(l)
    result = {}
    for letter in "ABCD":
        ch = choices.get(letter)
        if ch is None:
            result[letter] = None
            continue
        same_row, below = [], []
        for e in ch["extra"]:
            (same_row if abs((e["y0"] + e["y1"]) / 2 - ch["cy"]) < 4 else below).append(e)
        cells = ch["cells"] + [join_lines([e]) for e in sorted(same_row, key=lambda e: e["x0"])]
        result[letter] = {"cells": cells, "below": sorted(below, key=lambda e: e["y0"])}
    return result


def render_choice(c, header):
    if c is None:
        return None
    cells = [cl for cl in c["cells"] if ctext(cl).strip()]
    if header and len(cells) == len(header):
        return nbsp_units("; ".join("%s: %s" % (h, render(v)) for h, v in zip(header, cells)))
    chars = []
    for cl in cells:
        if chars:
            chars.append(SPACE)
        chars.extend(cl)
    below = join_lines(c["below"])
    if below:
        chars.append(SPACE)
        chars.extend(below)
    t = nbsp_units(render(chars))
    return t if t else None


# ------------------------------------------------------------- per question

QNUM_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*\.?\s*")


def strip_number(lines, num, prefix_re=QNUM_PREFIX_RE):
    """Remove the leading 'N.' from the question's first numbered line."""
    for l in sorted(lines, key=lambda l: (l["y0"], l["x0"])):
        m = prefix_re.match(l["text"])
        if m and int(m.group(1)) == num:
            rest = l["chars"][m.end() :]
            k = lines.index(l)
            if not ctext(rest).strip():
                lines.pop(k)
            else:
                frac = m.end() / max(1, len(l["text"]))
                lines[k] = dict(l, chars=rest, text=ctext(rest), x0=l["x0"] + frac * (l["x1"] - l["x0"]))
            return lines
    return lines


def has_figure(vis, used_rules):
    """True if the visuals plausibly include a picture (not just rules,
    borders, arrows or fraction bars)."""
    keep = []
    for v in vis:
        if v in used_rules:
            continue
        kind, x0, y0, x1, y1 = v
        w, h = x1 - x0, y1 - y0
        if kind == "img":
            if w * h > 150:
                return True
            continue
        if h < 2.5 or w < 2.5:  # rules, underlines, table borders
            continue
        if 7 <= w <= 45 and h <= 9:  # equation arrows
            continue
        keep.append(v)
    if not keep:
        return False
    x0 = min(v[1] for v in keep)
    y0 = min(v[2] for v in keep)
    x1 = max(v[3] for v in keep)
    y1 = max(v[4] for v in keep)
    area = (x1 - x0) * (y1 - y0)
    # a single filled rectangle is almost always a table frame / shading
    if len(keep) == 1 and area > 0:
        _, a, b, c, d = keep[0]
        return False if (c - a) > 120 else area > 1500
    return area >= 600


DROP_LOG = None  # set to a list to collect the figure labels dropped (debugging)


def figure_region(vis, used_rules):
    """Bounding box of the picture in a question (the visuals has_figure counts,
    grown over the axis rules that touch them), or None."""
    if not has_figure(vis, used_rules):
        return None
    keep = [v for v in vis if v not in used_rules and (v[0] == "img" or (v[3] - v[1] >= 2.5 and v[4] - v[2] >= 2.5
            and not (7 <= v[3] - v[1] <= 45 and v[4] - v[2] <= 9)))]
    if not keep:
        return None
    x0, y0 = min(v[1] for v in keep), min(v[2] for v in keep)
    x1, y1 = max(v[3] for v in keep), max(v[4] for v in keep)
    for v in vis:
        if v not in used_rules and v[1] <= x1 + 2 and v[3] >= x0 - 2 and v[2] <= y1 + 2 and v[4] >= y0 - 2:
            x0, y0, x1, y1 = min(x0, v[1]), min(y0, v[2]), max(x1, v[3]), max(y1, v[4])
    return x0, y0, x1, y1


def drop_figure_text(lines, region, margin=12):
    """Labels inside a picture (axis titles, tick values, atom labels) are part
    of the figure, not the question text: drop short lines within it."""
    if not region:
        return lines
    x0, y0, x1, y1 = region
    indent = min((l["x0"] for l in lines), default=0)
    out = []
    for l in lines:
        txt = ctext(l["chars"]).strip()
        if ("=" in txt or any(c.s == "tex" for c in l["chars"]) or re.search(r"[.?:,;]$", txt)
                or (l["x0"] <= indent + 4 and len(re.findall(r"\b[a-z]{3,}\b", txt)) >= 2)):
            out.append(l)  # prose or a worked equation beside the picture
            continue
        cx, cy = (l["x0"] + l["x1"]) / 2, (l["y0"] + l["y1"]) / 2
        def inside(m):
            return x0 - m <= cx <= x1 + m and y0 - m <= cy <= y1 + m
        short = len(txt.split()) <= 4
        small = all(c.s != "b" for c in l["chars"] if c.c != " ")  # free-standing small label
        if (inside(margin) and short) or (inside(3 * margin) and small):
            if DROP_LOG is not None:
                DROP_LOG.append(txt)
            continue
        out.append(l)
    return out


def build_entry(year_label, year, exam, num, pieces, answer, img_dir, web_prefix):
    """pieces: list of (page, col, top, bottom) in reading order."""
    stem_lines, choice_lines = [], []
    stem_vis, choice_vis = [], []
    for page, col, top, bottom in pieces:
        ls = lines_in(LINE_CACHE[page.number], page.rect.width, col, top, bottom)
        vis = visuals_in(page, col, top, bottom)
        ls, used_arrows = merge_inline(ls, vis, page)
        vis = [v for v in vis if v not in used_arrows]
        a_line = next((l for l in ls if re.match(r"^\s*\(A\)", l["text"])), None)
        if a_line is not None:
            # in two-column choice layouts "(B)" can sit a hair above "(A)"
            acy = (a_line["y0"] + a_line["y1"]) / 2
            a_line = min((l for l in ls if re.match(r"^\s*\([A-D]\)", l["text"]) and abs((l["y0"] + l["y1"]) / 2 - acy) < 4),
                         key=lambda l: l["y0"])
        if a_line is None:
            # a continuation piece without (A): choices continue, or the stem does
            (choice_lines if choice_lines else stem_lines).extend(ls)
            (choice_vis if choice_lines else stem_vis).extend(vis)
        else:
            cut = a_line["y0"] - 2
            stem_lines.extend(l for l in ls if l["y0"] < cut)
            choice_lines.extend(l for l in ls if l["y0"] >= cut)
            stem_vis.extend(v for v in vis if v[2] < cut)
            choice_vis.extend(v for v in vis if v[2] >= cut)

    stem_lines = strip_number(list(stem_lines), num)
    stem_lines, used_s = merge_fractions(stem_lines, stem_vis)
    stem_lines = drop_figure_text(stem_lines, figure_region(stem_vis, used_s))
    choice_lines, used_c = merge_fractions(choice_lines, choice_vis)

    choice_map = split_choices(choice_lines) if choice_lines else {k: None for k in "ABCD"}

    # tabular choices: the stem's last row is a header whose cells line up
    # with each choice's value cells, e.g. "ΔH  ΔS" over "(A) +  –"
    header = None
    rows = group_rows(stem_lines)
    if rows and choice_lines:
        last = rows[-1]
        counts = [len([cl for cl in c["cells"] if ctext(cl).strip()]) for c in choice_map.values() if c]
        first_txt = ctext(last["cells"][0]["chars"]).strip()
        left = min(l["x0"] for l in choice_lines)
        if (len(last["cells"]) >= 2 and counts and all(k == len(last["cells"]) for k in counts)
                and last["cells"][0]["x0"] > left + 5 and not first_txt.endswith(("?", ".", ":"))):
            header = [render(join_lines([c])) for c in last["cells"]]
            ids = {id(c) for c in last["cells"]}
            stem_lines = [l for l in stem_lines if id(l) not in ids]

    question = layout_stem(stem_lines, stem_vis)
    choices = {k: render_choice(choice_map.get(k), header) for k in "ABCD"}
    # a bare one-letter element symbol among chemical-formula choices, e.g. "K"
    if sum(bool(v and single_ce(v)) for v in choices.values()) >= 2:
        for k, v in choices.items():
            if v and v in ELEMENTS:
                choices[k] = r"\ce{%s}" % v

    q_fig = has_figure(stem_vis, used_s)
    a_fig = has_figure(choice_vis, used_c)
    if any(v is None for v in choices.values()) and (choice_vis or not choice_lines):
        a_fig = True
    if a_fig:
        # image-based answers: the choices live in the picture, not in text
        choices = {k: None for k in "ABCD"}

    base = f"{year_label}_{exam}_{num}"
    if (img_dir / f"{base}.png").exists():
        images = [f"{web_prefix}/{base}.png"]
    else:
        images = sorted(f"{web_prefix}/{p.name}" for p in img_dir.glob(f"{base}_[0-9].png"))
    qimg = [f"{web_prefix}/{base}_question.png"] if (img_dir / f"{base}_question.png").exists() else []
    sub = [f"{web_prefix}/{base}_subtext.png"] if (img_dir / f"{base}_subtext.png").exists() else None

    return {
        "id": base,
        "exam_type": exam,
        "year": year,
        "number": num,
        "topic": TOPICS[(num - 1) // 6],
        "question": question,
        "question_image": q_fig,
        "answer": answer,
        "answer_image": a_fig,
        "choices": choices,
        "images": {"image": images, "question_image": qimg, "subtext": sub},
        "solution": {"text": None, "image": None},
    }


LINE_CACHE = {}
DRAW_CACHE = {}

KEY_PAIR_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*\.\s+([A-D](?:\s*(?:,|or|and|&|/)\s*[A-D])*)(?![A-Za-z])")


def parse_key(doc):
    """Answer key from the exam's KEY page, read row by row from word
    positions: each question number takes the A-D letters to its right up
    to the next number. Handles 'N. X', 'N X %', and column-major layouts."""
    for page in doc:
        t = page.get_text()
        if not re.search(r"\bKEY\b", t):
            continue
        words = page.get_text("words")
        rows = []
        for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
            cy = (w[1] + w[3]) / 2
            if rows and abs(cy - rows[-1][0]) < 4:
                rows[-1][1].append(w)
            else:
                rows.append([cy, [w]])
        key = {}
        for _, ws in rows:
            cur = None
            for w in sorted(ws, key=lambda w: w[0]):
                tok = w[4]
                m = re.fullmatch(r"(\d{1,2})\.?", tok)
                if m and 1 <= int(m.group(1)) <= 80:
                    cur = int(m.group(1))
                    key.setdefault(cur, [])
                    continue
                if cur is not None:
                    for letter in re.findall(r"(?<![A-Za-z])([A-D])(?![A-Za-z])", tok):
                        if tok.strip(",/&") in ("A", "B", "C", "D") or re.fullmatch(r"[A-D](?:[,/&][A-D])+,?", tok):
                            key[cur].append(letter)
        out = {n: ",".join(dict.fromkeys(v)) for n, v in key.items() if v}
        if len(out) >= 40:
            return out
        # fall back to reading order ("1. B 2. D ...")
        t = re.sub(r"\s+", " ", t)
        out = {}
        for m in KEY_PAIR_RE.finditer(t):
            out.setdefault(int(m.group(1)), re.sub(r"\s*(?:,|or|and|&|/)\s*", ",", m.group(2)))
        return out
    return {}


def load_caches(doc):
    LINE_CACHE.clear()
    DRAW_CACHE.clear()
    for page in doc:
        LINE_CACHE[page.number] = page_lines(page)
        DRAW_CACHE[page.number] = [
            tuple(d["rect"]) for d in page.get_drawings() if d["rect"][2] > d["rect"][0] or d["rect"][3] > d["rect"][1]
        ]


def process_solutions(pdf_path, year_label, exam, img_dir, web_prefix):
    """{num: {"text", "image"}} from an annotated-solutions PDF. Segmentation
    comes from extract_solutions.extract_document() so the text lines up with
    the <id>_solution[_N].png crops; the text goes through the same
    stem layout / LaTeX conversion as the questions."""
    doc = fitz.open(pdf_path)
    load_caches(doc)
    jobs, _ = extract_solution_document(doc)
    by_num = defaultdict(list)
    for job in jobs:
        by_num[job["num"]].append(job)
    out = {}
    for num, js in by_num.items():
        parts = []
        for k, job in enumerate(js):
            page = job["page"]
            ls = lines_in(LINE_CACHE[page.number], page.rect.width, None, job["top_limit"], job["bottom_limit"])
            vis = visuals_in(page, None, job["top_limit"], job["bottom_limit"])
            ls, used = merge_inline(ls, vis, page)
            vis = [v for v in vis if v not in used]
            if k == 0:
                ls = strip_number(list(ls), num, SOLUTION_QNUM_RE)
            ls, used_f = merge_fractions(ls, vis)
            ls = drop_figure_text(ls, figure_region(vis, used_f))
            t = layout_stem(ls, vis)
            if t:
                parts.append(t)
        base = f"{year_label}_{exam}_{num}"
        images = [f"{web_prefix}/{base}{job['suffix']}.png" for job in js if (img_dir / f"{base}{job['suffix']}.png").exists()]
        out[num] = {"text": "\n\n".join(parts) or None, "image": images or None}
    return out


def process_exam(pdf_path, year_label, year, exam, img_dir, web_prefix, warnings):
    doc = fitz.open(pdf_path)
    load_caches(doc)
    jobs, _ = extract_document(doc)
    key = parse_key(doc)
    pieces = defaultdict(list)
    for job in jobs:
        if job["suffix"] in ("_question", "_subtext"):
            continue
        page = job["page"]
        bx0, _, bx1, _ = job["bbox"]
        pieces[job["num"]].append((page, col_of(bx0, bx1, page.rect.width), job["top_limit"], job["bottom_limit"]))
    entries = []
    for num in sorted(pieces):
        if not 1 <= num <= 60:
            continue  # national-I 61-64 are survey questions, not chemistry
        e = build_entry(year_label, year, exam, num, pieces[num], key.get(num), img_dir, web_prefix)
        entries.append(e)
    missing = sorted(set(range(1, 61)) - {e["number"] for e in entries})
    if missing:
        warnings.append(f"{pdf_path}: questions not segmented: {missing}")
    sol_pdf = pdf_path.with_name(f"{year_label}-solutions.pdf")
    if sol_pdf.exists():
        sols = process_solutions(sol_pdf, year_label, exam, img_dir, web_prefix)
        for e in entries:
            if e["number"] in sols:
                e["solution"] = sols[e["number"]]
        gone = sorted(set(range(1, 61)) - set(sols))
        if gone:
            warnings.append(f"{sol_pdf}: no solution found for: {gone}")
    return entries


def review_flags(e):
    flags = []
    if e["answer"] is None:
        flags.append("no answer in key")
    elif "," in e["answer"]:
        flags.append(f"multiple answers {e['answer']}")
    if not e["question"]:
        flags.append("empty question text")
    if "?" in (e["question"] or "") and r"\,?\," in (e["question"] or ""):
        flags.append("undecodable symbol glyph")
    for v in e["choices"].values():
        if v and r"\,?\," in v:
            flags.append("undecodable symbol glyph in choice")
            break
    if not e["images"]["image"]:
        flags.append("no full image found")
    nn = sum(v is None for v in e["choices"].values())
    if 0 < nn < 4:
        flags.append("some choices missing")
    return flags


def to_js(entries):
    def val(v, ind):
        pad = "    " * ind
        if isinstance(v, dict):
            inner = ",\n".join(f"{pad}    {k}: {val(x, ind + 1)}" for k, x in v.items())
            return "{\n" + inner + f"\n{pad}}}"
        if isinstance(v, str) and ("\\" in v or "\n" in v) and "`" not in v and "${" not in v and not v.endswith("\\"):
            return "String.raw`%s`" % v  # LaTeX reads as written: single backslashes, real newlines
        return json.dumps(v, ensure_ascii=False)

    body = ",\n".join("    " + val(e, 1) for e in entries)
    return (
        "// Generated by scripts/build_questions.py -- schema in data/FORMAT.md.\n"
        "// LaTeX strings are String.raw template literals. Chemistry is mhchem (\\ce{...},\n"
        "// displayed equations \\[ \\ce{...} \\] or align*), other math $...$, tables \\[ array \\]\n"
        "// or center/tabular with a header row, statement lists enumerate.\n\n"
        "const questions = [\n" + body + "\n];\n\nexport default questions;\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exams-dir", default="exams")
    ap.add_argument("--images-dir", default="assets")
    ap.add_argument("--web-prefix", default="/assets")
    ap.add_argument("--out", default="data/questions.js")
    ap.add_argument("--overrides", default="data/question_overrides.json")
    ap.add_argument("--review-log", default="data/questions_review.md")
    ap.add_argument("--only", nargs="*", help="limit to exams like 2007_local (for testing)")
    args = ap.parse_args()

    exams_dir = Path(args.exams_dir)
    entries, warnings = [], []
    for exam in ("local", "national-I"):
        img_dir = Path(args.images_dir) / exam
        pdfs = sorted((exams_dir / exam).glob("*-exam*.pdf"), key=lambda p: p.stem)
        for pdf in pdfs:
            m = re.match(r"(\d{4})-exam(?:-(.+))?$", pdf.stem)
            year = int(m.group(1))
            year_label = f"{year}-{m.group(2)}" if m.group(2) else str(year)
            if args.only and f"{year_label}_{exam}" not in args.only:
                continue
            entries.extend(process_exam(pdf, year_label, year, exam, img_dir, f"{args.web_prefix}/{exam}", warnings))

    ov_path = Path(args.overrides)
    overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    by_id = {e["id"]: e for e in entries}
    for qid, fields in overrides.items():
        if qid.startswith("_"):
            continue
        if qid in by_id:
            for k, v in fields.items():
                if isinstance(v, dict) and isinstance(by_id[qid].get(k), dict):
                    by_id[qid][k].update(v)
                else:
                    by_id[qid][k] = v

    Path(args.out).write_text(to_js(entries))
    print(f"wrote {len(entries)} questions to {args.out}")

    review = []
    for e in entries:
        if e["id"] in overrides:
            continue
        f = review_flags(e)
        if f:
            review.append(f"- `{e['id']}`: {'; '.join(f)}")
    if args.review_log and not args.only:
        Path(args.review_log).write_text(
            "# questions.js review list\n\nGenerated by `scripts/build_questions.py`. Entries the converter could "
            "not fully resolve; fix them via `data/question_overrides.json` (entries there are dropped from "
            "this list).\n\n" + "\n".join(warnings + review) + "\n"
        )
    print(f"{len(review)} entries flagged for review; {len(warnings)} exam-level warnings")
    for w in warnings:
        print("WARN", w)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
r"""
Extract per-question screenshots from USNCO exam PDFs.

For each multiple-choice question found, produces:
  - "<year>_<exam>_<n>.png"          full question: stem + (A)-(D) choices
  - "<year>_<exam>_<n>_question.png" stem only, cropped before the choices

Layout model (derived from exams/local/2007-exam.pdf and
exams/national-I/2020-exam.pdf, refined against 2000-2002 local and
1999-2001 national-I):
  - Each page is 1 or 2 columns of questions, flowing top-to-bottom within
    each column, and columns flow in reading order across the whole
    document (page 1 col 1, page 1 col 2, page 2 col 1, ...).
  - A question starts at a text line matching `^\d{1,3}\.\s` (or, to
    recover the rare source typo of a missing period, digits directly
    followed by a capital letter) whose left edge sits at the column's
    left margin (continuation lines are indented further in).
  - A question's content (stem text, answer-choice text, and any images /
    vector-drawn figures such as graphs or molecule diagrams) all fall
    between its own start line and the next boundary event (the next
    question, or a shared-subtext block -- see below) in the same column.
  - The boundary between "question-only" and "full" is the first answer
    line matching `^\(A\)` in that segment.

Three known layout patterns get special handling:

1. Column/page-split questions (e.g. 2001 local Q30): the typesetter
   sometimes runs out of room at the bottom of a column and continues a
   question's answer choices at the top of the NEXT column (same page or
   the following one), ahead of what is otherwise that column's first
   question. This script does not try to auto-stitch these back together
   -- it saves each piece it can find as "<n>_1.png", "<n>_2.png", ... in
   reading order, plus a "<n>_question.png" cropped from piece 1 the same
   way a normal question's question-only crop is (stem up to the first
   "(A)" line, or all of piece 1 if it has no "(A)" line at all, since
   that means the whole answer block landed in piece 2), and logs the
   split so a human (or a later pass) can stitch the full pieces together.
   Detection: a question whose own column segment doesn't
   contain all four A-D choice markers is "pending"; if the very next
   column in the document starts with unclaimed ("orphan") content before
   its own first question/subtext marker, that orphan content is treated
   as the pending question's next piece.

2. Shared subtext (e.g. "Questions 32 and 33 should both be answered with
   reference to this system." followed by a shared equation/diagram,
   ahead of 2001 local Q32): detected from lines that open with
   "Question(s) N [and/,/-] M ..." or contain "...to answer question(s)
   N and M.". The shared block is cropped once and saved once per
   referenced question as "<n>_subtext.png" (so "<year>_<exam>_32_subtext
   .png" and "..._33_subtext.png" are identical copies of the same crop).

3. A question immediately before a shared-subtext block (e.g. 2001 local
   Q31): the subtext header is itself a boundary event like a question
   start, so a preceding question's segment stops at the subtext's own
   top edge rather than reaching into it.

This is a heuristic, not a PDF-aware parser of ACS's layout. Cases it
can't confidently resolve (an unexplained orphan block, a question still
missing choices after a second piece, a shared-subtext reference it
failed to parse, a duplicate question number) are appended to
data/edge_cases_log.md rather than silently guessed at -- treat that file
as a running worklist, not noise.
"""

import argparse
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

ROW_TOL = 8        # pt: snap a figure within this many points of a line to "same row"
CROP_PAD = 5        # pt: padding added around the final crop box (clamped to segment bounds)
ZOOM = 4.0          # render scale (~288 DPI)
HEADER_Y = 20       # pt: ignore lines above this (page edge)
FOOTER_MARGIN = 65  # pt: ignore lines within this many points of the bottom
MAX_SPLIT_PIECES = 2  # how many pieces we'll chase before giving up and logging it

QNUM_STRICT_RE = re.compile(r"^\s*(\d{1,3})\.(?:\s|$)")  # "N." + whitespace or end-of-line
QNUM_LOOSE_RE = re.compile(r"^\s*(\d{1,3})\s+(?=[A-Z])")  # digits + space + capital, no "."
# The loose pattern recovers a rarer source typo -- a missing period, e.g.
# 2003 local Q22 is literally typeset "22 How much heat is required" with
# no "." at all. It is deliberately NOT used on its own: a bare "digits,
# capital letter" line start also matches ordinary numeric data, e.g. the
# constants reference page's "1 F = 96,500 C.mol-1" ("1 Faraday..."),
# which would otherwise be mistaken for a lone "question 1" on a page that
# has no real questions at all. build_columns() only accepts a loose match
# when it is numerically adjacent (+-1) to an already-strict-matched
# question in the same column, i.e. it only fills a gap in an existing,
# trusted run of numbers -- never originates a column's events by itself.
ANS_A_RE = re.compile(r"^\s*\(A\)")  # anchors the question/full-crop cutoff: must START a line
ANS_RE = {
    c: re.compile(r"(?:\(" + c + r"\)|\b" + c + r"\))") for c in "ABCD"
}  # can appear anywhere in the line
# Accepts the normal "(D)" form, plus a bare "D)" at a word boundary as a
# fallback -- seen once as a source typo (2004 national-I Q18: "D) face-
# centered cubic" with no opening paren). The \b keeps this from matching
# inside an unrelated word ending in that letter, e.g. "NAD)" or "Pd)".
# (two choices are sometimes typeset side-by-side on one physical line, e.g.
# "(A) O2(g) (B) SO2(g)", so completeness checks search the whole line, not just its start)

# Shared-subtext detection: "Questions 32 and 33 should both be answered
# with reference to..." (anchored at a block's start) or "...to answer
# questions 56 and 57." (trailing, inside a longer sentence). Both are
# checked against a BLOCK's full joined text (not a single line) because
# the question-number list sometimes wraps across lines.
QSHARED_HEAD_RE = re.compile(r"^\s*(?:Questions?|Problems?)\s+(.+)", re.IGNORECASE)
QSHARED_TRAIL_RE = re.compile(
    r"answer\s+questions?\s+([\d,\s\-‐‑‒–—and]+\d)", re.IGNORECASE
)
STOPWORD_RE = re.compile(
    r"\b(should|refer|concern|use|using|require|are|is|both)\b", re.IGNORECASE
)
NUM_RE = re.compile(r"\d+")
RANGE_RE = re.compile(r"(\d+)\s*[-‐‑‒–—]\s*(\d+)")


def parse_number_list(head):
    range_m = RANGE_RE.search(head)
    if range_m:
        a, b = int(range_m.group(1)), int(range_m.group(2))
        if 0 < b - a <= 10:
            return list(range(a, b + 1))
    return [int(n) for n in NUM_RE.findall(head)]


def parse_shared_refs(block_text):
    """Return the list of question numbers a shared-subtext block refers
    to, or None if block_text doesn't look like one."""
    m = QSHARED_HEAD_RE.match(block_text)
    if m:
        rest = m.group(1)
        stop = STOPWORD_RE.search(rest)
        head = rest[: stop.start()] if stop else rest[:60]
        nums = parse_number_list(head)
        if nums:
            return nums
    m2 = QSHARED_TRAIL_RE.search(block_text)
    if m2:
        nums = parse_number_list(m2.group(1))
        if nums:
            return nums
    return None


def get_content_lines(page):
    page_h = page.rect.height
    lines = []
    d = page.get_text("dict")
    for bi, b in enumerate(d["blocks"]):
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            text = "".join(s["text"] for s in l["spans"])
            if not text.strip():
                continue
            x0, y0, x1, y1 = l["bbox"]
            if y0 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
                continue
            lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text, "block": bi})
    return lines


def get_content_images(page):
    page_h, page_w = page.rect.height, page.rect.width
    out = []
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if y1 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
            continue
        if x1 - x0 > 0.8 * page_w:
            continue  # page-spanning decoration (e.g. a banner), not question content
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return out


def get_content_drawings(page):
    page_h, page_w = page.rect.height, page.rect.width
    out = []
    for dr in page.get_drawings():
        x0, y0, x1, y1 = dr["rect"]
        if x1 <= x0 or y1 <= y0:
            continue
        if y1 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
            continue
        if x1 - x0 > 0.8 * page_w:
            continue  # page-spanning rule/divider line, not question content --
            # its bbox center can land exactly on the column midpoint and
            # blow up whichever column's bbox union includes it (found via
            # a 2001 local Q30 crop that mysteriously spanned both columns)
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return out


def detect_subtext_events(lines):
    """Group lines by their PDF text block and flag any block whose joined
    text declares a shared reference. Block-level (not line-level) because
    the question-number list sometimes wraps across lines."""
    by_block = defaultdict(list)
    for l in lines:
        by_block[l["block"]].append(l)
    events = []
    for blines in by_block.values():
        blines.sort(key=lambda l: l["y0"])
        joined = " ".join(l["text"].strip() for l in blines)
        refs = parse_shared_refs(joined)
        if refs:
            first = blines[0]
            events.append(
                {"type": "subtext", "y0": first["y0"], "x0": first["x0"], "x1": first["x1"], "refs": refs}
            )
    return events


def detect_columns(page_w):
    """Return a fixed left/right column split at the page midpoint.

    Earlier attempts derived the split dynamically (biggest gap in line
    x0, or in question-start-candidate x0), but both break in predictable
    ways: gaps from indented table/answer text can exceed the true column
    gap, and a question's own stem can contain a numbered sub-list (e.g.
    "1. NCl3  2. SO3  3. PCl5") whose x0's are themselves mistaken for
    question-start candidates, skewing the gap search. Every exam page
    sampled across 1999-2026 (local and national-I) uses the same ~612pt
    page with column 1 content maxing out well left of center and column 2
    starting well right of it, so a fixed midpoint is simpler and more
    robust than inferring the boundary from noisy content each time.
    """
    return [(0, page_w / 2), (page_w / 2, page_w)]


def assign_column(elems, col_ranges):
    def col_of(cx):
        for i, (lo, hi) in enumerate(col_ranges):
            if lo <= cx < hi:
                return i
        return len(col_ranges) - 1

    for e in elems:
        e["col"] = col_of((e["x0"] + e["x1"]) / 2)


def bbox_union(elems):
    if not elems:
        return None
    return (
        min(e["x0"] for e in elems),
        min(e["y0"] for e in elems),
        max(e["x1"] for e in elems),
        max(e["y1"] for e in elems),
    )


def true_top(ev, col_visuals):
    # A question's (or subtext block's) own figure sometimes renders a
    # couple of points ABOVE its anchor line (e.g. a small graph next to
    # the stem). Pull the boundary up to include it -- but only look a
    # short, fixed distance back, and only at images/drawings (never text,
    # which never precedes its own anchor in this layout).
    cand = [v["y0"] for v in col_visuals if ev["y0"] - ROW_TOL <= v["y0"] < ev["y0"]]
    return min([ev["y0"]] + cand)


def build_columns(page):
    """Return this page's columns as a list of dicts, each with its lines
    /images/drawings, its sorted boundary events (question-start and
    shared-subtext), and each event's content-aware top. Returns [] for
    pages with no real content (front matter, answer key)."""
    page_h, page_w = page.rect.height, page.rect.width
    lines = get_content_lines(page)
    if not lines:
        return []
    if re.search(r"\bKEY\b", page.get_text()):
        # Answer-key / scoring page (e.g. "1. C", "2. D", ...; titled "KEY",
        # "SCORING KEY", or "ANSWER KEY" depending on year) -- confirmed via
        # a full sweep of every downloaded exam that exactly one page per
        # exam matches this and it is always the key, never a real question.
        return []

    images = get_content_images(page)
    drawings = get_content_drawings(page)
    col_ranges = detect_columns(page_w)

    strict_candidates = [l for l in lines if QNUM_STRICT_RE.match(l["text"])]
    loose_candidates = [
        l
        for l in lines
        if QNUM_LOOSE_RE.match(l["text"]) and not QNUM_STRICT_RE.match(l["text"])
    ]
    subtext_candidates = detect_subtext_events(lines)

    assign_column(lines, col_ranges)
    assign_column(images, col_ranges)
    assign_column(drawings, col_ranges)
    assign_column(strict_candidates, col_ranges)
    assign_column(loose_candidates, col_ranges)
    assign_column(subtext_candidates, col_ranges)

    col_margin = {}
    for ci in range(len(col_ranges)):
        xs0 = [l["x0"] for l in strict_candidates if l["col"] == ci]
        if xs0:
            # Mode, not min: a single question number can carry extra
            # leading whitespace baked into its own text (seen in 2010
            # national-I Q5, " 5. For aqueous solutions..."), which shifts
            # its measured x0 several points left of where every other
            # question in the column actually sits. min() would anchor the
            # whole column's margin to that one outlier and silently fail
            # the real, consistently-indented questions (Q1-4 there) out
            # of the +10 tolerance below. The most common x0 is a better
            # estimate of the column's true margin than its single most
            # extreme value.
            counts = Counter(round(x) for x in xs0)
            col_margin[ci] = counts.most_common(1)[0][0]
        else:
            col_margin[ci] = col_ranges[ci][0]

    q_events = []
    for l in strict_candidates:
        m = QNUM_STRICT_RE.match(l["text"])
        if m and l["x0"] <= col_margin[l["col"]] + 10:
            q_events.append({"type": "question", "num": int(m.group(1)), "y0": l["y0"], "col": l["col"]})

    strict_nums_by_col = defaultdict(set)
    for e in q_events:
        strict_nums_by_col[e["col"]].add(e["num"])
    for l in loose_candidates:
        m = QNUM_LOOSE_RE.match(l["text"])
        if not m or l["x0"] > col_margin[l["col"]] + 10:
            continue
        num = int(m.group(1))
        if num - 1 in strict_nums_by_col[l["col"]] or num + 1 in strict_nums_by_col[l["col"]]:
            q_events.append({"type": "question", "num": num, "y0": l["y0"], "col": l["col"]})

    columns = []
    for ci in range(len(col_ranges)):
        col_lines = [l for l in lines if l["col"] == ci]
        col_images = [v for v in images if v["col"] == ci]
        col_drawings = [v for v in drawings if v["col"] == ci]
        events = [e for e in q_events if e["col"] == ci] + [e for e in subtext_candidates if e["col"] == ci]
        events.sort(key=lambda e: e["y0"])
        visuals = col_images + col_drawings
        tops = [true_top(e, visuals) for e in events]
        columns.append(
            {
                "page": page,
                "lines": col_lines,
                "images": col_images,
                "drawings": col_drawings,
                "events": events,
                "tops": tops,
                "bottom_limit": page_h - FOOTER_MARGIN,
            }
        )
    return columns


def extract_document(doc):
    """Walk every column of every page in reading order and return
    (jobs, edge_cases). Each job is {num, suffix, bbox, page, top_limit,
    bottom_limit} ready to render; edge_cases are human-readable strings
    for anything the heuristics couldn't confidently resolve."""
    all_columns = []
    for page in doc:
        all_columns.extend(build_columns(page))

    jobs = []
    edge_cases = []
    seen_complete = set()
    pending = None  # {"num", "piece_idx", "choices_found"}

    for colinfo in all_columns:
        page = colinfo["page"]
        lines, images, drawings = colinfo["lines"], colinfo["images"], colinfo["drawings"]
        events, tops = colinfo["events"], colinfo["tops"]
        col_bottom = colinfo["bottom_limit"]

        first_top = tops[0] if events else col_bottom
        orphan_lines = [l for l in lines if l["y0"] < first_top]
        orphan_visuals = [v for v in images + drawings if v["y0"] < first_top]
        orphan_elems = orphan_lines + orphan_visuals

        # Check the WHOLE page (not just this column) for the "DIRECTIONS"
        # heading: the instructions paragraph it introduces can spill from
        # column 1 into column 2's leading content without repeating the
        # heading word there.
        is_boilerplate = orphan_lines and "DIRECTIONS" in page.get_text().upper()

        if events and orphan_elems and is_boilerplate:
            pass  # the standard pre-Q1 "DIRECTIONS" instructions block -- expected, not an edge case

        elif events and orphan_elems:
            # Only meaningful when THIS column has its own real content to
            # anchor against. A column with zero events is front matter
            # (cover page, periodic table) -- "everything before the first
            # event" would otherwise be the whole column.
            if pending is not None and pending["piece_idx"] < MAX_SPLIT_PIECES:
                bbox = bbox_union(orphan_elems)
                pending["piece_idx"] += 1
                jobs.append(
                    {
                        "num": pending["num"],
                        "suffix": f"_{pending['piece_idx']}",
                        "bbox": bbox,
                        "page": page,
                        "top_limit": 0,
                        "bottom_limit": first_top,
                    }
                )
                pending["choices_found"] |= {
                    c for c, pat in ANS_RE.items() if any(pat.search(l["text"]) for l in orphan_lines)
                }
                edge_cases.append(
                    f"SPLIT RESOLVED: q{pending['num']} piece {pending['piece_idx']} found as leading "
                    f"content of the next column (page {page.number + 1}); saved as "
                    f"..._{pending['num']}_{pending['piece_idx']}.png -- stitch with the earlier piece(s)."
                )
                if {"A", "B", "C", "D"} <= pending["choices_found"]:
                    pending = None
                elif pending["piece_idx"] >= MAX_SPLIT_PIECES:
                    edge_cases.append(
                        f"STILL INCOMPLETE: q{pending['num']} is missing choice marker(s) "
                        f"{sorted({'A','B','C','D'} - pending['choices_found'])} even after "
                        f"{pending['piece_idx']} pieces -- gave up chasing further continuations; "
                        f"needs manual review."
                    )
                    pending = None
            elif events[0]["type"] == "question":
                # No pending split to resolve, but this column's first
                # question has its own leading content (e.g. a data table
                # typeset slightly above its own number, as in 2007 local
                # Q24) -- true_top() only pulls in images/drawings in that
                # gap, not text, specifically to avoid a previous question's
                # tail bleeding into the next one (see module docstring).
                # That risk doesn't apply here: this IS the column's first
                # event, so there is no previous question in this column to
                # protect against. Fold the content in rather than losing
                # it.
                tops[0] = min([tops[0]] + [e["y0"] for e in orphan_elems])
                sample = " / ".join(l["text"].strip()[:50] for l in orphan_lines[:2]) or "(figure only)"
                edge_cases.append(
                    f"LEADING CONTENT FOLDED IN: page {page.number + 1} q{events[0]['num']} had its own "
                    f"content (e.g. a table) rendered above its number line -- attached rather than "
                    f"dropped. Sample: {sample!r}."
                )
            else:
                sample = " / ".join(l["text"].strip()[:50] for l in orphan_lines[:3]) or "(figure only)"
                edge_cases.append(
                    f"UNEXPLAINED ORPHAN CONTENT: page {page.number + 1} has unclaimed content before "
                    f"its first question/subtext marker, but no question was pending a continuation "
                    f"(or the pending one already hit the piece cap). Sample: {sample!r}. Skipped -- "
                    f"not written to any file, needs manual review."
                )

        for i, ev in enumerate(events):
            top = tops[i]
            bottom = tops[i + 1] if i + 1 < len(events) else col_bottom

            def in_range(e, top=top, bottom=bottom):
                return top <= e["y0"] < bottom

            seg_lines = [l for l in lines if in_range(l)]
            seg_images = [v for v in images if in_range(v)]
            seg_drawings = [v for v in drawings if in_range(v)]
            full_elems = seg_lines + seg_images + seg_drawings

            if ev["type"] == "subtext":
                bbox = bbox_union(full_elems)
                if bbox:
                    for refnum in ev["refs"]:
                        jobs.append(
                            {
                                "num": refnum,
                                "suffix": "_subtext",
                                "bbox": bbox,
                                "page": page,
                                "top_limit": top,
                                "bottom_limit": bottom,
                            }
                        )
                else:
                    edge_cases.append(
                        f"EMPTY SUBTEXT: page {page.number + 1} parsed a shared-subtext header for "
                        f"questions {ev['refs']} but found no content in its own block -- skipped."
                    )
                continue

            if not seg_lines:
                continue
            num = ev["num"]

            if num in seen_complete:
                edge_cases.append(
                    f"DUPLICATE QUESTION NUMBER: q{num} matched again on page {page.number + 1} after "
                    f"already being completed earlier in this exam -- skipped the repeat, files from "
                    f"the first occurrence were kept."
                )
                continue

            ans_a = sorted((l for l in seg_lines if ANS_A_RE.match(l["text"])), key=lambda l: l["y0"])
            cutoff = ans_a[0]["y0"] - ROW_TOL if ans_a else None
            full_bbox = bbox_union(full_elems)
            q_bbox = None
            if cutoff is not None:
                q_elems = [e for e in full_elems if e["y0"] < cutoff]
                if q_elems:
                    q_bbox = bbox_union(q_elems)
            choices_found = {c for c, pat in ANS_RE.items() if any(pat.search(l["text"]) for l in seg_lines)}

            joined_text = " ".join(l["text"] for l in seg_lines).strip()
            if len(joined_text) < 60 and re.search(r"\b(question removed|removed|omitted)\b", joined_text, re.IGNORECASE):
                # ACS occasionally voids a question after administration
                # (seen: 2022 national-I "44. Question removed") -- there
                # genuinely are no A-D choices to find, so routing this
                # through the normal complete/split logic would misreport
                # it as a layout split. Save the label as-is and move on.
                if pending is not None:
                    edge_cases.append(
                        f"UNRESOLVED SPLIT: q{pending['num']} was missing choice marker(s) "
                        f"{sorted({'A','B','C','D'} - pending['choices_found'])} and no continuation "
                        f"ever appeared before q{num} (marked removed/omitted) started -- only piece 1 "
                        f"was saved; needs manual review."
                    )
                    pending = None
                seen_complete.add(num)
                jobs.append(
                    {"num": num, "suffix": "", "bbox": full_bbox, "page": page, "top_limit": top, "bottom_limit": bottom}
                )
                edge_cases.append(
                    f"QUESTION VOIDED BY ACS: q{num} is marked {joined_text!r} in the source PDF with no "
                    f"real question content -- saved as-is, not treated as a split."
                )
                continue

            if choices_found == {"A", "B", "C", "D"}:
                if pending is not None:
                    edge_cases.append(
                        f"UNRESOLVED SPLIT: q{pending['num']} was missing choice marker(s) "
                        f"{sorted({'A','B','C','D'} - pending['choices_found'])} and no continuation "
                        f"ever appeared before q{num} started -- only piece 1 was saved; needs manual "
                        f"review."
                    )
                    pending = None
                seen_complete.add(num)
                jobs.append(
                    {"num": num, "suffix": "", "bbox": full_bbox, "page": page, "top_limit": top, "bottom_limit": bottom}
                )
                if q_bbox:
                    jobs.append(
                        {
                            "num": num,
                            "suffix": "_question",
                            "bbox": q_bbox,
                            "page": page,
                            "top_limit": top,
                            "bottom_limit": cutoff,
                        }
                    )
                else:
                    edge_cases.append(
                        f"NO QUESTION-ONLY CUTOFF: q{num} has all four choices but no line starting "
                        f"with '(A)' was found to anchor the cutoff -- only the full crop was saved."
                    )
            else:
                if pending is not None:
                    edge_cases.append(
                        f"UNRESOLVED SPLIT (overwritten): q{pending['num']} never found a continuation "
                        f"before another incomplete question (q{num}) appeared -- only piece 1 of "
                        f"q{pending['num']} was saved; needs manual review."
                    )
                jobs.append(
                    {"num": num, "suffix": "_1", "bbox": full_bbox, "page": page, "top_limit": top, "bottom_limit": bottom}
                )
                # Piece 1 is where the stem lives, so a "_question" crop is
                # still meaningful even though the question is split --
                # every question number gets one, split or not. If piece 1
                # itself has no "(A)" line (the common case: the whole
                # choice block landed in piece 2), piece 1 already IS the
                # question-only content, so reuse its own bbox/bounds.
                jobs.append(
                    {
                        "num": num,
                        "suffix": "_question",
                        "bbox": q_bbox if q_bbox else full_bbox,
                        "page": page,
                        "top_limit": top,
                        "bottom_limit": cutoff if q_bbox else bottom,
                    }
                )
                pending = {"num": num, "piece_idx": 1, "choices_found": choices_found}
                edge_cases.append(
                    f"SPLIT DETECTED: q{num} is missing choice marker(s) "
                    f"{sorted({'A','B','C','D'} - choices_found)} in its own column (page "
                    f"{page.number + 1}) -- saved as piece 1 (..._{num}_1.png) and will look for the "
                    f"rest at the start of the next column."
                )

    if pending is not None:
        edge_cases.append(
            f"UNRESOLVED SPLIT (end of document): q{pending['num']} was still missing choice marker(s) "
            f"{sorted({'A','B','C','D'} - pending['choices_found'])} with no more columns left to check "
            f"-- only piece 1 was saved; needs manual review."
        )

    return jobs, edge_cases


def render_crop(page, bbox, top_limit=None, bottom_limit=None, zoom=ZOOM, pad=CROP_PAD):
    x0, y0, x1, y1 = bbox
    top, bottom = y0 - pad, y1 + pad
    if top_limit is not None:
        top = max(top, top_limit)
    if bottom_limit is not None:
        bottom = min(bottom, bottom_limit)
    x0 = max(0, x0 - pad)
    x1 = min(page.rect.width, x1 + pad)
    top = max(0, top)
    bottom = min(page.rect.height, bottom)
    rect = fitz.Rect(x0, top, x1, bottom)
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)


def process_pdf(pdf_path, year, exam_label, out_dir):
    doc = fitz.open(pdf_path)
    jobs, edge_cases = extract_document(doc)
    edge_cases = [f"{pdf_path.name}: {e}" for e in edge_cases]

    nums = set()
    for job in jobs:
        nums.add(job["num"])
        base = f"{year}_{exam_label}_{job['num']}{job['suffix']}"
        pix = render_crop(job["page"], job["bbox"], job["top_limit"], job["bottom_limit"])
        pix.save(out_dir / f"{base}.png")

    doc.close()
    return nums, edge_cases


def write_edge_case_log(log_path, run_label, edge_cases):
    if not edge_cases:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with open(log_path, "a") as f:
        if is_new:
            f.write("# Extraction edge-case log\n\n")
            f.write(
                "Append-only log of exam-layout patterns `scripts/extract_questions.py` could not "
                "confidently resolve on its own. Each run appends a timestamped section; entries are "
                "a worklist for manual review, not noise -- if a pattern recurs often enough, teach the "
                "script to handle it instead of re-reviewing it by hand each time.\n\n"
            )
        f.write(f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -- {run_label}\n\n")
        for e in edge_cases:
            f.write(f"- {e}\n")
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exams-dir", default="exams")
    ap.add_argument("--out-dir", default="assets/images_pilot")
    ap.add_argument("--log-file", default="data/edge_cases_log.md")
    ap.add_argument(
        "--local-years", nargs="*", type=int, default=[], help="years to process in exams/local"
    )
    ap.add_argument(
        "--national-i-years",
        nargs="*",
        type=int,
        default=[],
        help="years to process in exams/national-I",
    )
    args = ap.parse_args()

    exams_dir = Path(args.exams_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    all_edge_cases = []
    missing_files = []

    for year in args.local_years:
        path = exams_dir / "local" / f"{year}-exam.pdf"
        if path.exists():
            nums, edge_cases = process_pdf(path, year, "local", out_dir)
            summary.append((path.name, len(nums)))
            all_edge_cases.extend(edge_cases)
            continue
        # 2023 has no plain "2023-exam.pdf" -- ACS published two versions
        # ("2023-exam-original.pdf" and "2023-exam-new.pdf"). Process each
        # as its own labeled year so filenames stay unambiguous.
        variants = sorted((exams_dir / "local").glob(f"{year}-exam-*.pdf"))
        if not variants:
            missing_files.append(str(path))
            continue
        for vpath in variants:
            suffix = vpath.stem.removeprefix(f"{year}-exam-")
            year_label = f"{year}-{suffix}"
            nums, edge_cases = process_pdf(vpath, year_label, "local", out_dir)
            summary.append((vpath.name, len(nums)))
            all_edge_cases.extend(edge_cases)

    for year in args.national_i_years:
        path = exams_dir / "national-I" / f"{year}-exam.pdf"
        if not path.exists():
            missing_files.append(str(path))
            continue
        nums, edge_cases = process_pdf(path, year, "national-I", out_dir)
        summary.append((path.name, len(nums)))
        all_edge_cases.extend(edge_cases)

    print("=== Summary ===")
    for name, n in summary:
        print(f"{name}: {n} questions extracted")
    if missing_files:
        print("\n=== Missing files ===")
        for m in missing_files:
            print(m)

    print(f"\n=== Edge cases this run: {len(all_edge_cases)} ===")
    for e in all_edge_cases:
        print(e)

    run_label = f"local {args.local_years or '-'}, national-I {args.national_i_years or '-'} -> {out_dir}"
    write_edge_case_log(Path(args.log_file), run_label, all_edge_cases)
    if all_edge_cases:
        print(f"\nLogged to {args.log_file}")


if __name__ == "__main__":
    main()

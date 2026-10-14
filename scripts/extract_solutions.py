#!/usr/bin/env python3
r"""
Extract per-question screenshots from USNCO "Annotated Solution" PDFs.

These are a different document entirely from the exam PDFs that
extract_questions.py handles: a single-column, LaTeX-typeset writeup per
year (same "Annotated Solution ... Authors: ... Proofreader and Editor:
Dr. Qiang 'Steven' Chen ..." template across every year and both exam
types) that walks through the reasoning for each question in turn, with
no restated (A)-(D) choices to crop against -- just one explanation per
question number, flowing continuously across page breaks wherever they
happen to fall.

For each question found, produces:
  - "<year>_<exam>_<n>_solution.png"            if it fits on one page
  - "<year>_<exam>_<n>_solution_1.png", "..._2.png", ...  if the PDF
    itself breaks the explanation across two or more pages (reading
    order), so the pieces can be stitched back together.

Layout model (derived from exams/local/2021-solutions.pdf, cross-checked
against exams/local/{2010,2016}-solutions.pdf and
exams/national-I/1999-solutions.pdf):
  - Single column, A4-sized pages (595 x 842pt), no left/right split --
    unlike the exam PDFs, there's no column-detection step here at all.
  - A question starts at a text line matching `^\d{1,3}\.\s` (or just
    "N." alone, see QNUM_RE) at the page's left margin. The margin is
    computed PER PAGE (modal x0 among every such candidate line on that
    page; a page with none inherits the previous page's), since some
    years use a LaTeX "twoside" layout that mirrors margins across
    even/odd pages for book binding.
  - A question's content is everything between its own start line and
    the next question's start line, in reading order across the WHOLE
    document -- i.e. pages are just one long column that happens to be
    cut into physical sheets. A segment that crosses a page boundary is
    rendered as one cropped image per page it touches, in order.
  - Every crop's horizontal span runs from the document's left edge
    (where the question number begins, the smallest per-page margin
    anywhere in the PDF) to its right edge (the furthest any content
    reaches right anywhere in the PDF) -- a single pair of bounds
    computed once per document, so every crop from that year shares the
    same horizontal frame.

This is a heuristic, not a PDF-aware parser. Cases it can't confidently
resolve get appended to data/edge_cases_log.md, same convention as
extract_questions.py.
"""

import argparse
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

ROW_TOL = 8        # pt: snap a figure within this many points of a line to "same row"
CROP_PAD = 6        # pt: padding added around the final crop box (clamped to segment bounds)
ZOOM = 3.0          # render scale (~216 DPI) -- these pages are text-dense; still crisp, smaller files
HEADER_Y = 20       # pt: ignore lines above this (page edge)
FOOTER_MARGIN = 70  # pt: ignore lines within this many points of the bottom (page-number footer sits ~777 on an 842pt-tall page)

QNUM_RE = re.compile(r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,3})\s*[.:](?:\s|$)")
# Three templates seen across years (same volunteer-authored "Annotated
# Solution" series, different individual authors): plain "1.", "Q1.",
# and "Question 1:". All three anchor on digits immediately followed by
# "." or ":", so folding them into one pattern is safe -- a stray "Q" or
# "Question" elsewhere (e.g. "Q = mcdeltaT") never has a bare "." or ":"
# right after a number in that position.


def get_content_lines(page):
    page_h = page.rect.height
    lines = []
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            text = "".join(s["text"] for s in l["spans"])
            if not text.strip():
                continue
            x0, y0, x1, y1 = l["bbox"]
            if y0 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
                continue
            lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text})
    return lines


def get_content_images(page):
    page_h, page_w = page.rect.height, page.rect.width
    out = []
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if y1 < HEADER_Y or y0 > page_h - FOOTER_MARGIN:
            continue
        if x1 - x0 > 0.9 * page_w:
            continue  # page-spanning decoration, not solution content
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
        if x1 - x0 > 0.9 * page_w:
            continue
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return out


def bbox_union(elems):
    if not elems:
        return None
    return (
        min(e["x0"] for e in elems),
        min(e["y0"] for e in elems),
        max(e["x1"] for e in elems),
        max(e["y1"] for e in elems),
    )


def true_top(y0, visuals):
    # Mirrors extract_questions.py's true_top: a question's own diagram
    # can render a couple of points above its number line. Pull the top
    # boundary up to include it, but only look a short, fixed distance.
    cand = [v["y0"] for v in visuals if y0 - ROW_TOL <= v["y0"] < y0]
    return min([y0] + cand)


def build_document(doc):
    """Return (page_data, flat_events, doc_left_edge, doc_right_edge) for
    the whole PDF. page_data[i] has i's lines/images/drawings; flat_events
    is every question-start found, in reading order, as {page_idx, num,
    y0}; doc_left_edge/doc_right_edge are the document-wide crop bounds --
    the smallest left margin and the furthest any content extends right,
    anywhere in the document."""
    page_data = []
    for page in doc:
        lines = get_content_lines(page)
        images = get_content_images(page)
        drawings = get_content_drawings(page)
        page_data.append({"page": page, "lines": lines, "images": images, "drawings": drawings})

    # Margin computed PER PAGE, not once for the whole document: some years
    # use a LaTeX "twoside" layout where even/odd pages mirror their
    # margins for book binding (seen in local 2022 and national-I 2021/22
    # -- e.g. x0=28 on even pages, x0=142 on odd pages). A single global
    # margin would reject every candidate on whichever parity is the
    # minority, silently dropping half the questions. A page with no
    # candidates of its own (a pure continuation page) inherits the most
    # recent page's margin, since that's the layout it's visually part of.
    last_margin = 0
    page_margin = []
    for pd in page_data:
        xs0 = [l["x0"] for l in pd["lines"] if QNUM_RE.match(l["text"])]
        if xs0:
            last_margin = Counter(round(x) for x in xs0).most_common(1)[0][0]
        page_margin.append(last_margin)

    flat_events = []
    for pi, pd in enumerate(page_data):
        margin = page_margin[pi]
        evs = []
        for l in pd["lines"]:
            m = QNUM_RE.match(l["text"])
            if m and l["x0"] <= margin + 10:
                evs.append({"page_idx": pi, "num": int(m.group(1)), "y0": l["y0"]})
        evs.sort(key=lambda e: e["y0"])
        flat_events.extend(evs)

    # Right edge: the single x1 that the rightmost-extending piece of
    # content (text, figure, or drawing) reaches anywhere in the document --
    # one fixed reference per PDF (i.e. per year's solutions), so every
    # crop from that year shares the same right boundary rather than each
    # hugging its own content.
    doc_right_edge = max(
        (e["x1"] for pd in page_data for e in pd["lines"] + pd["images"] + pd["drawings"]),
        default=None,
    )

    # Left edge for CROPPING (as opposed to page_margin, which stays
    # per-page for question-start detection): the smallest margin seen
    # anywhere in the document. Twoside layouts mirror the margin across
    # even/odd pages (e.g. x0=28 vs x0=142), so a single per-document value
    # has to be the minimum of the two -- using the larger one would clip
    # real content on whichever pages use the smaller margin. This gives
    # every crop in the document the same left edge too.
    doc_left_edge = min(page_margin) if page_margin else 0

    return page_data, flat_events, doc_left_edge, doc_right_edge


def filter_sequential(events, edge_cases):
    """Keep only candidates whose number is >= the next expected one. A
    numbered sub-list inside an explanation (e.g. "There are two ways:
    1. ... 2. ...") can sit at the exact same left margin as a real
    question marker and is otherwise indistinguishable from one -- but it
    always restarts from a small number, which by the time it appears is
    always LESS than the real question number already in progress, so
    rejecting anything that doesn't move the sequence forward catches it
    without being thrown off by a genuinely missing/skipped question (ACS
    does occasionally void one -- e.g. 2022 national-I Q44 -- and its
    solution write-up is then simply absent, jumping straight to N+1;
    requiring an EXACT match on "expected" would wrongly reject every
    single real question after a gap like that, forever)."""
    filtered = []
    expected = 1
    for ev in events:
        if ev["num"] >= expected:
            if ev["num"] > expected:
                edge_cases.append(
                    f"GAP IN SEQUENCE: expected q{expected} next but found q{ev['num']} on page "
                    f"{ev['page_idx'] + 1} -- treated as a legitimately skipped/voided question "
                    f"(e.g. ACS removed it) rather than a detection miss; nothing saved for "
                    f"q{expected}..q{ev['num'] - 1}."
                )
            filtered.append(ev)
            expected = ev["num"] + 1
        else:
            edge_cases.append(
                f"SKIPPED NON-SEQUENTIAL MATCH: a line matching the question-number pattern with "
                f"num={ev['num']} on page {ev['page_idx'] + 1} is behind the current sequence "
                f"(expected q{expected} or higher) -- treated as a numbered sub-list inside an "
                f"explanation, not a new question, and excluded from segmentation."
            )
    return filtered


def extract_document(doc):
    """Return (jobs, edge_cases). Each job is {num, suffix, bbox, page,
    top_limit, bottom_limit, left, right} ready to render."""
    page_data, raw_events, doc_left_edge, doc_right_edge = build_document(doc)
    jobs = []
    edge_cases = []
    events = filter_sequential(raw_events, edge_cases)
    seen = set()

    for i, ev in enumerate(events):
        num = ev["num"]
        if num in seen:
            edge_cases.append(
                f"DUPLICATE QUESTION NUMBER: q{num} matched again on page {ev['page_idx'] + 1} after "
                f"already being completed earlier -- skipped the repeat, the first occurrence's files "
                f"were kept."
            )
            continue
        seen.add(num)

        start_page_idx, start_y0 = ev["page_idx"], ev["y0"]
        if i + 1 < len(events):
            end_page_idx, end_y0 = events[i + 1]["page_idx"], events[i + 1]["y0"]
        else:
            end_page_idx, end_y0 = len(page_data) - 1, None  # run to the bottom of the last page

        pieces = []  # list of (page_idx, top, bottom)
        if start_page_idx == end_page_idx:
            pd = page_data[start_page_idx]
            visuals = pd["images"] + pd["drawings"]
            top = true_top(start_y0, visuals)
            bottom = end_y0 if end_y0 is not None else pd["page"].rect.height - FOOTER_MARGIN
            pieces.append((start_page_idx, top, bottom))
        else:
            pd0 = page_data[start_page_idx]
            visuals0 = pd0["images"] + pd0["drawings"]
            top0 = true_top(start_y0, visuals0)
            pieces.append((start_page_idx, top0, pd0["page"].rect.height - FOOTER_MARGIN))
            for mid in range(start_page_idx + 1, end_page_idx):
                pieces.append((mid, HEADER_Y, page_data[mid]["page"].rect.height - FOOTER_MARGIN))
            bottom_last = end_y0 if end_y0 is not None else page_data[end_page_idx]["page"].rect.height - FOOTER_MARGIN
            pieces.append((end_page_idx, HEADER_Y, bottom_last))

        # Resolve content BEFORE deciding single-vs-multi naming: a question
        # whose next sibling starts on the following page doesn't
        # necessarily mean its own content actually spills onto that page
        # -- the page break can just happen to fall cleanly between two
        # questions, leaving the "next page" piece empty. Only call it a
        # real cross-page split once we know at least two pieces are
        # genuinely non-empty; a single surviving piece is just a normal
        # one-page solution.
        resolved = []  # list of (page_idx, bbox)
        for page_idx, top, bottom in pieces:
            pd = page_data[page_idx]

            def in_range(e, top=top, bottom=bottom):
                return top <= e["y0"] < bottom

            elems = [e for e in pd["lines"] + pd["images"] + pd["drawings"] if in_range(e)]
            bbox = bbox_union(elems)
            if bbox is not None:
                resolved.append((page_idx, top, bottom, bbox))

        if not resolved:
            edge_cases.append(
                f"EMPTY SOLUTION: q{num} (page {start_page_idx + 1}) had no content in its expected "
                f"range -- skipped, not written to any file."
            )
            continue

        multi = len(resolved) > 1
        if multi:
            edge_cases.append(
                f"SPLIT ACROSS PAGES: q{num}'s solution runs from page {resolved[0][0] + 1} to page "
                f"{resolved[-1][0] + 1} -- saved as {len(resolved)} indexed piece(s) "
                f"(..._{num}_solution_1.png .. _{len(resolved)}.png), in reading order."
            )

        for idx, (page_idx, top, bottom, bbox) in enumerate(resolved, start=1):
            pd = page_data[page_idx]
            suffix = f"_solution_{idx}" if multi else "_solution"
            jobs.append(
                {
                    "num": num,
                    "suffix": suffix,
                    "bbox": bbox,
                    "page": pd["page"],
                    "top_limit": top,
                    "bottom_limit": bottom,
                    "left": doc_left_edge,
                    "right": doc_right_edge,
                }
            )

    return jobs, edge_cases


def render_crop(page, bbox, left, right, top_limit=None, bottom_limit=None, zoom=ZOOM, pad=CROP_PAD):
    # Horizontal span runs from the page's left margin (where the index
    # number begins) to the document's right edge -- the furthest any
    # content reaches right ANYWHERE in that year's PDF, computed once per
    # document so every crop from that year shares the same horizontal
    # frame instead of each hugging its own content width. Only the
    # vertical span stays content-based (per crop).
    _, y0, _, y1 = bbox
    top, bottom = y0 - pad, y1 + pad
    if top_limit is not None:
        top = max(top, top_limit)
    if bottom_limit is not None:
        bottom = min(bottom, bottom_limit)
    top = max(0, top)
    bottom = min(page.rect.height, bottom)
    x0 = max(0, left)
    x1 = min(page.rect.width, right if right is not None else page.rect.width)
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
        pix = render_crop(
            job["page"], job["bbox"], job["left"], job["right"], job["top_limit"], job["bottom_limit"]
        )
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
                "Append-only log of exam-layout patterns the extraction scripts could not confidently "
                "resolve on their own. Each run appends a timestamped section; entries are a worklist "
                "for manual review, not noise -- if a pattern recurs often enough, teach the script to "
                "handle it instead of re-reviewing it by hand each time.\n\n"
            )
        f.write(f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -- {run_label}\n\n")
        for e in edge_cases:
            f.write(f"- {e}\n")
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exams-dir", default="exams")
    ap.add_argument("--out-dir", default="assets/images_solutions")
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
    skipped_no_solutions = []

    def run(year, subdir, exam_label):
        path = exams_dir / subdir / f"{year}-solutions.pdf"
        if not path.exists():
            skipped_no_solutions.append(str(path))
            return
        nums, edge_cases = process_pdf(path, year, exam_label, out_dir)
        summary.append((path.name, len(nums)))
        all_edge_cases.extend(edge_cases)

    for year in args.local_years:
        run(year, "local", "local")
    for year in args.national_i_years:
        run(year, "national-I", "national-I")

    print("=== Summary ===")
    for name, n in summary:
        print(f"{name}: {n} solutions extracted")
    if skipped_no_solutions:
        print(f"\n=== No solutions PDF (skipped, {len(skipped_no_solutions)}) ===")
        for s in skipped_no_solutions:
            print(s)

    print(f"\n=== Edge cases this run: {len(all_edge_cases)} ===")
    for e in all_edge_cases:
        print(e)

    run_label = (
        f"[solutions] local {args.local_years or '-'}, national-I {args.national_i_years or '-'} "
        f"-> {out_dir}"
    )
    write_edge_case_log(Path(args.log_file), run_label, all_edge_cases)
    if all_edge_cases:
        print(f"\nLogged to {args.log_file}")


if __name__ == "__main__":
    main()

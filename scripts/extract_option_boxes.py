#!/usr/bin/env python3
r"""
Locate the (A)-(D) answer choices inside each extracted question image and
write their click boxes to data/option_boxes.js for the web app.

Re-runs extract_questions.extract_document() on each exam PDF to recover the
exact crop rectangle every "<year>_<exam>_<n>[_k].png" was rendered from,
then finds the "(A)".."(D)" markers inside that rectangle with PyMuPDF text
search. Working in PDF space (instead of OCR on the PNG) keeps positions
exact regardless of layout:

  - one column of four rows      (A) / (B) / (C) / (D)
  - a 2 x 2 grid                 (A) (B) / (C) (D)
  - one row of four              (A) (B) (C) (D)
  - choices split across pieces  ..._30_1.png has (A)(B), ..._30_2.png (C)(D)

Markers are grouped into rows (by y) and columns (by order within a row).
Every box in an image gets the same width so the overlay reads as a
symmetric grid: the narrowest column pitch (or, for a single column, the
widest choice's text extent). Boxes in a row share one height: the pitch to
the next row, or for the last row the content running contiguously below
its markers (so wrapped choices are covered but a trailing "END OF TEST"
banner is not).

Boxes are stored as fractions of the image's width/height so the page can
scale them with the image. A crop is skipped (no boxes; the app falls back
to plain A-D buttons) if its rebuilt size doesn't match the PNG on disk --
i.e. the image was edited by hand after extraction -- or if fewer than one
marker is found.

Usage:
    python3.13 scripts/extract_option_boxes.py            # every exam in exams/
    python3.13 scripts/extract_option_boxes.py --check    # also print a per-layout summary
"""

import argparse
import json
import struct
import sys
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_questions as eq  # noqa: E402

ROW_TOL = 4       # pt: markers whose tops are within this are on the same row
GAP = 3           # pt: breathing room kept between neighbouring boxes
PAD_LEFT = 2      # pt: start the box just left of the "(" of the marker
SIZE_TOL = 3      # px: allowed difference between rebuilt crop and PNG size
LETTERS = "ABCD"


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def crop_rect(page, job, pad=eq.CROP_PAD):
    """Mirror of extract_questions.render_crop's clip computation."""
    x0, y0, x1, y1 = job["bbox"]
    top, bottom = y0 - pad, y1 + pad
    if job["top_limit"] is not None:
        top = max(top, job["top_limit"])
    if job["bottom_limit"] is not None:
        bottom = min(bottom, job["bottom_limit"])
    x0 = max(0, x0 - pad)
    x1 = min(page.rect.width, x1 + pad)
    return fitz.Rect(x0, max(0, top), x1, min(page.rect.height, bottom))


def find_markers(page, rect):
    """Return the best run of consecutive choice markers in reading order,
    as [(letter, fitz.Rect)]."""
    hits = []
    for letter in LETTERS:
        for r in page.search_for(f"({letter})", clip=rect):
            # search_for ignores case; keep only the upper-case marker
            if f"({letter})" in page.get_textbox(r + (-1, -1, 1, 1)):
                hits.append((letter, r))
    hits.sort(key=lambda h: (round(h[1].y0 / ROW_TOL), h[1].x0))

    best = []
    for i, (letter, r) in enumerate(hits):
        run, want = [(letter, r)], LETTERS.index(letter) + 1
        for l2, r2 in hits[i + 1:]:
            if want < 4 and l2 == LETTERS[want]:
                run.append((l2, r2))
                want += 1
        # longer runs win; on ties prefer the later start (choices sit below the stem)
        if len(run) >= len(best):
            best = run
    return best


def group_rows(markers):
    rows = []
    for m in sorted(markers, key=lambda m: m[1].y0):
        if rows and abs(m[1].y0 - rows[-1][0][1].y0) <= ROW_TOL:
            rows[-1].append(m)
        else:
            rows.append([m])
    return [sorted(r, key=lambda m: m[1].x0) for r in rows]


CONTIG_GAP = 6    # pt: a vertical gap larger than this ends the last row's content


def content_extent(page, rect, top, until=None):
    """Bottom of the contiguous run of everything drawn inside rect below
    `top`, and the right edge of everything from `top` down to `until`
    (default: that contiguous bottom)."""
    clip = fitz.Rect(rect.x0, top, rect.x1, rect.y1)
    elems = []
    for b in page.get_text("dict", clip=clip)["blocks"]:
        for l in b.get("lines", []):
            if "".join(s["text"] for s in l["spans"]).strip():
                elems.append(fitz.Rect(l["bbox"]) & clip)
    elems += [fitz.Rect(im["bbox"]) & clip for im in page.get_image_info()]
    elems += [fitz.Rect(d["rect"]) & clip for d in page.get_drawings()]
    elems = [e for e in elems if not e.is_empty and e.y0 >= top - 1]
    if not elems:
        return rect.x1, rect.y1
    bottom = top
    for e in sorted(elems, key=lambda e: e.y0):
        if e.y0 > bottom + CONTIG_GAP:
            break
        bottom = max(bottom, e.y1)
    kept = [e for e in elems if e.y0 <= (until if until is not None else bottom)]
    return max(e.x1 for e in kept), bottom


def option_boxes(page, rect):
    markers = find_markers(page, rect)
    if not markers:
        return None, "none"
    rows = group_rows(markers)
    ncols = max(len(r) for r in rows)
    layout = f"{len(rows)}x{ncols}"

    row_tops = [r[0][1].y0 for r in rows]
    _, last_bottom = content_extent(page, rect, row_tops[-1] - 1)
    right, _ = content_extent(page, rect, row_tops[0] - 1, until=last_bottom)

    # width: narrowest pitch between neighbouring markers on the same row,
    # otherwise from the marker to the right edge of the choice text
    pitches = [b[1].x0 - a[1].x0 for r in rows for a, b in zip(r, r[1:])]
    if pitches:
        width = min(pitches) - GAP
    else:
        width = right - min(m[1].x0 for m in markers) + PAD_LEFT

    # height per row: pitch to the next row, or contiguous content for the last
    heights = [b - a - GAP for a, b in zip(row_tops, row_tops[1:])]
    heights.append(last_bottom - row_tops[-1] + 4)

    boxes = []
    for r, height in zip(rows, heights):
        for letter, m in r:
            x0 = m.x0 - PAD_LEFT
            y0 = m.y0 - 2
            box = fitz.Rect(x0, y0, x0 + width, y0 + height) & rect
            boxes.append({
                "letter": letter,
                "x": round((box.x0 - rect.x0) / rect.width, 4),
                "y": round((box.y0 - rect.y0) / rect.height, 4),
                "w": round(box.width / rect.width, 4),
                "h": round(box.height / rect.height, 4),
            })
    return boxes, layout


def process_pdf(pdf_path, label, exam_type, images_dir, out, stats):
    doc = fitz.open(pdf_path)
    jobs, _ = eq.extract_document(doc)
    for job in jobs:
        if job["suffix"] in ("_question", "_subtext"):
            continue
        name = f"{label}_{exam_type}_{job['num']}{job['suffix']}.png"
        png = images_dir / exam_type / name
        if not png.exists():
            stats["missing_png"] += 1
            continue
        rect = crop_rect(job["page"], job)
        pw, ph = png_size(png)
        if abs(pw - rect.width * eq.ZOOM) > SIZE_TOL or abs(ph - rect.height * eq.ZOOM) > SIZE_TOL:
            stats["size_mismatch"] += 1
            continue
        boxes, layout = option_boxes(job["page"], rect)
        stats[f"layout {layout}"] += 1
        if boxes:
            out[f"/assets/{exam_type}/{name}"] = boxes
    doc.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exams-dir", default="exams")
    ap.add_argument("--images-dir", default="assets")
    ap.add_argument("--out", default="data/option_boxes.js")
    ap.add_argument("--check", action="store_true", help="print a summary of layouts found")
    args = ap.parse_args()

    exams_dir, images_dir = Path(args.exams_dir), Path(args.images_dir)
    out, stats = {}, Counter()
    for exam_type in ("local", "national-I"):
        for pdf in sorted((exams_dir / exam_type).glob("*-exam*.pdf")):
            # "2023-exam-new.pdf" -> label "2023-new"; "2021-exam.pdf" -> "2021"
            year, _, variant = pdf.stem.partition("-exam")
            label = year + variant
            process_pdf(pdf, label, exam_type, images_dir, out, stats)
            print(f"{pdf}: done", file=sys.stderr)

    Path(args.out).write_text(
        "// Generated by scripts/extract_option_boxes.py -- click boxes for the (A)-(D)\n"
        "// choices in each question image, as fractions of the image's width/height.\n\n"
        "const optionBoxes = " + json.dumps(out, separators=(",", ":")) + ";\n\n"
        "export default optionBoxes;\n"
    )
    print(f"wrote {len(out)} images to {args.out}")
    if args.check:
        for k, v in sorted(stats.items()):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

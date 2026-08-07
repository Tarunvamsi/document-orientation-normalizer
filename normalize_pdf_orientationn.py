#!/usr/bin/env python3
"""
normalize_pdf_orientation.py

Rotates PDF pages using one of two different strategies, selected via
--mode:

  --mode shape (default)
      Forces every page to the SAME SHAPE (all portrait, or all
      landscape) based on page dimensions. Pages that don't match are
      rotated to match - text content rotates along with the page,
      which means a wide table page rotated into portrait will end up
      with sideways text. This mode prioritizes uniform page shape over
      keeping every page's text readable.

  --mode text
      Only rotates a page if its TEXT is actually misaligned (upside
      down or sideways). Reads the exact text direction straight from
      the PDF's content stream (no OCR, no image rendering, no
      tesseract needed) and rotates only what's genuinely wrong. A wide
      table page whose text already reads correctly is left alone, even
      though its page shape differs from the rest of the document. This
      mode prioritizes text readability over shape uniformity, and only
      works on pages with a real extractable text layer (not scanned
      images with no text).

In both modes, rotation is permanently baked into the page's content
stream and MediaBox (via transfer_rotation_to_content()) rather than
left as a soft /Rotate flag, so the result displays correctly in any
viewer or tool - not just ones that respect that flag.

Requires:
    pip install pypdf pymupdf

Usage:
    # Force uniform page shape (majority orientation wins)
    python3 normalize_pdf_orientation.py input.pdf output.pdf --mode shape

    # Force uniform page shape, always portrait
    python3 normalize_pdf_orientation.py input.pdf output.pdf --mode shape --target portrait

    # Only fix genuinely misaligned text, leave shape alone otherwise
    python3 normalize_pdf_orientation.py input.pdf output.pdf --mode text
"""

import argparse
import math
import sys
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Shared: bake rotation permanently into the page (not just a /Rotate flag)
# ---------------------------------------------------------------------------

def apply_rotation(page, delta):
    """Rotate a page by delta degrees and bake it permanently into the
    content stream / MediaBox, rather than leaving it as a /Rotate flag
    that some viewers/tools might ignore."""
    page.rotate(delta)
    page.transfer_rotation_to_content()


# ---------------------------------------------------------------------------
# Mode: shape - force uniform page dimensions
# ---------------------------------------------------------------------------

def effective_size(page):
    """(width, height) of a page AS DISPLAYED, after its current
    /Rotate value is applied."""
    box = page.mediabox
    width, height = float(box.width), float(box.height)
    if page.rotation % 360 in (90, 270):
        width, height = height, width
    return width, height


def analyze_shapes(reader):
    info = []
    for i, page in enumerate(reader.pages):
        width, height = effective_size(page)
        info.append({
            "index": i,
            "orientation": "landscape" if width > height else "portrait",
            "current_rotation": page.rotation % 360,
        })
    return info


def decide_shape_target(info, target_arg):
    if target_arg in ("portrait", "landscape"):
        return target_arg
    portrait_count = sum(1 for p in info if p["orientation"] == "portrait")
    return "portrait" if portrait_count >= len(info) - portrait_count else "landscape"


def shape_rotation_delta(current_rotation):
    """Pick +90 or -90 to flip orientation, preferring to undo an
    existing 90/270 rotation rather than piling on and landing upside
    down at 180."""
    if current_rotation == 90:
        return -90
    elif current_rotation == 270:
        return 90
    else:
        return 90


def normalize_by_shape(input_path, output_path, target_arg="auto", verbose=True):
    reader = PdfReader(input_path)
    info = analyze_shapes(reader)
    if not info:
        raise ValueError("PDF has no pages.")

    target = decide_shape_target(info, target_arg)

    if verbose:
        print(f"Mode: shape (force uniform page dimensions)")
        print(f"Total pages: {len(info)}")
        counts = {}
        for p in info:
            counts[p["orientation"]] = counts.get(p["orientation"], 0) + 1
        for orient, count in counts.items():
            print(f"  {orient}: {count} page(s)")
        print(f"Target orientation: {target}\n")

    writer = PdfWriter()
    rotated_pages = []

    for p, page in zip(info, reader.pages):
        if p["orientation"] != target:
            delta = shape_rotation_delta(p["current_rotation"])
            apply_rotation(page, delta)
            rotated_pages.append((p["index"] + 1, delta))
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    if verbose:
        if rotated_pages:
            print(f"Rotated {len(rotated_pages)} page(s):")
            for pg, delta in rotated_pages:
                print(f"  Page {pg}: rotated {delta:+d}°")
        else:
            print("No pages needed rotation; document was already uniform.")
        print(f"\nSaved: {output_path}")

    return {"mode": "shape", "target": target, "rotated_pages": rotated_pages,
            "total_pages": len(info)}


# ---------------------------------------------------------------------------
# Mode: text - only fix genuinely misaligned text
# ---------------------------------------------------------------------------

def get_dominant_text_angle(fitz_page, min_chars=1):
    """Character-count-weighted dominant text direction angle (degrees,
    0-360) in the page's raw content-stream space. None if no
    extractable text (e.g. a scanned image with no text layer)."""
    text_dict = fitz_page.get_text("dict")
    weighted_vectors = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            char_count = sum(len(span.get("text", "")) for span in line.get("spans", []))
            if char_count >= min_chars:
                weighted_vectors.append((dx, dy, char_count))

    if not weighted_vectors:
        return None

    sum_x = sum(dx * w for dx, dy, w in weighted_vectors)
    sum_y = sum(dy * w for dx, dy, w in weighted_vectors)
    return math.degrees(math.atan2(sum_y, sum_x)) % 360


def text_rotation_delta(raw_text_angle, current_page_rotation):
    """Rotation (nearest 90) needed so the text displays upright,
    combining the text's raw angle with whatever rotation the page
    already has applied."""
    effective_angle = (raw_text_angle + current_page_rotation) % 360
    delta = round(-effective_angle / 90) * 90 % 360
    if delta > 180:
        delta -= 360
    return delta


def normalize_by_text(input_path, output_path, min_chars=1, verbose=True):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    fitz_doc = fitz.open(input_path)

    if verbose:
        print("Mode: text (only fix genuinely misaligned text)")

    rotated_pages = []
    skipped_no_text = []

    for i, page in enumerate(reader.pages):
        raw_angle = get_dominant_text_angle(fitz_doc[i], min_chars)

        if raw_angle is None:
            skipped_no_text.append(i + 1)
            writer.add_page(page)
            continue

        delta = text_rotation_delta(raw_angle, page.rotation % 360)
        if delta != 0:
            apply_rotation(page, delta)
            rotated_pages.append((i + 1, delta))
        writer.add_page(page)

    fitz_doc.close()

    with open(output_path, "wb") as f:
        writer.write(f)

    if verbose:
        print(f"Total pages: {len(reader.pages)}")
        if rotated_pages:
            print(f"Rotated {len(rotated_pages)} page(s) based on exact text angle:")
            for pg, delta in rotated_pages:
                print(f"  Page {pg}: rotated {delta:+d}°")
        else:
            print("No pages needed rotation.")
        if skipped_no_text:
            print(f"\nSkipped {len(skipped_no_text)} page(s) - no extractable text layer "
                  f"found, left unchanged: {skipped_no_text}")
        print(f"\nSaved: {output_path}")

    return {"mode": "text", "rotated_pages": rotated_pages,
            "skipped_pages": skipped_no_text, "total_pages": len(reader.pages)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Rotate PDF pages either to force uniform page shape (--mode shape) "
                    "or to fix only genuinely misaligned text (--mode text)."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("output", help="Path to save the result")
    parser.add_argument("--mode", choices=["shape", "text"], default="shape",
                        help="'shape': force uniform page dimensions (default). "
                             "'text': only fix pages whose text is actually misaligned.")
    parser.add_argument("--target", choices=["auto", "portrait", "landscape"], default="auto",
                        help="[--mode shape only] Desired orientation for every page. "
                             "'auto' uses whichever is more common (default: auto).")
    parser.add_argument("--min-chars", type=int, default=1,
                        help="[--mode text only] Ignore text lines shorter than this "
                             "many characters when determining angle (default: 1).")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")

    args = parser.parse_args()

    try:
        if args.mode == "shape":
            normalize_by_shape(args.input, args.output, args.target, verbose=not args.quiet)
        else:
            normalize_by_text(args.input, args.output, args.min_chars, verbose=not args.quiet)
    except FileNotFoundError:
        print(f"Error: could not find file '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
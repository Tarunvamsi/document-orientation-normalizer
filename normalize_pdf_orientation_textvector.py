#!/usr/bin/env python3
"""
normalize_pdf_orientation_textvector.py

Rotates pages so their TEXT reads upright - without OCR.

How is this different from the OCR version?
The OCR script (normalize_pdf_orientation_ocr.py) renders each page to an
image and asks Tesseract to visually guess the text angle from pixels.
That works on scanned images, but it's slow and needs the tesseract
binary installed.

This script instead reads the PDF's own content stream directly. Every
piece of text in a real (non-scanned) PDF is drawn with an exact
direction vector - PyMuPDF exposes this via get_text('dict')['...']['dir'].
That means we can determine the EXACT text angle with no guessing, no
image rendering, and no OCR engine at all - only for PDFs that have a
real, extractable text layer (i.e. NOT pages that are just a scanned
photo/image with no underlying text - those have no text vector to read,
and this script will correctly skip them since there's no signal to use).

KEY SUBTLETY: PyMuPDF reports each line's direction in the RAW content
stream space - it does NOT account for whatever rotation the page's
/Rotate flag already has applied. So a page can have perfectly
horizontal text in the raw content (dir=(1,0)) while ALSO being flagged
/Rotate=90, which is what actually makes it display sideways. This
script combines both pieces of information:

    effective_displayed_angle = (raw_text_angle + current_page_rotation) % 360

...and then computes the exact rotation needed to bring that back to 0
(upright), rounded to the nearest 90 degrees (since PDF page rotation
only supports 0/90/180/270).

The rotation is then permanently baked into the content stream and
MediaBox (via transfer_rotation_to_content()) rather than left as a
/Rotate flag, so it displays correctly in any tool - not just ones that
respect that flag.

Requires:
    pip install pypdf pymupdf
    (no OCR engine, no tesseract, no poppler needed)

Usage:
    python3 normalize_pdf_orientation_textvector.py input.pdf output.pdf
    python3 normalize_pdf_orientation_textvector.py input.pdf output.pdf --min-chars 8
"""

import argparse
import math
import sys
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF


def get_dominant_text_angle(fitz_page, min_chars=1):
    """
    Read every text line's direction vector from the page's raw content
    stream and return the character-count-weighted dominant angle in
    degrees (0-360), measured in the page's own untransformed space.

    Returns None if the page has no extractable text (e.g. it's a
    scanned image with no text layer) - there's no signal to act on.
    """
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

    # Vector-sum the weighted directions rather than averaging angles
    # directly - this handles the wraparound/circular nature of angles
    # correctly and is naturally robust to a few outlier lines.
    sum_x = sum(dx * w for dx, dy, w in weighted_vectors)
    sum_y = sum(dy * w for dx, dy, w in weighted_vectors)
    angle = math.degrees(math.atan2(sum_y, sum_x))
    return angle % 360


def compute_rotation_delta(raw_text_angle, current_page_rotation):
    """
    Given the text's raw angle (in content-stream space) and the page's
    already-applied /Rotate value, compute the additional rotation
    (rounded to the nearest 90) needed to make the text display upright.
    """
    effective_angle = (raw_text_angle + current_page_rotation) % 360
    delta = round(-effective_angle / 90) * 90 % 360
    if delta > 180:
        delta -= 360
    return delta


def normalize(input_path, output_path, min_chars=1, verbose=True):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    fitz_doc = fitz.open(input_path)

    rotated_pages = []
    skipped_no_text = []

    for i, page in enumerate(reader.pages):
        fitz_page = fitz_doc[i]
        raw_angle = get_dominant_text_angle(fitz_page, min_chars)

        if raw_angle is None:
            skipped_no_text.append(i + 1)
            writer.add_page(page)
            continue

        current_rotation = page.rotation % 360
        delta = compute_rotation_delta(raw_angle, current_rotation)

        if delta != 0:
            page.rotate(delta)
            page.transfer_rotation_to_content()
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
                  f"found (likely a scanned image with no text), left unchanged: {skipped_no_text}")
        print(f"\nSaved: {output_path}")

    return {
        "rotated_pages": rotated_pages,
        "skipped_pages": skipped_no_text,
        "total_pages": len(reader.pages),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Rotate PDF pages so their text reads upright, using the exact "
                    "text direction from the PDF's content stream (no OCR needed). "
                    "Pages with no extractable text (scanned images) are left unchanged."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("output", help="Path to save the corrected PDF")
    parser.add_argument("--min-chars", type=int, default=1,
                        help="Ignore text lines shorter than this many characters "
                             "when determining the dominant angle (default: 1).")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")

    args = parser.parse_args()

    try:
        normalize(args.input, args.output, args.min_chars, verbose=not args.quiet)
    except FileNotFoundError:
        print(f"Error: could not find file '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
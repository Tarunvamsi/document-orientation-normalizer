#!/usr/bin/env python3
"""
normalize_pdf_orientation_ocr.py

Text-aware version of the PDF orientation normalizer.

The earlier dimension-only script (normalize_pdf_orientation.py) decides
what to rotate purely from page width vs. height. That works for pages
that were scanned sideways, but it also happily "fixes" pages that are
genuinely, intentionally landscape (wide tables, charts) -- rotating
those makes the text sideways instead of fixing anything.

This version instead renders each page to an image (using PyMuPDF, which
is a pure pip install with no system dependency like poppler) and asks
Tesseract OCR's orientation detection (OSD) which way the TEXT is
actually facing, then rotates only pages whose text is genuinely
upside-down or sideways. Pages with wide tables where the text already
reads normally left-to-right are correctly left alone, even though their
page box is landscape-shaped.

Requires:
    Python packages (installable in a venv, no compiling):
        pip install pypdf pymupdf pytesseract

    System binary (NOT pip-installable - this is the one native
    dependency that remains, since PyMuPDF replaces the poppler/
    pdf2image requirement but OCR itself still needs the Tesseract
    engine):
        macOS:   brew install tesseract        (much lighter than
                                                  poppler - no cairo/
                                                  glib/nss chain)
        Ubuntu:  apt-get install tesseract-ocr
        Windows: https://github.com/UB-Mannheim/tesseract/wiki

Usage:
    python3 normalize_pdf_orientation_ocr.py input.pdf output.pdf
    python3 normalize_pdf_orientation_ocr.py input.pdf output.pdf --dpi 200
    python3 normalize_pdf_orientation_ocr.py input.pdf output.pdf --min-confidence 1.0
"""

import argparse
import sys
import io
import os
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF
from PIL import Image
import pytesseract

# If tesseract isn't on your system PATH (e.g. conda installed it but your
# shell isn't activating the conda environment), point pytesseract at the
# exact binary location directly. Common locations are checked automatically;
# override TESSERACT_CMD below if yours lives somewhere else.
_CANDIDATE_TESSERACT_PATHS = [
    "/usr/local/Caskroom/miniconda/base/bin/tesseract",  # conda (Intel Mac)
    "/opt/homebrew/bin/tesseract",                        # brew (Apple Silicon)
    "/usr/local/bin/tesseract",                           # brew (Intel)
    "/usr/bin/tesseract",                                 # Linux apt
]

TESSERACT_CMD = None
for _path in _CANDIDATE_TESSERACT_PATHS:
    if os.path.isfile(_path):
        TESSERACT_CMD = _path
        break

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def render_page_to_image(pdf_path, page_index, dpi=150):
    """Render one page of the PDF to a PIL Image using PyMuPDF -
    no poppler/pdftoppm system dependency required."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    zoom = dpi / 72.0  # PDF points are 1/72 inch; scale to target DPI
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix)
    img_bytes = pix.tobytes("png")
    doc.close()
    return Image.open(io.BytesIO(img_bytes))


def detect_rotation_needed(image, min_confidence=0.0):
    """
    Ask Tesseract's OSD (orientation & script detection) how many degrees
    the image needs to be rotated (clockwise) to make the text upright.

    Returns (rotate_degrees, confidence) or (None, 0) if OCR found no
    usable text (e.g. a blank page or a page that's all images/graphics
    with no real text) -- in that case we can't make a text-based
    decision and the caller should fall back to leaving the page as-is
    or to the dimension heuristic.
    """
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        return None, 0.0

    rotate = osd.get("rotate", 0)
    confidence = osd.get("orientation_conf", 0.0)

    if confidence < min_confidence:
        return None, confidence

    return rotate, confidence


def normalize(input_path, output_path, dpi=150, min_confidence=1.0, verbose=True):
    reader = PdfReader(input_path)
    writer = PdfWriter()

    if verbose:
        print(f"Rendering pages at {dpi} DPI for OCR orientation check...")

    rotated_pages = []
    skipped_low_confidence = []

    for i, page in enumerate(reader.pages):
        image = render_page_to_image(input_path, i, dpi)
        rotate_needed, confidence = detect_rotation_needed(image, min_confidence)

        if rotate_needed is None:
            # No usable text signal (blank page, pure image, handwriting,
            # or confidence too low to trust). Leave the page untouched
            # rather than guess.
            skipped_low_confidence.append(i + 1)
            writer.add_page(page)
            continue

        if rotate_needed != 0:
            page.rotate(rotate_needed)
            # Bake the rotation permanently into the content stream and
            # MediaBox rather than leaving it as a /Rotate flag - some
            # viewers/tools ignore that flag, which looks like "the text
            # didn't rotate." This makes the page a true native shape
            # regardless of viewer.
            page.transfer_rotation_to_content()
            rotated_pages.append((i + 1, rotate_needed, round(confidence, 1)))

        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    if verbose:
        print(f"\nTotal pages: {len(reader.pages)}")
        if rotated_pages:
            print(f"Rotated {len(rotated_pages)} page(s) based on detected text angle:")
            for pg, deg, conf in rotated_pages:
                print(f"  Page {pg}: rotated {deg}° (confidence {conf})")
        else:
            print("No pages needed rotation.")
        if skipped_low_confidence:
            print(f"\nSkipped {len(skipped_low_confidence)} page(s) - no confident text "
                  f"orientation signal (left unchanged): {skipped_low_confidence}")
        print(f"\nSaved: {output_path}")

    return {
        "rotated_pages": rotated_pages,
        "skipped_pages": skipped_low_confidence,
        "total_pages": len(reader.pages),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Detect actual TEXT orientation (via OCR) in a PDF and rotate only "
                    "pages whose text is sideways/upside-down, leaving genuinely "
                    "landscape content (wide tables etc.) untouched."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("output", help="Path to save the corrected PDF")
    parser.add_argument("--dpi", type=int, default=150,
                        help="Render DPI for OCR (higher = more accurate but slower). Default 150.")
    parser.add_argument("--min-confidence", type=float, default=1.0,
                        help="Minimum OSD confidence to trust the rotation decision. "
                             "Pages below this are left unchanged. Default 1.0.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")

    args = parser.parse_args()

    try:
        normalize(args.input, args.output, args.dpi, args.min_confidence, verbose=not args.quiet)
    except FileNotFoundError:
        print(f"Error: could not find file '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
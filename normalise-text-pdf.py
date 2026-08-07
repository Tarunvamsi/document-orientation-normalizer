#!/usr/bin/env python3

import argparse
import sys
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
import pytesseract


def detect_rotation_needed(image, min_confidence=0.0):
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

    images = convert_from_path(input_path, dpi=dpi)

    rotated_pages = []
    skipped_low_confidence = []

    for i, (page, image) in enumerate(zip(reader.pages, images)):
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
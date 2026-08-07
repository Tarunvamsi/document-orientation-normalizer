#!/usr/bin/env python3
"""
normalize_pdf_orientation.py

Makes every page in a PDF the same orientation (all portrait, or all
landscape). Pages that don't match the target are rotated - and since
rotating a PDF page rotates everything drawn on it, the text/content
rotates right along with the page. That means a page that gets flipped
from landscape to portrait will have its content end up sideways - that
is expected and intentional: this script prioritizes uniform page shape
over keeping every page's text upright. (If instead you want text to
always stay readable and don't care about strict shape uniformity, that
is a different, harder problem requiring OCR - ask if you need that
version instead.)

ORIENTATION DETECTION
Each page's orientation is computed from its *effective* (as-displayed)
width/height - i.e. after applying whatever rotation is already stored
in the page's /Rotate attribute. This matters because some PDFs already
have pages that are physically one shape but displayed as another via
/Rotate (e.g. a scanned page turned sideways at scan time).

ROTATION DIRECTION FIX
Naively always rotating by +90 degrees to fix a mismatched page can
backfire: if a page already has an existing rotation (e.g. it was
already rotated 90 to fix a sideways scan), blindly adding another +90
can land it on 180 (upside-down) instead of correctly back at 0. This
script checks the page's current rotation first and picks the rotation
direction that actually resolves it, rather than always turning the
same way.

USAGE
    python3 normalize_pdf_orientation.py input.pdf output.pdf
    python3 normalize_pdf_orientation.py input.pdf output.pdf --target portrait
    python3 normalize_pdf_orientation.py input.pdf output.pdf --target landscape
    python3 normalize_pdf_orientation.py input.pdf output.pdf --target auto --quiet

    --target auto (default): whichever orientation is more common among
        the document's pages wins; only the minority pages get rotated.
    --target portrait / landscape: force every page to that orientation,
        regardless of what's currently more common.

REQUIRES
    pip install pypdf
"""

import argparse
import sys
from pypdf import PdfReader, PdfWriter


def effective_size(page):
    """Return (width, height) of a page AS DISPLAYED, i.e. after
    applying the page's current /Rotate value. pypdf's page.rotation
    reflects any rotation already stored on the page."""
    box = page.mediabox
    width = float(box.width)
    height = float(box.height)

    rotation = page.rotation % 360
    if rotation in (90, 270):
        width, height = height, width

    return width, height


def analyze(reader):
    """Return a list of dicts describing each page's current state."""
    info = []
    for i, page in enumerate(reader.pages):
        width, height = effective_size(page)
        info.append({
            "index": i,
            "orientation": "landscape" if width > height else "portrait",
            "width": round(width, 1),
            "height": round(height, 1),
            "current_rotation": page.rotation % 360,
        })
    return info


def decide_target(info, target_arg):
    """Resolve --target auto into an actual portrait/landscape choice
    based on majority vote across the document's pages."""
    if target_arg in ("portrait", "landscape"):
        return target_arg

    portrait_count = sum(1 for p in info if p["orientation"] == "portrait")
    landscape_count = len(info) - portrait_count
    return "portrait" if portrait_count >= landscape_count else "landscape"


def rotation_delta_for_fix(current_rotation):
    """
    Decide which way to rotate (+90 or -90) to flip a page's effective
    orientation (portrait<->landscape).

    If the page already has a 90/270 rotation applied, prefer undoing
    it (move back toward 0) rather than piling on more rotation, which
    can otherwise land the page upside-down (180) instead of upright.
    For pages with no prior rotation (0 or 180), there's no "undo"
    direction to prefer, so default to +90.
    """
    if current_rotation == 90:
        return -90
    elif current_rotation == 270:
        return 90
    else:  # 0 or 180 - no existing rotation to undo, direction is arbitrary
        return 90


def normalize(input_path, output_path, target_arg="auto", verbose=True):
    reader = PdfReader(input_path)
    info = analyze(reader)

    if not info:
        raise ValueError("PDF has no pages.")

    target = decide_target(info, target_arg)

    if verbose:
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
            delta = rotation_delta_for_fix(p["current_rotation"])
            page.rotate(delta)
            # IMPORTANT: page.rotate() only sets the page's /Rotate flag -
            # a hint telling viewers "display this rotated." It does NOT
            # touch the actual text/content coordinates or the MediaBox.
            # Well-behaved viewers respect /Rotate and rotate the visual
            # display correctly, but some tools (older viewers, certain
            # print/merge pipelines, some processing scripts) ignore it,
            # which looks like "the text didn't rotate."
            #
            # transfer_rotation_to_content() permanently bakes the
            # rotation into the actual content stream and swaps the
            # MediaBox dimensions, producing a true native page in the
            # new orientation - not dependent on any viewer honoring
            # /Rotate. This is the fix for content appearing unrotated.
            page.transfer_rotation_to_content()
            rotated_pages.append((p["index"] + 1, delta))  # 1-indexed for humans

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

    return {
        "target": target,
        "rotated_pages": rotated_pages,
        "total_pages": len(info),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Make every page in a PDF the same orientation (portrait or "
                    "landscape) by rotating mismatched pages - content rotates "
                    "along with the page."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("output", help="Path to save the normalized PDF")
    parser.add_argument(
        "--target",
        choices=["auto", "portrait", "landscape"],
        default="auto",
        help="Desired final orientation for every page. 'auto' uses whichever "
             "orientation is more common in the document (default: auto).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")

    args = parser.parse_args()

    try:
        normalize(args.input, args.output, args.target, verbose=not args.quiet)
    except FileNotFoundError:
        print(f"Error: could not find file '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
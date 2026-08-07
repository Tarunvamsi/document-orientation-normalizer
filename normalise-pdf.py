#!/usr/bin/env python3
"""
normalize_pdf_orientation.py

Detects the orientation (portrait / landscape) of every page in a PDF and
rotates the "wrong-way" pages so that the whole document ends up uniform
(all portrait, or all landscape).

Orientation detection accounts for the page's /Rotate value (a page can be
physically wide but displayed as tall because a 90/270 rotation is already
applied), so it looks at the *effective* (as-displayed) width and height,
not just the raw MediaBox.

Usage:
    python normalize_pdf_orientation.py input.pdf output.pdf
    python normalize_pdf_orientation.py input.pdf output.pdf --target portrait
    python normalize_pdf_orientation.py input.pdf output.pdf --target landscape
    python normalize_pdf_orientation.py input.pdf output.pdf --target auto

--target auto (default): the majority orientation in the document wins,
    and only the minority pages get rotated.
--target portrait / landscape: force every page to that orientation
    regardless of what's currently more common.

Requires: pypdf   ->  pip install pypdf --break-system-packages
"""

import argparse
import sys
from pypdf import PdfReader, PdfWriter


def effective_size(page):
    """Return (width, height) of a page AS DISPLAYED, i.e. after applying
    the page's /Rotate attribute. pypdf's page.rotation already reflects
    any accumulated rotation."""
    box = page.mediabox
    width = float(box.width)
    height = float(box.height)

    rotation = page.rotation % 360
    if rotation in (90, 270):
        width, height = height, width

    return width, height


def orientation_of(page):
    width, height = effective_size(page)
    if width > height:
        return "landscape"
    return "portrait"


def analyze(reader):
    """Return a list of dicts: page index, orientation, width, height."""
    info = []
    for i, page in enumerate(reader.pages):
        width, height = effective_size(page)
        info.append({
            "index": i,
            "orientation": "landscape" if width > height else "portrait",
            "width": round(width, 1),
            "height": round(height, 1),
        })
    return info


def decide_target(info, target_arg):
    if target_arg in ("portrait", "landscape"):
        return target_arg

    # auto -> majority vote
    portrait_count = sum(1 for p in info if p["orientation"] == "portrait")
    landscape_count = len(info) - portrait_count
    return "portrait" if portrait_count >= landscape_count else "landscape"


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
        needs_rotation = p["orientation"] != target
        if needs_rotation:
            # IMPORTANT: don't blindly always rotate +90. If the page
            # already has a rotation applied (e.g. it was scanned in
            # sideways and already carries /Rotate=90), adding another
            # +90 can land it on 180 (upside-down) instead of back to 0
            # (upright) - both "fix" the portrait/landscape box shape,
            # but only one is actually readable.
            #
            # Heuristic: prefer undoing an existing 90/270 rotation by
            # moving BACK toward 0 rather than piling on more rotation.
            # This correctly fixes the common "scanned sideways" case.
            # For pages with no prior rotation (freshly landscape/portrait
            # content), direction is ambiguous either way, so default to
            # +90.
            current = page.rotation % 360
            if current == 90:
                delta = -90
            elif current == 270:
                delta = 90
            else:  # current in (0, 180) - no clear "undo" direction
                delta = 90
            page.rotate(delta)
            rotated_pages.append(p["index"] + 1)  # 1-indexed for humans

        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    if verbose:
        if rotated_pages:
            print(f"Rotated {len(rotated_pages)} page(s) to match target: {rotated_pages}")
        else:
            print("No pages needed rotation; document was already uniform.")
        print(f"Saved: {output_path}")

    return {
        "target": target,
        "rotated_pages": rotated_pages,
        "total_pages": len(info),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Detect page orientation in a PDF and rotate pages so the whole document is uniform (all portrait or all landscape)."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("output", help="Path to save the normalized PDF")
    parser.add_argument(
        "--target",
        choices=["auto", "portrait", "landscape"],
        default="auto",
        help="Desired final orientation for every page. 'auto' uses whichever orientation is more common in the document (default: auto).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress progress output."
    )

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
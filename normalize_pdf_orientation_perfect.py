#!/usr/bin/env python3
"""
normalize_pdf_orientation_perfect.py

Produces a PDF where every page is the same shape (portrait or
landscape) AND every page's text is upright and readable - the
"perfect PDF" combination.

This requires two different fixes depending on WHY a page doesn't
match, applied in order:

STEP 1 - Fix genuine misrotation (lossless, no visual quality loss)
    Some mismatched pages are really just tagged with the wrong
    /Rotate value (e.g. a page scanned in sideways). Their underlying
    content is already the right shape - it just needs to be rotated
    back. This is detected by reading the page's raw MediaBox
    dimensions (its true native shape, ignoring any /Rotate flag) - if
    that native shape already matches the target orientation, the page
    only needs a rotation to align its text, not scaling.

STEP 2 - Scale-to-fit (fallback for genuinely oversized content)
    Some pages are natively, intentionally the "wrong" shape - e.g. a
    wide 8-column data table authored as a landscape page in an
    otherwise-portrait report. No rotation can make that content both
    upright AND the target shape at the same time, because rotating
    only swaps which dimension is which - it can't make wide content
    narrower. For these pages, the content is scaled down uniformly
    (preserving its aspect ratio, staying upright, centered on a
    target-sized blank page) so it fits within the target dimensions.
    This is the only way to get uniform page shape without either
    sideways text or cropped/cut-off content - the trade-off is smaller
    text on that page.

Both fixes are permanent (baked into the actual content stream and
MediaBox), not left as a soft /Rotate flag.

Requires:
    pip install pypdf pymupdf

Usage:
    python3 normalize_pdf_orientation_perfect.py input.pdf output.pdf
    python3 normalize_pdf_orientation_perfect.py input.pdf output.pdf --target landscape
"""

import argparse
import math
import sys
from collections import Counter
from pypdf import PdfReader, PdfWriter, Transformation
import fitz  # PyMuPDF, used only to read exact text direction


# ---------------------------------------------------------------------------
# Text angle detection (same approach as the text-vector script)
# ---------------------------------------------------------------------------

def get_dominant_text_angle(fitz_page, min_chars=1):
    text_dict = fitz_page.get_text("dict")
    weighted = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            chars = sum(len(s.get("text", "")) for s in line.get("spans", []))
            if chars >= min_chars:
                weighted.append((dx, dy, chars))
    if not weighted:
        return None
    sx = sum(dx * w for dx, dy, w in weighted)
    sy = sum(dy * w for dx, dy, w in weighted)
    return math.degrees(math.atan2(sy, sx)) % 360


def rotation_to_upright(raw_text_angle, current_rotation):
    effective = (raw_text_angle + current_rotation) % 360
    delta = round(-effective / 90) * 90 % 360
    if delta > 180:
        delta -= 360
    return delta


def get_content_bbox(fitz_page, page_width, page_height):
    """Return the bounding box (x0, y0, x1, y1) of the actual drawn
    content (text + images/drawings) on the page, in PDF coordinate
    space (origin bottom-left). Falls back to the full page box if
    nothing is found.

    This matters because a page's MediaBox can be much bigger than
    what's actually drawn on it (e.g. normal margins on a landscape
    page) - scaling based on the full box instead of the real content
    would preserve that existing blank space and then add MORE blank
    space on top when centering on the target canvas, compounding into
    a result that looks tiny and off-center even though nothing is
    technically wrong.
    """
    text_dict = fitz_page.get_text("dict")
    xs0, ys0, xs1, ys1 = [], [], [], []

    for block in text_dict.get("blocks", []):
        bbox = block.get("bbox")
        if bbox:
            xs0.append(bbox[0]); ys0.append(bbox[1])
            xs1.append(bbox[2]); ys1.append(bbox[3])

    # also include any drawings/images so we don't crop out graphics
    try:
        for d in fitz_page.get_drawings():
            r = d.get("rect")
            if r:
                xs0.append(r.x0); ys0.append(r.y0)
                xs1.append(r.x1); ys1.append(r.y1)
    except Exception:
        pass

    for img in fitz_page.get_image_info():
        bbox = img.get("bbox")
        if bbox:
            xs0.append(bbox[0]); ys0.append(bbox[1])
            xs1.append(bbox[2]); ys1.append(bbox[3])

    if not xs0:
        return (0, 0, page_width, page_height)

    # fitz reports bbox with y measured from the TOP of the page;
    # pypdf/PDF coordinate space measures y from the BOTTOM - convert.
    x0, x1 = min(xs0), max(xs1)
    top_y0, top_y1 = min(ys0), max(ys1)
    y0 = page_height - top_y1
    y1 = page_height - top_y0

    return (x0, y0, x1, y1)

def rotate_bbox(bbox, delta, page_w, page_h):
    """Transform a content bbox (in original page space) into the
    coordinate space of a page that has just been rotated by delta
    degrees, so scaling math afterward stays correct for pages that
    need BOTH a rotation-correction and scaling."""
    delta = delta % 360
    x0, y0, x1, y1 = bbox
    if delta == 0:
        return bbox
    if delta == 90:
        return (page_h - y1, x0, page_h - y0, x1)
    if delta == 270:
        return (y0, page_w - x1, y1, page_w - x0)
    if delta == 180:
        return (page_w - x1, page_h - y1, page_w - x0, page_h - y0)
    return bbox


def determine_target_size(reader):
    """Use the most common raw page size in the document as the
    canvas every page should end up matching."""
    sizes = [(round(float(p.mediabox.width)), round(float(p.mediabox.height)))
             for p in reader.pages]
    return Counter(sizes).most_common(1)[0][0]


def normalize(input_path, output_path, target_arg="auto", margin=36, verbose=True):
    reader = PdfReader(input_path)
    fitz_doc = fitz.open(input_path)
    writer = PdfWriter()

    raw_w, raw_h = determine_target_size(reader)
    if target_arg == "portrait":
        target_w, target_h = min(raw_w, raw_h), max(raw_w, raw_h)
    elif target_arg == "landscape":
        target_w, target_h = max(raw_w, raw_h), min(raw_w, raw_h)
    else:
        target_w, target_h = raw_w, raw_h

    if verbose:
        print(f"Target page size: {target_w} x {target_h}")

    rotated_only = []
    scaled = []
    unchanged = []

    for i, page in enumerate(reader.pages):
        native_w = float(page.mediabox.width)
        native_h = float(page.mediabox.height)
        current_rotation = page.rotation % 360

        raw_angle = get_dominant_text_angle(fitz_doc[i])

        # STEP 1: if the page's native (unrotated) box already fits the
        # target orientation, this is just a misrotation - fix it with a
        # lossless rotation, no scaling needed.
        native_fits_target = (native_w <= target_w and native_h <= target_h)

        if native_fits_target:
            if raw_angle is not None:
                delta = rotation_to_upright(raw_angle, current_rotation)
            else:
                # no text to check - fall back to just fixing shape via
                # the current rotation flag alone
                delta = 0 if current_rotation == 0 else (
                    -90 if current_rotation == 90 else
                    90 if current_rotation == 270 else 90
                )
            if delta != 0:
                page.rotate(delta)
                page.transfer_rotation_to_content()
                rotated_only.append((i + 1, delta))
            else:
                unchanged.append(i + 1)
            writer.add_page(page)
            continue

        # STEP 2: native content is genuinely too big for the target in
        # its own upright orientation - first correct any misrotation so
        # we're scaling upright content, then scale-to-fit.
        pre_rotation_delta = 0
        if raw_angle is not None:
            pre_rotation_delta = rotation_to_upright(raw_angle, current_rotation)
            if pre_rotation_delta != 0:
                page.rotate(pre_rotation_delta)
                page.transfer_rotation_to_content()

        # Measure content extents on the ORIGINAL (unrotated) fitz page -
        # fitz_doc wasn't modified by the pypdf rotation above, so if we
        # just rotated `page`, transform the measured bbox to match.
        content_bbox = get_content_bbox(fitz_doc[i], native_w, native_h)
        if pre_rotation_delta != 0:
            content_bbox = rotate_bbox(content_bbox, pre_rotation_delta, native_w, native_h)

        cx0, cy0, cx1, cy1 = content_bbox
        content_w = max(cx1 - cx0, 1e-6)
        content_h = max(cy1 - cy0, 1e-6)

        # Leave a margin around the edges rather than filling the target
        # exactly - fitting content into (target - 2*margin) keeps text
        # from touching the page border.
        usable_w = max(target_w - 2 * margin, 1e-6)
        usable_h = max(target_h - 2 * margin, 1e-6)

        scale = min(usable_w / content_w, usable_h / content_h)
        new_content_w, new_content_h = content_w * scale, content_h * scale

        # center the SCALED CONTENT within the usable (margin-inset) area
        tx = margin + (usable_w - new_content_w) / 2 - (cx0 * scale)
        ty = margin + (usable_h - new_content_h) / 2 - (cy0 * scale)

        blank = writer.add_blank_page(width=target_w, height=target_h)
        transform = Transformation().scale(scale, scale).translate(tx, ty)
        blank.merge_transformed_page(page, transform)
        scaled.append((i + 1, round(scale, 2)))

    fitz_doc.close()

    with open(output_path, "wb") as f:
        writer.write(f)

    if verbose:
        print(f"\nTotal pages: {len(reader.pages)}")
        if rotated_only:
            print(f"Rotated only (lossless fix): {rotated_only}")
        if scaled:
            print(f"Scaled to fit (content was natively oversized): {scaled}")
        if unchanged:
            print(f"Already correct: {unchanged}")
        print(f"\nSaved: {output_path}")

    return {"rotated_only": rotated_only, "scaled": scaled, "unchanged": unchanged}


def main():
    parser = argparse.ArgumentParser(
        description="Make every PDF page the same shape AND keep all text upright. "
                    "Misrotated pages are fixed losslessly via rotation; genuinely "
                    "oversized content (e.g. wide tables) is scaled to fit instead."
    )
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--target", choices=["auto", "portrait", "landscape"], default="auto",
                        help="Target orientation. 'auto' uses the document's most common "
                             "page size as-is (default).")
    parser.add_argument("--margin", type=float, default=36,
                        help="Margin in points to leave around scaled content, so it "
                             "doesn't touch the page edge (default: 36pt = 0.5in).")
    parser.add_argument("--quiet", action="store_true")

    args = parser.parse_args()

    try:
        normalize(args.input, args.output, args.target, args.margin, verbose=not args.quiet)
    except FileNotFoundError:
        print(f"Error: could not find file '{args.input}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
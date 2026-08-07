#!/usr/bin/env python3
"""
normalize_docx_orientation.py

Word (.docx) analog of normalize_pdf_orientation_perfect.py.

Word has no per-page rotation concept - orientation is a per-SECTION
property (<w:sectPr>/<w:pgSz w:orient="portrait|landscape">), and a
section can span many pages. Word never renders sideways text, so
there's no "misrotated scan" case to fix losslessly the way there is
in a PDF. Every orientation mismatch here is really the PDF script's
STEP 2 case: content that's genuinely wide (e.g. a big table) living
on a landscape page inside an otherwise-portrait document.

What this script does, per section that doesn't match the target
orientation:
  1. Rewrites that section's <w:pgSz> to the target width/height/orient
     (margins are left untouched).
  2. Computes scale = (new usable width) / (old usable width), where
     usable width = page width - left/right margins.
  3. If scale < 1 (i.e. we're narrowing the page), proportionally
     shrinks every table's overall width, grid-column widths, and
     cell widths in that section, plus every inline/floating image's
     extent (cx/cy in EMUs, scaled uniformly so aspect ratio holds).
     If scale >= 1 (widening), nothing is scaled - extra margin is
     harmless.

This is a best-effort geometry fix, not a layout engine: text itself
is not re-sized, so a shrunk table's cells will wrap text onto more
lines rather than shrinking the font. That is the direct docx
equivalent of the PDF script's "smaller text" trade-off - the
alternative (clipped/cut-off content) is worse.

Requires: lxml
Usage:
    python3 normalize_docx_orientation.py input.docx output.docx
    python3 normalize_docx_orientation.py input.docx output.docx --target portrait
"""

import argparse
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
NSMAP = {"w": W_NS, "wp": WP_NS, "a": A_NS}


def qn(tag):
    prefix, local = tag.split(":")
    return f"{{{NSMAP[prefix]}}}{local}"


def get_pgsz(sectpr):
    pgsz = sectpr.find(qn("w:pgSz"))
    if pgsz is None:
        return None
    w = int(pgsz.get(qn("w:w")))
    h = int(pgsz.get(qn("w:h")))
    orient = pgsz.get(qn("w:orient"), "portrait")
    return pgsz, w, h, orient


def get_margins(sectpr):
    pgmar = sectpr.find(qn("w:pgMar"))
    if pgmar is None:
        return 1440, 1440
    left = int(pgmar.get(qn("w:left"), "1440"))
    right = int(pgmar.get(qn("w:right"), "1440"))
    return left, right


def find_sections(body):
    """Walk body's direct children and split them into sections.

    Returns a list of dicts: {sectPr, content_elements, is_final}
    A section's sectPr lives either inside the pPr of its last
    paragraph (a "section-break paragraph", usually empty) for every
    section but the last, or as a direct trailing child of <w:body>
    for the final section.
    """
    sections = []
    content_buffer = []
    for child in body:
        tag = etree.QName(child).localname
        if tag == "p":
            ppr = child.find(qn("w:pPr"))
            sectpr = ppr.find(qn("w:sectPr")) if ppr is not None else None
            if sectpr is not None:
                sections.append({
                    "sectPr": sectpr,
                    "content": content_buffer,
                    "is_final": False,
                })
                content_buffer = []
                continue  # the break-marker paragraph itself isn't section content
            content_buffer.append(child)
        elif tag == "sectPr":
            # trailing sectPr for the FINAL section, direct child of body
            sections.append({
                "sectPr": child,
                "content": content_buffer,
                "is_final": True,
            })
            content_buffer = []
        else:
            content_buffer.append(child)
    return sections


def determine_target(sections, target_arg):
    sizes = []
    orients = Counter()
    for s in sections:
        info = get_pgsz(s["sectPr"])
        if info is None:
            continue
        _, w, h, orient = info
        long_dim, short_dim = max(w, h), min(w, h)
        sizes.append((long_dim, short_dim))
        orients[orient] += 1

    (long_dim, short_dim), _ = Counter(sizes).most_common(1)[0]

    if target_arg == "portrait":
        return short_dim, long_dim, "portrait"
    if target_arg == "landscape":
        return long_dim, short_dim, "landscape"
    # auto: majority orientation among existing sections
    majority = orients.most_common(1)[0][0] if orients else "portrait"
    if majority == "landscape":
        return long_dim, short_dim, "landscape"
    return short_dim, long_dim, "portrait"


def scale_tables_and_images(content_elements, scale):
    if scale >= 1.0:
        return
    for el in content_elements:
        for tbl in el.iter(qn("w:tbl")):
            tblpr = tbl.find(qn("w:tblPr"))
            if tblpr is not None:
                tblw = tblpr.find(qn("w:tblW"))
                if tblw is not None and tblw.get(qn("w:type")) == "dxa":
                    tblw.set(qn("w:w"), str(round(int(tblw.get(qn("w:w"))) * scale)))
            for gridcol in tbl.iter(qn("w:gridCol")):
                cur = gridcol.get(qn("w:w"))
                if cur is not None:
                    gridcol.set(qn("w:w"), str(round(int(cur) * scale)))
            for tcw in tbl.iter(qn("w:tcW")):
                if tcw.get(qn("w:type")) == "dxa":
                    cur = tcw.get(qn("w:w"))
                    if cur is not None:
                        tcw.set(qn("w:w"), str(round(int(cur) * scale)))
        for extent in el.iter(qn("wp:extent")):
            cx = extent.get("cx")
            cy = extent.get("cy")
            if cx is not None:
                extent.set("cx", str(round(int(cx) * scale)))
            if cy is not None:
                extent.set("cy", str(round(int(cy) * scale)))
        for aext in el.iter(qn("a:ext")):
            cx = aext.get("cx")
            cy = aext.get("cy")
            if cx is not None:
                aext.set("cx", str(round(int(cx) * scale)))
            if cy is not None:
                aext.set("cy", str(round(int(cy) * scale)))


def normalize(input_path, output_path, target_arg="auto", verbose=True):
    work_dir = Path(output_path).parent / f".{Path(output_path).stem}_unpacked"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    with zipfile.ZipFile(input_path) as z:
        z.extractall(work_dir)

    doc_xml_path = work_dir / "word" / "document.xml"
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(doc_xml_path), parser)
    root = tree.getroot()
    body = root.find(qn("w:body"))

    sections = find_sections(body)
    target_w, target_h, target_orient = determine_target(sections, target_arg)

    if verbose:
        print(f"Target page size: {target_w} x {target_h} ({target_orient})")
        print(f"Sections found: {len(sections)}")

    changed, unchanged, scaled_report = [], [], []

    for idx, s in enumerate(sections):
        sectpr = s["sectPr"]
        info = get_pgsz(sectpr)
        if info is None:
            unchanged.append(idx + 1)
            continue
        pgsz_el, w, h, orient = info

        if w == target_w and h == target_h and orient == target_orient:
            unchanged.append(idx + 1)
            continue

        left, right = get_margins(sectpr)
        old_usable = w - left - right
        new_usable = target_w - left - right
        scale = new_usable / old_usable if old_usable > 0 else 1.0

        pgsz_el.set(qn("w:w"), str(target_w))
        pgsz_el.set(qn("w:h"), str(target_h))
        pgsz_el.set(qn("w:orient"), target_orient)

        if scale < 1.0:
            scale_tables_and_images(s["content"], scale)
            scaled_report.append((idx + 1, round(scale, 3)))
        changed.append(idx + 1)

    tree.write(str(doc_xml_path), xml_declaration=True, encoding="UTF-8", standalone=True)

    if output_path == str(input_path):
        raise ValueError("output_path must differ from input_path")
    if Path(output_path).exists():
        Path(output_path).unlink()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(work_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(work_dir))

    shutil.rmtree(work_dir)

    if verbose:
        print(f"\nSections changed to target orientation: {changed}")
        if scaled_report:
            print(f"Sections with tables/images scaled down: {scaled_report}")
        print(f"Sections already matching target: {unchanged}")
        print(f"\nSaved: {output_path}")

    return {"changed": changed, "scaled": scaled_report, "unchanged": unchanged}


def main():
    parser = argparse.ArgumentParser(
        description="Force every section in a .docx to the same page orientation, "
                     "scaling down tables/images in sections that were landscape "
                     "so nothing gets clipped."
    )
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--target", choices=["auto", "portrait", "landscape"], default="auto",
                         help="Target orientation. 'auto' uses whichever orientation the "
                              "majority of sections already have (default).")
    parser.add_argument("--quiet", action="store_true")

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
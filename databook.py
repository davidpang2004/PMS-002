"""
Databook generation — assembles selected documents from the DMS into a single
deliverable PDF with cover page, table of contents, bookmarks, and a
page-number/SN header on every page. Folder/section names are intentionally
never shown anywhere in the output — only each document's own name (in the
TOC/bookmarks) and its SN (stamped at the top of every page).

Public entry point:
    build_databook(selection, docs_dir, doc_index, tree, options) -> bytes
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, grey
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, HRFlowable,
)

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch

# ---------------------------------------------------------------------------
# Unicode font registration — enables CJK (Chinese/Japanese/Korean) text
# ---------------------------------------------------------------------------
# Module-level font names; updated by _init_fonts() on first use.
_FONT      = "Helvetica"       # regular — used in canvas.setFont() and ParagraphStyle
_FONT_BOLD = "Helvetica-Bold"  # bold    — used in canvas.setFont() and bold ParagraphStyle
_FONTS_READY = False

_HEIF_READY = False


def _ensure_heif_support() -> None:
    """Register Pillow's HEIC/HEIF opener so Image.open() can read iPhone
    photos. Without this, HEIC images silently fall back to the "could not
    render" placeholder in _image_to_pdf_bytes. Idempotent and safe to call
    even if pillow_heif isn't installed (HEIC just won't embed in that case,
    same as before)."""
    global _HEIF_READY
    if _HEIF_READY:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    _HEIF_READY = True


def _init_fonts() -> None:
    """Register a Unicode-capable font pair so CJK text renders correctly.

    Tries platform fonts in order of preference; silently falls back to
    Helvetica if nothing suitable is found (Latin text still works fine).
    """
    global _FONT, _FONT_BOLD, _FONTS_READY
    if _FONTS_READY:
        return
    _FONTS_READY = True

    # (regular_path, reg_idx, bold_path, bold_idx)
    # idx=None → plain TTF (not a TTC collection)
    candidates = [
        # macOS — STHeiti (sans-serif, ships with every Mac)
        ("/System/Library/Fonts/STHeiti Light.ttc",  0,
         "/System/Library/Fonts/STHeiti Medium.ttc", 0),
        # macOS — Songti (serif, fallback)
        ("/System/Library/Fonts/Supplemental/Songti.ttc", 0,
         "/System/Library/Fonts/Supplemental/Songti.ttc", 1),
        # Windows — Microsoft YaHei
        ("C:/Windows/Fonts/msyh.ttc",   0, "C:/Windows/Fonts/msyhbd.ttc", 0),
        ("C:/Windows/Fonts/simhei.ttf", None, "C:/Windows/Fonts/simhei.ttf", None),
        # Linux — Noto Sans CJK SC
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2,
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",    2),
        ("/usr/share/fonts/truetype/arphic/uming.ttc", 0,
         "/usr/share/fonts/truetype/arphic/uming.ttc", 0),
    ]

    for reg_path, reg_idx, bold_path, bold_idx in candidates:
        if not os.path.exists(reg_path):
            continue
        try:
            kw_r = {"subfontIndex": reg_idx}  if reg_idx  is not None else {}
            kw_b = {"subfontIndex": bold_idx} if bold_idx is not None else {}
            bold_src = bold_path if (bold_path and os.path.exists(bold_path)) else reg_path
            pdfmetrics.registerFont(TTFont("_DMS_R", reg_path, **kw_r))
            pdfmetrics.registerFont(TTFont("_DMS_B", bold_src, **kw_b))
            # Tell reportlab's paragraph engine how to find bold/italic variants
            # when it encounters <b> or <i> tags inside a Paragraph.
            addMapping("_DMS_R", 0, 0, "_DMS_R")   # regular
            addMapping("_DMS_R", 1, 0, "_DMS_B")   # bold
            addMapping("_DMS_R", 0, 1, "_DMS_R")   # italic → same as regular
            addMapping("_DMS_R", 1, 1, "_DMS_B")   # bold-italic → same as bold
            _FONT      = "_DMS_R"
            _FONT_BOLD = "_DMS_B"
            return
        except Exception:
            continue
    # No CJK font found — Latin/ASCII will still render; CJK chars will be blank.

ACCENT = HexColor("#b45309")          # amber-700, matches the DMS UI
SUBTLE = HexColor("#78716c")          # stone-500
BORDER = HexColor("#d6d3d1")          # stone-300


# ---------------------------------------------------------------------------
# Helpers — find docs, build node maps, etc.
# ---------------------------------------------------------------------------
def _find_node(tree: dict, node_id: str):
    if not tree:
        return None
    if tree.get("id") == node_id:
        return tree
    for child in tree.get("children") or []:
        found = _find_node(child, node_id)
        if found:
            return found
    return None


def _path_for_node(tree: dict, node_id: str) -> str:
    """Return ' / '-separated path of node names from root to nodeId."""
    out: list[str] = []

    def walk(n, trail):
        t = trail + [n.get("name", "")]
        if n.get("id") == node_id:
            out.extend(t)
            return True
        for c in n.get("children") or []:
            if walk(c, t):
                return True
        return False

    if tree:
        walk(tree, [])
    return " / ".join(out)


def _doc_path(docs_dir: Path, doc_id: str) -> Path | None:
    """Find the on-disk file for a given DOC ID.

    Files are stored as '<DOC-ID>__<original-name>.<ext>' inside per-node
    subdirectories of docs_dir (see dms_server._migrate_flat_docs), so this
    must search recursively — matching the rglob pattern used by every doc
    resolution site in dms_server.py — rather than globbing docs_dir itself.
    """
    matches = list(docs_dir.rglob(f"{doc_id}__*")) or list(docs_dir.rglob(f"{doc_id}*"))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Image → single-PDF-page conversion (Pillow)
# ---------------------------------------------------------------------------
# Photos are only ever displayed shrunk to fit a letter-size page (see below),
# but reportlab's drawImage embeds whatever pixel data it's handed — passing
# it the original file directly (as this used to) bakes the FULL original
# resolution into the PDF, deflate-compressed as a near-raw pixel array
# rather than JPEG. For a modern phone photo (e.g. 3024x4032) that's ~14 MB
# per image once embedded, even though HEIC/JPEG source files are ~1 MB —
# merging 100+ photos this way produced a multi-gigabyte PDF. Downscaling to
# a box no bigger than any page can actually show, and re-encoding as JPEG
# before handing it to reportlab, cuts that by two-plus orders of magnitude
# with no visible loss on the page.
_MAX_EMBED_DIM = 900   # px, longest edge fit within this box (~165x smaller than embedding full-res)
_EMBED_JPEG_QUALITY = 85

# Image extensions we can embed via Pillow (HEIC/HEIF need pillow-heif, which
# _ensure_heif_support() registers). Used as a fallback when a document's mime
# is missing or generic — see the dispatch in build_databook().
_IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif",
}


def _downscaled_jpeg_reader(img_path: Path, max_dim: int = _MAX_EMBED_DIM) -> "ImageReader":
    """Open an image, downscale it to fit within max_dim x max_dim (no
    upscaling — small images pass through unchanged), and return an
    ImageReader wrapping a re-encoded JPEG. reportlab embeds an already-JPEG
    source as-is (no further re-compression), so this is what actually keeps
    the output PDF small — resizing alone wouldn't help if reportlab still
    stored the result as a raw/deflate pixel array."""
    with Image.open(img_path) as im:
        im = im.convert("RGB")
        iw, ih = im.size
        scale = min(1.0, max_dim / max(iw, ih))
        if scale < 1.0:
            im = im.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=_EMBED_JPEG_QUALITY, optimize=True)
        buf.seek(0)
        return ImageReader(buf)


def _image_to_pdf_bytes(img_path: Path, caption: str, strict: bool = False,
                        max_dim: int = _MAX_EMBED_DIM,
                        meta_lines: "list[str] | None" = None) -> bytes:
    """Convert an image into a single-page PDF using reportlab, preserving aspect.

    When strict is True, a render failure raises instead of producing a page
    with a "[Could not render image]" note -- callers that want to fall back
    to a different rendering path (see build_ask_ai_pdf) pass strict=True.
    max_dim caps the embedded pixels' longest edge (higher = more legible
    detail, larger file). meta_lines, if given, are printed as small grey
    lines under the caption (date taken, GPS/place, description, ...).
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Caption at top
    c.setFont(_FONT_BOLD, 11)
    c.setFillColor(black)
    c.drawString(MARGIN, PAGE_H - MARGIN, caption[:90])

    header_h = 24
    for i, line in enumerate(meta_lines or []):
        c.setFont(_FONT, 8.5)
        c.setFillColor(SUBTLE)
        c.drawString(MARGIN, PAGE_H - MARGIN - 12 - i * 11, str(line)[:120])
        header_h = 24 + (i + 1) * 11

    # Compute fit area below the caption + metadata block
    top_y = PAGE_H - MARGIN - header_h
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = top_y - MARGIN

    try:
        reader = _downscaled_jpeg_reader(img_path, max_dim=max_dim)
        iw, ih = reader.getSize()
        scale = min(avail_w / iw, avail_h / ih)
        draw_w = iw * scale
        draw_h = ih * scale
        x = (PAGE_W - draw_w) / 2
        y = MARGIN + (avail_h - draw_h) / 2
        c.drawImage(
            reader, x, y, width=draw_w, height=draw_h,
            preserveAspectRatio=True, anchor="c",
        )
    except Exception as e:
        if strict:
            raise
        c.setFont(_FONT, 10)
        c.setFillColor(SUBTLE)
        c.drawString(MARGIN, PAGE_H / 2, f"[Could not render image: {e}]")

    c.showPage()
    c.save()
    return buf.getvalue()


def build_front_page_pdf_bytes(file_bytes: bytes, mime: str, filename: str = "") -> bytes:
    """
    Normalize a user-supplied front-page file into page bytes suitable for
    prepending to a databook. Accepts a PDF (returned as-is) or a raster image
    (wrapped into a single full-bleed page, no caption — the file is assumed
    to already be a designed cover/letterhead). Anything else raises
    ValueError with a message safe to show the user directly.
    """
    mime = (mime or "").lower()
    name = (filename or "").lower()
    is_pdf = mime == "application/pdf" or name.endswith(".pdf")
    is_image = mime.startswith("image/") or name.endswith((
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ))

    if is_pdf:
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            if len(reader.pages) == 0:
                raise ValueError("the PDF has no pages")
            reader.close()
        except Exception as e:
            raise ValueError(f"Could not read front page PDF: {e}")
        return file_bytes

    if is_image:
        _ensure_heif_support()
        try:
            with Image.open(io.BytesIO(file_bytes)) as im:
                iw, ih = im.size
                scale = min(PAGE_W / iw, PAGE_H / ih)
                draw_w, draw_h = iw * scale, ih * scale
                buf = io.BytesIO()
                c = canvas.Canvas(buf, pagesize=letter)
                c.drawImage(
                    ImageReader(im),
                    (PAGE_W - draw_w) / 2, (PAGE_H - draw_h) / 2,
                    width=draw_w, height=draw_h,
                    preserveAspectRatio=True, anchor="c",
                )
                c.showPage()
                c.save()
        except Exception as e:
            raise ValueError(f"Could not read front page image: {e}")
        return buf.getvalue()

    raise ValueError(
        f"Unsupported front page file type ({mime or name or 'unknown'}). "
        "Please provide a PDF or image file — export Word/Pages documents to PDF first."
    )


# ---------------------------------------------------------------------------
# Cover page + TOC + section header pages — built with reportlab platypus
# ---------------------------------------------------------------------------
def _build_cover_pdf(title: str, subtitle: str, sections: list[dict]) -> bytes:
    """Build the cover + TOC pages as a small PDF document."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=title or "Databook",
    )

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle(
        "DBTitle", parent=styles["Heading1"],
        fontName=_FONT_BOLD, fontSize=28, leading=34, textColor=black,
        spaceAfter=10, alignment=1,  # center
    )
    h_sub = ParagraphStyle(
        "DBSub", parent=styles["Normal"],
        fontName=_FONT, fontSize=14, leading=18, textColor=SUBTLE,
        spaceAfter=4, alignment=1,
    )
    h_kicker = ParagraphStyle(
        "DBKicker", parent=styles["Normal"],
        fontName=_FONT, fontSize=9, leading=11, textColor=ACCENT,
        alignment=1, spaceAfter=6,
    )
    h_meta = ParagraphStyle(
        "DBMeta", parent=styles["Normal"],
        fontName=_FONT, fontSize=10, leading=14, textColor=SUBTLE, alignment=1,
    )
    p_doc = ParagraphStyle(
        "DBDoc", parent=styles["Normal"],
        fontName=_FONT, fontSize=10, leading=14, textColor=black,
    )

    story: list = []

    # ----- Cover page -----
    story.append(Spacer(1, 1.4 * inch))
    story.append(Paragraph("ENGINEERING DATABOOK", h_kicker))
    story.append(Paragraph(title or "Untitled", h_title))
    if subtitle:
        story.append(Paragraph(subtitle, h_sub))
    story.append(Spacer(1, 0.6 * inch))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}", h_meta,
    ))
    total_docs = sum(len(s["docs"]) for s in sections)
    story.append(Paragraph(
        f"{len(sections)} section{'' if len(sections) == 1 else 's'} · "
        f"{total_docs} document{'' if total_docs == 1 else 's'}", h_meta,
    ))
    story.append(PageBreak())

    # ----- Table of contents -----
    story.append(Paragraph("Table of contents", h_title))
    story.append(Spacer(1, 0.25 * inch))

    toc_rows: list = []
    for sec in sections:
        for d in sec["docs"]:
            doc_sn = (d.get("sn") or sec.get("sn") or "").strip()
            sn_label = f"<font color='#78716c'>{_escape(doc_sn)}</font>" if doc_sn else ""
            toc_rows.append([
                Paragraph(_escape(d["name"]), p_doc),
                Paragraph(sn_label or f"<font color='#78716c'>{_escape(d['id'])}</font>", p_doc),
            ])

    if toc_rows:
        toc = Table(toc_rows, colWidths=[5.2 * inch, 1.8 * inch])
        toc.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(toc)

    doc.build(story)
    return buf.getvalue()


def _escape(s: str) -> str:
    """Minimal XML/HTML escape for reportlab Paragraph contents."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_to_portrait(page):
    """Rotate a landscape source page so it displays portrait.

    Bakes the rotation into the page's own content/media box (via
    transfer_rotation_to_content) rather than just flipping the /Rotate
    flag, so the header overlay in _make_header_overlay -- which is always
    drawn on a portrait letter canvas -- lands correctly regardless of the
    source PDF's original page orientation. No-op for already-portrait or
    square pages.
    """
    try:
        box = page.mediabox
        if float(box.width) > float(box.height):
            page.rotate(90)
            page.transfer_rotation_to_content()
    except Exception:
        pass  # leave the page as-is rather than fail the whole merge
    return page


# ---------------------------------------------------------------------------
# Header overlay — stamps the document's SN + page number at the top of
# every page (no folder/document names, per the databook's privacy design)
# ---------------------------------------------------------------------------
def _make_header_overlay(page_num: int, total_pages: int, sn: str) -> bytes:
    """Tiny single-page PDF that gets stamped over each merged page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(_FONT, 8)
    c.setFillColor(SUBTLE)

    # Rule near the top of the page
    line_y = PAGE_H - 0.45 * inch
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, line_y, PAGE_W - MARGIN, line_y)

    # SN (left), page number (right) — both in the header
    text_y = line_y + 0.12 * inch
    if sn:
        c.drawString(MARGIN, text_y, (sn or "")[:80])
    c.drawRightString(
        PAGE_W - MARGIN, text_y,
        f"Page {page_num} of {total_pages}",
    )
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main entry — build the databook
# ---------------------------------------------------------------------------
def build_databook(
    selection: list[dict],
    docs_dir: Path,
    doc_index: list[dict],
    tree: dict,
    title: str = "",
    subtitle: str = "",
    front_page_pdf_bytes: bytes | None = None,
    include_cover: bool = True,
    force_portrait: bool = False,
) -> bytes:
    """
    selection: ordered list of {nodeId, docIds: [str, ...]} representing the
               user's tree-order selection. Empty docIds lists are skipped.
    front_page_pdf_bytes: optional pre-normalized PDF page(s) (see
               build_front_page_pdf_bytes) inserted before the auto-generated
               cover/TOC page.
    include_cover: when False, skips the auto-generated "ENGINEERING
               DATABOOK" title + table-of-contents pages entirely -- the
               output starts directly with the first document's own pages.
               Per-document bookmarks (the PDF outline) are still added
               either way, so navigation doesn't depend on the cover page.
    force_portrait: when True, any source PDF page wider than it is tall is
               rotated to display portrait (see _normalize_to_portrait), so
               the whole merged output has a single, consistent orientation.
    Returns the assembled PDF as bytes.
    """
    _init_fonts()
    _ensure_heif_support()
    docs_by_id = {d["id"]: d for d in doc_index}

    # Build the structured "sections" list (skip empty selections)
    sections: list[dict] = []
    for entry in selection:
        node_id = entry.get("nodeId")
        doc_ids = [d for d in (entry.get("docIds") or []) if d in docs_by_id]
        if not doc_ids:
            continue
        node = _find_node(tree, node_id)
        node_path = _path_for_node(tree, node_id) or (node and node.get("name")) or node_id
        sections.append({
            "node_id": node_id,
            "node_name": node.get("name") if node else node_id,
            "sn": (node.get("sn") if node else "") or "",
            "path": node_path,
            "docs": [docs_by_id[d] for d in doc_ids],
        })

    if not sections:
        raise ValueError("No documents selected for the databook.")

    # ---- Phase 1: build all the per-document PDF byte blobs into a list ----
    # Track where each section starts (page number) for bookmarking.
    writer = PdfWriter()

    # 1a — optional user-supplied front page, inserted before the auto cover/TOC
    front_page_count = 0
    if front_page_pdf_bytes:
        front_reader = PdfReader(io.BytesIO(front_page_pdf_bytes))
        for p in front_reader.pages:
            if force_portrait:
                p = _normalize_to_portrait(p)
            writer.add_page(p)
        front_page_count = len(front_reader.pages)
        front_reader.close()

    # 1b — cover + TOC (skippable — see include_cover)
    cover_start_page = front_page_count
    cover_page_count = 0
    if include_cover:
        cover_bytes = _build_cover_pdf(title, subtitle, sections)
        cover_reader = PdfReader(io.BytesIO(cover_bytes))
        cover_page_count = len(cover_reader.pages)
        for p in cover_reader.pages:
            writer.add_page(p)
        cover_reader.close()

    # 1c — for each section: each document. No section divider pages are
    # inserted, and no folder/section names appear anywhere in the output —
    # only each document's own SN, stamped at the top of its pages.
    doc_starts: list[tuple[int, str, str]] = []  # (page_num, doc_name, doc_sn)

    for sec in sections:
        for doc in sec["docs"]:
            # Effective SN: doc's own SN if set, otherwise section's SN
            doc_sn = (doc.get("sn") or sec.get("sn") or "").strip()
            doc_starts.append((len(writer.pages), doc["name"], doc_sn))
            doc_path = _doc_path(docs_dir, doc["id"])
            if doc_path is None:
                # Missing file — insert a placeholder page
                placeholder = _build_missing_doc_pdf(doc)
                ph_reader = PdfReader(io.BytesIO(placeholder))
                for p in ph_reader.pages:
                    writer.add_page(p)
                ph_reader.close()
                continue

            mime = (doc.get("mime") or "").lower()
            # iPhone HEIC photos (and some other images) are frequently stored
            # with an empty or generic mime ("application/octet-stream"), which
            # used to drop them into the "Unsupported type" placeholder branch
            # even though Pillow (+ pillow-heif) can render them fine. Fall back
            # to the file extension — of the on-disk file or the document name —
            # whenever the mime isn't already a decisive pdf/image value.
            ext = (doc_path.suffix or Path(doc.get("name") or "").suffix).lower().lstrip(".")
            is_pdf = mime == "application/pdf" or (not mime.startswith("image/") and ext == "pdf")
            is_image = mime.startswith("image/") or (not is_pdf and ext in _IMAGE_EXTS)
            if is_pdf:
                try:
                    src = PdfReader(str(doc_path))
                    for p in src.pages:
                        if force_portrait:
                            p = _normalize_to_portrait(p)
                        writer.add_page(p)
                    src.close()
                except Exception:
                    placeholder = _build_missing_doc_pdf(doc, error="Could not read PDF")
                    ph_reader = PdfReader(io.BytesIO(placeholder))
                    for p in ph_reader.pages:
                        writer.add_page(p)
                    ph_reader.close()
            elif is_image:
                img_pdf = _image_to_pdf_bytes(doc_path, doc["name"])
                img_reader = PdfReader(io.BytesIO(img_pdf))
                for p in img_reader.pages:
                    writer.add_page(p)
                img_reader.close()
            else:
                placeholder = _build_missing_doc_pdf(doc, error=f"Unsupported type: {mime or ext or 'unknown'}")
                ph_reader = PdfReader(io.BytesIO(placeholder))
                for p in ph_reader.pages:
                    writer.add_page(p)
                ph_reader.close()

    total_pages = len(writer.pages)

    # ---- Phase 2: bookmarks (outline) — flat by document; no folder/section
    # names appear as bookmarks, only the document name and its SN.
    if front_page_count:
        writer.add_outline_item("Front Page", 0)
    if include_cover:
        writer.add_outline_item("Cover & Table of Contents", cover_start_page)
    for ds_page, ds_name, ds_sn in doc_starts:
        label = f"{ds_sn} · {ds_name}" if ds_sn else ds_name
        writer.add_outline_item(label, ds_page)

    # ---- Phase 3: stamp headers directly onto writer's pages (in-place) ----
    # We never serialize to an intermediate buffer — this avoids holding 3-5×
    # the output size in RAM simultaneously (the previous triple-copy pattern
    # was the cause of extreme memory usage on large databooks).

    # Build a flat page→SN lookup so headers show the right document's SN.
    page_owner: dict[int, str] = {}
    cover_pages = set(range(0, front_page_count + cover_page_count))

    for ds_page, ds_name, ds_sn in doc_starts:
        next_boundaries = sorted(
            [p for p, _, _ in doc_starts if p > ds_page]
            + [total_pages]
        )
        end = next_boundaries[0] if next_boundaries else total_pages
        for p in range(ds_page, end):
            page_owner[p] = ds_sn

    # Cache overlay PDFs by (page_num, sn) so we create at most one PdfReader
    # per unique header label rather than one per page.
    _overlay_cache: dict[tuple, object] = {}

    for i, page in enumerate(writer.pages):
        if i in cover_pages:
            continue  # leave cover/TOC pristine
        owner_sn = page_owner.get(i, "")
        cache_key = (i + 1, total_pages, owner_sn)
        if cache_key not in _overlay_cache:
            overlay_bytes = _make_header_overlay(i + 1, total_pages, owner_sn)
            _overlay_cache[cache_key] = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
        page.merge_page(_overlay_cache[cache_key])

    # Single serialisation — no intermediate copies.
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Per-document AI Q&A log — rendered as a PDF (see /api/docs/<id>/ask in
# dms_server.py). Unlike the merged-document flow above, this has no source
# PDF to merge; it's a plain platypus document, closer to _build_cover_pdf.
# ---------------------------------------------------------------------------
def build_qa_log_pdf(doc_name: str, entries: list[dict]) -> bytes:
    """Render a document's running AI Q&A history as a PDF.

    entries: [{"timestamp": str, "model": str, "question": str, "answer": str}, ...]
    in the order they should appear (oldest first). PDF isn't append-friendly,
    so the caller (post_doc_ask) keeps this entry list as the source of truth
    on the log document's docIndex entry and calls this to rebuild the whole
    file from scratch each time a new question is answered — the same
    "read the structured history, add one row, rewrite the file" pattern
    used for the Excel plot-snapshot history, just with a PDF renderer
    instead of openpyxl.
    """
    _init_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"AI Q&A — {doc_name}",
    )

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle(
        "QATitle", parent=styles["Normal"],
        fontName=_FONT_BOLD, fontSize=16, leading=20, textColor=black, spaceAfter=12,
    )
    h_meta = ParagraphStyle(
        "QAMeta", parent=styles["Normal"],
        fontName=_FONT, fontSize=9, leading=12, textColor=SUBTLE, spaceAfter=4,
    )
    p_q = ParagraphStyle(
        "QAQ", parent=styles["Normal"],
        fontName=_FONT_BOLD, fontSize=10.5, leading=15, textColor=black, spaceAfter=3,
    )
    p_a = ParagraphStyle(
        "QAA", parent=styles["Normal"],
        fontName=_FONT, fontSize=10.5, leading=15, textColor=black, spaceAfter=6,
    )

    def _para_text(s: str) -> str:
        # Escape first, then turn newlines into <br/> -- Paragraph markup
        # doesn't treat "\n" as a line break on its own.
        return _escape(s).replace("\n", "<br/>")

    story: list = [Paragraph(_escape(f"AI Q&A — {doc_name}"), h_title)]

    if not entries:
        story.append(Paragraph("(no questions asked yet)", h_meta))
    for i, e in enumerate(entries):
        if i > 0:
            story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceBefore=6, spaceAfter=10))
        meta_bits = " · ".join(filter(None, [e.get("timestamp", ""), e.get("model", "")]))
        story.append(Paragraph(_escape(meta_bits), h_meta))
        story.append(Paragraph("Q: " + _para_text(e.get("question", "")), p_q))
        story.append(Paragraph("A: " + _para_text(e.get("answer", "")), p_a))

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Folder-level "Ask AI" — request cover page (see /api/folder-ai-pdf in
# dms_server.py). A single page carrying the user's free-form request,
# prepended to the merged selected documents so the whole PDF can be handed
# to an external tool (ChatGPT, Claude, ...) as one file.
# ---------------------------------------------------------------------------
def build_prompt_cover_pdf(request_text: str, folder_name: str = "",
                           doc_count: int = 0) -> bytes:
    """Render the request page that leads a folder-level Ask-AI PDF.

    request_text: the user's instruction/question, shown verbatim.
    folder_name:  shown as a small subheading for context.
    doc_count:    how many document pages follow -- shown as a one-line note
                  so the reader knows to keep scrolling for the images
                  themselves (no filename list -- the pages carry the images).
    """
    _init_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title="Request",
    )

    styles = getSampleStyleSheet()
    h_kicker = ParagraphStyle(
        "PCKicker", parent=styles["Normal"],
        fontName=_FONT_BOLD, fontSize=9, leading=12, textColor=SUBTLE, spaceAfter=2,
    )
    h_title = ParagraphStyle(
        "PCTitle", parent=styles["Normal"],
        fontName=_FONT_BOLD, fontSize=17, leading=22, textColor=black, spaceAfter=14,
    )
    p_body = ParagraphStyle(
        "PCBody", parent=styles["Normal"],
        fontName=_FONT, fontSize=11.5, leading=17, textColor=black, spaceAfter=8,
    )
    h_meta = ParagraphStyle(
        "PCMeta", parent=styles["Normal"],
        fontName=_FONT, fontSize=9, leading=13, textColor=SUBTLE, spaceAfter=3,
    )

    def _para_text(s: str) -> str:
        return _escape(s).replace("\n", "<br/>")

    story: list = [Paragraph("REQUEST / 请求", h_kicker)]
    story.append(Paragraph(_escape(folder_name) if folder_name else "Ask AI", h_title))
    story.append(Paragraph(_para_text(request_text or "(no request text)"), p_body))

    if doc_count:
        story.append(Spacer(1, 0.35 * inch))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))
        story.append(Paragraph(
            f"The {doc_count} document(s) referenced by this request follow on "
            f"the next pages. / 本请求涉及的 {doc_count} 份文档见后续页面。",
            h_meta))

    doc.build(story)
    return buf.getvalue()


def _exif_datetime_original(img_path: Path) -> str:
    """Return the photo's 'date taken' from EXIF (YYYY-MM-DD HH:MM:SS), or ""."""
    try:
        with Image.open(str(img_path)) as im:
            exif = im.getexif()
            if not exif:
                return ""
            # 36867 DateTimeOriginal (in the Exif sub-IFD), 306 DateTime (root)
            val = ""
            try:
                sub = exif.get_ifd(0x8769)
                val = sub.get(36867) or sub.get(36868) or ""
            except Exception:
                pass
            val = val or exif.get(306) or ""
            val = str(val).strip()
            if len(val) >= 10 and val[4] == ":" and val[7] == ":":
                return val[:10].replace(":", "-") + val[10:]
            return val
    except Exception:
        return ""


def _photo_meta_lines(entry: dict, img_path: Path) -> list:
    """Small caption block for an image page: date taken, location, and any
    description / SN / key parameters DMS has on the document."""
    lines: list = []

    taken = _exif_datetime_original(img_path) or (entry.get("docDate") or "")
    if taken:
        lines.append(f"Date taken / 拍摄日期: {taken}")

    meta = entry.get("metadata") or {}
    place = ""
    loc = meta.get("Location")
    if isinstance(loc, dict):
        place = (loc.get("actual") or loc.get("design") or "").strip()
    elif isinstance(loc, str):
        place = loc.strip()
    lat, lon = entry.get("lat"), entry.get("lon")
    loc_bits = [b for b in [place, (f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else "")] if b]
    if loc_bits:
        lines.append("Location / 位置: " + "  ".join(loc_bits))

    if (entry.get("sn") or "").strip():
        lines.append(f"SN: {entry['sn'].strip()}")
    if (entry.get("description") or "").strip():
        lines.append("Note: " + " ".join(entry["description"].split()))

    for k, v in meta.items():
        if k == "Location":
            continue
        if isinstance(v, dict):
            v = (v.get("actual") or v.get("design") or "")
        v = str(v).strip()
        if v:
            lines.append(f"{k}: {v}")

    return lines[:8]


def build_ask_ai_pdf(request_text: str, folder_name: str,
                     docs: list[dict], docs_dir: "Path") -> bytes:
    """Assemble the folder-level Ask-AI PDF: a request cover page, then the
    actual content of each selected document.

    docs: ordered list of docIndex entries (id/name/mime plus whatever
          metadata DMS has -- docDate, lat/lon, metadata.Location,
          description, sn, ...). PDFs are merged page-for-page; images
          (JPG/PNG/HEIC/...) are rendered onto their own page, captioned
          with date taken / location / notes, so ChatGPT/Claude gets that
          context too; anything else gets a one-line placeholder page.

    Unlike build_databook this does its own image handling via Pillow (with
    HEIC registered), so a phone photo always lands as a visible picture,
    never a "[could not render]" line.
    """
    _init_fonts()
    _ensure_heif_support()

    writer = PdfWriter()

    cover = build_prompt_cover_pdf(request_text, folder_name, doc_count=len(docs))
    cr = PdfReader(io.BytesIO(cover))
    for p in cr.pages:
        writer.add_page(p)
    cr.close()

    for d in docs:
        path = _doc_path(docs_dir, d["id"])
        name = d.get("name") or d["id"]
        if path is None or not path.exists():
            ph = PdfReader(io.BytesIO(_build_missing_doc_pdf(d)))
            for p in ph.pages:
                writer.add_page(p)
            ph.close()
            continue

        ext = (path.suffix or Path(name).suffix).lower().lstrip(".")
        mime = (d.get("mime") or "").lower()
        is_pdf = mime == "application/pdf" or (not mime.startswith("image/") and ext == "pdf")

        if is_pdf:
            try:
                src = PdfReader(str(path))
                for p in src.pages:
                    writer.add_page(_normalize_to_portrait(p))
                src.close()
                continue
            except Exception:
                pass  # fall through to placeholder

        # Treat everything else Pillow can open as an image (covers HEIC, and
        # phone photos stored with an empty/generic mime). Embed at a higher
        # resolution than a bulk databook would -- this PDF exists to be read
        # by another AI, so photo detail/legibility matters more than size.
        try:
            img_pdf = _image_to_pdf_bytes(
                path, name, strict=True, max_dim=1600,
                meta_lines=_photo_meta_lines(d, path))
            ir = PdfReader(io.BytesIO(img_pdf))
            for p in ir.pages:
                writer.add_page(p)
            ir.close()
            continue
        except Exception:
            pass

        ph = PdfReader(io.BytesIO(_build_missing_doc_pdf(
            d, error=f"Unsupported type: {mime or ext or 'unknown'}")))
        for p in ph.pages:
            writer.add_page(p)
        ph.close()

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _build_missing_doc_pdf(doc: dict, error: str = "File not found on disk") -> bytes:
    """One-page placeholder when a referenced document can't be embedded."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(_FONT_BOLD, 14)
    c.setFillColor(black)
    c.drawString(MARGIN, PAGE_H - MARGIN - 30, doc.get("name", "(unnamed document)"))
    c.setFont(_FONT, 10)
    c.setFillColor(SUBTLE)
    c.drawString(MARGIN, PAGE_H - MARGIN - 50, f"ID: {doc.get('id', '')}")
    c.setFont(_FONT_BOLD, 12)
    c.setFillColor(HexColor("#b91c1c"))  # red-700
    c.drawString(MARGIN, PAGE_H / 2, "⚠  " + error)
    c.setFont(_FONT, 9)
    c.setFillColor(SUBTLE)
    c.drawString(
        MARGIN, PAGE_H / 2 - 20,
        "This page is a placeholder. The original file could not be included.",
    )
    c.showPage()
    c.save()
    return buf.getvalue()

"""
Databook generation — assembles selected documents from the DMS into a single
deliverable PDF with cover page, table of contents, section headers per node,
bookmarks, and page-number/document-name footers on every page.

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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
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

    Files are stored as '<DOC-ID>__<original-name>.<ext>', so we need a
    prefix glob rather than an extension-only glob.
    """
    matches = list(docs_dir.glob(f"{doc_id}*"))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Image → single-PDF-page conversion (Pillow)
# ---------------------------------------------------------------------------
def _image_to_pdf_bytes(img_path: Path, caption: str) -> bytes:
    """Convert an image into a single-page PDF using reportlab, preserving aspect."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Caption at top
    c.setFont(_FONT_BOLD, 11)
    c.setFillColor(black)
    c.drawString(MARGIN, PAGE_H - MARGIN, caption[:90])

    # Compute fit area below caption
    top_y = PAGE_H - MARGIN - 24
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = top_y - MARGIN

    try:
        with Image.open(img_path) as im:
            iw, ih = im.size
            scale = min(avail_w / iw, avail_h / ih)
            draw_w = iw * scale
            draw_h = ih * scale
            x = (PAGE_W - draw_w) / 2
            y = MARGIN + (avail_h - draw_h) / 2
            c.drawImage(
                str(img_path), x, y, width=draw_w, height=draw_h,
                preserveAspectRatio=True, anchor="c",
            )
    except Exception as e:
        c.setFont(_FONT, 10)
        c.setFillColor(SUBTLE)
        c.drawString(MARGIN, PAGE_H / 2, f"[Could not render image: {e}]")

    c.showPage()
    c.save()
    return buf.getvalue()


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
    h_section = ParagraphStyle(
        "DBSection", parent=styles["Heading2"],
        fontName=_FONT_BOLD, fontSize=14, leading=18, textColor=ACCENT, spaceBefore=10, spaceAfter=4,
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
        sn_part = f" &nbsp;<font color='#b45309'>· SN {_escape(sec['sn'])}</font>" if sec.get("sn") else ""
        toc_rows.append([Paragraph(f"<b>{_escape(sec['path'])}</b>{sn_part}", p_doc), ""])
        for d in sec["docs"]:
            doc_sn = (d.get("sn") or sec.get("sn") or "").strip()
            sn_label = f"<font color='#78716c'>{_escape(doc_sn)}</font>" if doc_sn else ""
            toc_rows.append([
                Paragraph(
                    "&nbsp;&nbsp;&nbsp;&nbsp;" + _escape(d["name"]),
                    p_doc,
                ),
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


def _build_section_header_pdf(node_path: str, node_id: str, node_sn: str, doc_count: int) -> bytes:
    """One-page divider that introduces a node's section."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Accent stripe
    c.setFillColor(ACCENT)
    c.rect(MARGIN, PAGE_H - MARGIN - 4, 60, 4, fill=1, stroke=0)

    c.setFont(_FONT, 8)
    c.setFillColor(ACCENT)
    c.drawString(MARGIN, PAGE_H - MARGIN - 30, "SECTION")

    # Path / title
    c.setFillColor(black)
    c.setFont(_FONT_BOLD, 22)
    parts = node_path.split(" / ") if node_path else ["(unnamed)"]
    y = PAGE_H - MARGIN - 70
    for i, part in enumerate(parts):
        c.setFont(_FONT_BOLD, 22 if i == len(parts) - 1 else 14)
        c.setFillColor(black if i == len(parts) - 1 else SUBTLE)
        c.drawString(MARGIN + (i * 12), y, part)
        y -= 30 if i == len(parts) - 1 else 22

    # SN badge (prominent if set)
    if node_sn:
        y -= 10
        c.setFont(_FONT_BOLD, 9)
        c.setFillColor(ACCENT)
        c.drawString(MARGIN, y, "SERIAL NUMBER")
        c.setFont(_FONT, 16)
        c.setFillColor(black)
        c.drawString(MARGIN, y - 22, node_sn)

    # Footer info
    c.setFont(_FONT, 9)
    c.setFillColor(SUBTLE)
    c.drawString(MARGIN, MARGIN + 20, f"Node ID: {node_id}")
    c.drawString(MARGIN, MARGIN, f"{doc_count} document{'' if doc_count == 1 else 's'} in this section")

    c.showPage()
    c.save()
    return buf.getvalue()


def _escape(s: str) -> str:
    """Minimal XML/HTML escape for reportlab Paragraph contents."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Footer overlay — adds page number + document name to every page
# ---------------------------------------------------------------------------
def _make_footer_overlay(page_num: int, total_pages: int, doc_name: str) -> bytes:
    """Tiny single-page PDF that gets stamped underneath each merged page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(_FONT, 8)
    c.setFillColor(SUBTLE)

    # Top thin line + bottom rule
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, 0.45 * inch, PAGE_W - MARGIN, 0.45 * inch)

    # Document name (left), page number (right) — both in footer
    name = (doc_name or "")[:80]
    c.drawString(MARGIN, 0.30 * inch, name)
    c.drawRightString(
        PAGE_W - MARGIN, 0.30 * inch,
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
) -> bytes:
    """
    selection: ordered list of {nodeId, docIds: [str, ...]} representing the
               user's tree-order selection. Empty docIds lists are skipped.
    Returns the assembled PDF as bytes.
    """
    _init_fonts()
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

    # 1a — cover + TOC
    cover_bytes = _build_cover_pdf(title, subtitle, sections)
    cover_reader = PdfReader(io.BytesIO(cover_bytes))
    cover_start_page = 0
    cover_page_count = len(cover_reader.pages)
    for p in cover_reader.pages:
        writer.add_page(p)
    cover_reader.close()

    # 1b — for each section: header page + each document
    section_starts: list[int] = []
    doc_starts: list[tuple[int, str, str, str]] = []  # (page_num, doc_name, section_path, doc_sn)

    for sec in sections:
        section_starts.append(len(writer.pages))
        # Section header
        hdr = _build_section_header_pdf(sec["path"], sec["node_id"], sec["sn"], len(sec["docs"]))
        hdr_reader = PdfReader(io.BytesIO(hdr))
        for p in hdr_reader.pages:
            writer.add_page(p)
        hdr_reader.close()

        # Documents in this section
        for doc in sec["docs"]:
            # Effective SN: doc's own SN if set, otherwise section's SN
            doc_sn = (doc.get("sn") or sec.get("sn") or "").strip()
            doc_starts.append((len(writer.pages), doc["name"], sec["path"], doc_sn))
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
            if mime == "application/pdf":
                try:
                    src = PdfReader(str(doc_path))
                    for p in src.pages:
                        writer.add_page(p)
                    src.close()
                except Exception:
                    placeholder = _build_missing_doc_pdf(doc, error="Could not read PDF")
                    ph_reader = PdfReader(io.BytesIO(placeholder))
                    for p in ph_reader.pages:
                        writer.add_page(p)
                    ph_reader.close()
            elif mime.startswith("image/"):
                img_pdf = _image_to_pdf_bytes(doc_path, doc["name"])
                img_reader = PdfReader(io.BytesIO(img_pdf))
                for p in img_reader.pages:
                    writer.add_page(p)
                img_reader.close()
            else:
                placeholder = _build_missing_doc_pdf(doc, error=f"Unsupported type: {mime}")
                ph_reader = PdfReader(io.BytesIO(placeholder))
                for p in ph_reader.pages:
                    writer.add_page(p)
                ph_reader.close()

    total_pages = len(writer.pages)

    # ---- Phase 2: bookmarks (outline) ----
    cover_bm = writer.add_outline_item("Cover & Table of Contents", cover_start_page)
    for sec, start in zip(sections, section_starts):
        sec_label = f"{sec['path']} · SN {sec['sn']}" if sec.get("sn") else sec["path"]
        sec_bm = writer.add_outline_item(sec_label, start)
        # Each doc within the section gets a sub-bookmark
        for doc in sec["docs"]:
            for ds_page, ds_name, ds_path, _ds_sn in doc_starts:
                if ds_path == sec["path"] and ds_name == doc["name"]:
                    writer.add_outline_item(ds_name, ds_page, parent=sec_bm)
                    break

    # ---- Phase 3: stamp footers directly onto writer's pages (in-place) ----
    # We never serialize to an intermediate buffer — this avoids holding 3-5×
    # the output size in RAM simultaneously (the previous triple-copy pattern
    # was the cause of extreme memory usage on large databooks).

    # Build a flat page→owner lookup so footers show the right document name.
    page_owner: dict[int, str] = {}
    cover_pages = set(range(cover_start_page, cover_start_page + cover_page_count))

    for sec, start in zip(sections, section_starts):
        page_owner[start] = sec["path"]
    for ds_page, ds_name, ds_path, ds_sn in doc_starts:
        next_boundaries = sorted(
            [p for p, _, _, _ in doc_starts if p > ds_page]
            + [s for s in section_starts if s > ds_page]
            + [total_pages]
        )
        end = next_boundaries[0] if next_boundaries else total_pages
        footer_text = f"{ds_sn} · {ds_name}" if ds_sn else ds_name
        for p in range(ds_page, end):
            page_owner[p] = footer_text

    # Cache overlay PDFs by (page_num, owner_name) so we create at most one
    # PdfReader per unique footer label rather than one per page.
    _overlay_cache: dict[tuple, object] = {}

    for i, page in enumerate(writer.pages):
        if i in cover_pages:
            continue  # leave cover/TOC pristine
        owner_name = page_owner.get(i, "")
        cache_key = (i + 1, total_pages, owner_name)
        if cache_key not in _overlay_cache:
            overlay_bytes = _make_footer_overlay(i + 1, total_pages, owner_name)
            _overlay_cache[cache_key] = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
        page.merge_page(_overlay_cache[cache_key])

    # Single serialisation — no intermediate copies.
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

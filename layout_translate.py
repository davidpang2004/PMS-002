"""Layout-preserving PDF translation.

Instead of flowing a page's translation into plain paragraphs, find every text
unit (a table cell, a heading, a wrapped paragraph...) on the ORIGINAL page with
its position, translate each unit, then paint the translation back into the same
rectangle of a copy of the original page (the old text is wiped with the local
background colour first; lines, shading and images are left alone). Tables
therefore keep their rows and columns.

Works on PDFs with a real text layer. Scanned/image-only pages have nothing to
locate and are copied through unchanged (reported via `blank_pages`).
"""
import html as _html
import json
import re

EMBED_NAME = "dms-layout.json"
_LETTER = re.compile(r"[^\W\d_]", re.U)
_CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
_ENDS_SENTENCE = re.compile(r"[.:;!?。：；！？]\s*$")


def _hex(c: int) -> str:
    return "#%06x" % (int(c) & 0xFFFFFF)


def _needs_translation(text: str, target_has_cjk: bool) -> bool:
    if not _LETTER.search(text):
        return False                      # numbers / symbols only
    if target_has_cjk and _CJK.search(text):
        return False                      # already in the target script
    return True


def _h_rules(page) -> list:
    """Horizontal rules on the page (table row lines, cell/box top & bottom
    edges): [(y, x0, x1)]. Text lines on opposite sides of one are different
    cells/rows and must never be merged into one paragraph."""
    out = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":
                    (ax, ay), (bx, by) = it[1], it[2]
                    if abs(ay - by) < 1:
                        out.append(((ay + by) / 2, min(ax, bx), max(ax, bx)))
                elif it[0] in ("re", "qu"):
                    r = it[1].rect if it[0] == "qu" else it[1]
                    if r.width < 3:
                        continue
                    if r.height < 1.5:
                        out.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                    else:
                        out.append((r.y0, r.x0, r.x1))
                        out.append((r.y1, r.x0, r.x1))
    except Exception:
        pass
    return out


def extract_units(page) -> list:
    """-> [{rect:[x0,y0,x1,y1], text, size, color, bold}] for one page."""
    d = page.get_text("dict")
    rules = _h_rules(page)
    segs = []                              # one per text run on a line, reading order
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            spans.sort(key=lambda s: s["bbox"][0])
            cur = None
            for s in spans:
                size = s.get("size", 10) or 10
                bold = bool(s.get("flags", 0) & 16) or "bold" in (s.get("font", "").lower())
                if cur and (s["bbox"][0] - cur["rect"][2]) <= 0.55 * size:
                    cur["text"] += s["text"]
                    cur["rect"][2] = max(cur["rect"][2], s["bbox"][2])
                    cur["rect"][1] = min(cur["rect"][1], s["bbox"][1])
                    cur["rect"][3] = max(cur["rect"][3], s["bbox"][3])
                else:
                    if cur:
                        segs.append(cur)
                    cur = {"rect": list(s["bbox"]), "text": s["text"], "size": size,
                           "color": s.get("color", 0), "bold": bold, "line": id(ln)}
            if cur:
                segs.append(cur)
    # Merge wrapped lines of one paragraph / table cell: the next line sits
    # directly under an earlier unit, same column and style, and that unit
    # didn't end a sentence. Searches back a little because the lines of
    # neighbouring table columns interleave in reading order.
    merged = []
    for sg in segs:
        hit = None
        for prev in reversed(merged[-40:]):
            if sg["line"] == prev["line"]:
                continue
            pr, r, size = prev["rect"], sg["rect"], prev["size"]
            ov = min(pr[2], r[2]) - max(pr[0], r[0])
            if (abs(sg["size"] - size) < 0.6 and sg["bold"] == prev["bold"]
                    and -0.5 * size <= r[1] - prev["last_bottom"] < 0.6 * size
                    and ov > 0.5 * min(pr[2] - pr[0], r[2] - r[0])
                    and abs(r[0] - pr[0]) < 1.5 * size
                    and not _ENDS_SENTENCE.search(prev["text"])
                    and not sg["text"][:1].isupper()      # a capital starts a new row/sentence
                    and not sg["text"][:1].isdigit()
                    and not any(prev["last_bottom"] - 1 <= y <= r[1] + 1
                                and min(x1, pr[2], r[2]) - max(x0, pr[0], r[0]) > 0.3 * (r[2] - r[0])
                                for y, x0, x1 in rules)):
                hit = prev
                break
        if hit:
            pr, r = hit["rect"], sg["rect"]
            hit["text"] += " " + sg["text"]
            hit["rect"] = [min(pr[0], r[0]), pr[1], max(pr[2], r[2]), max(pr[3], r[3])]
            hit["last_bottom"] = r[3]
            hit["line"] = sg["line"]
        else:
            sg["last_bottom"] = sg["rect"][3]
            merged.append(sg)
    units = []
    for sg in merged:
        text = re.sub(r"\s+", " ", sg["text"]).strip()
        if text:
            units.append({"rect": [round(v, 2) for v in sg["rect"]], "text": text,
                          "size": round(sg["size"], 2), "color": sg["color"],
                          "bold": sg["bold"]})
    return units


def _lum(rgb):
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def _bg_color(pix, rect, text_color=None):
    """Background colour just left/right of the rect (at several heights, so a
    shaded cell shorter than the text box is still sampled); most common wins.
    The samples often land on a table's cell border (black lines); painting the
    text box with that colour blacks the cell out, so when the text colour is
    known, only samples that contrast with it (light bg for dark text, dark bg
    for light text) are considered."""
    w, h = pix.width, pix.height
    pts = []
    hh = rect[3] - rect[1]
    for fy in (0.3, 0.5, 0.7):
        y = rect[1] + hh * fy
        for x in (rect[0] - 1.5, rect[2] + 1.5):
            xi = min(max(int(x), 0), w - 1)
            yi = min(max(int(y), 0), h - 1)
            try:
                pts.append(tuple(pix.pixel(xi, yi))[:3])
            except Exception:
                pass
    if text_color is not None and pts:
        tc = int(text_color) & 0xFFFFFF
        text_lum = _lum(((tc >> 16) & 255, (tc >> 8) & 255, tc & 255))
        ok = [p for p in pts if abs(_lum(p) - text_lum) >= 0.4]
        pts = ok            # may be empty -> plain white below
    if not pts:
        return (1, 1, 1)
    # ties go to the lighter colour (a border line is never the cell fill)
    best = max(set(pts), key=lambda c: (pts.count(c), _lum(c)))
    return tuple(c / 255 for c in best)


def render(src_path, pages_units: dict, markup_to_html) -> bytes:
    """Open the source PDF, replace each unit's text by unit['text'] (a
    translation that may contain [[y:..]] colour markup) and return the
    new PDF's bytes. `pages_units` = {page_no(1-based): [unit, ...]};
    units with a falsy 'text' are left as the original."""
    import pymupdf
    doc = pymupdf.open(str(src_path))
    try:
        for pno in range(1, len(doc) + 1):
            units = [u for u in pages_units.get(pno, []) if u.get("text")]
            page = doc[pno - 1]
            if not units:
                continue
            pix = page.get_pixmap(dpi=72, alpha=False)
            for u in units:
                r = pymupdf.Rect(u["rect"])
                inset = 0.1 * r.height          # text boxes are taller than the glyphs
                page.add_redact_annot(r + (-0.3, inset, 0.3, -inset),
                                      fill=_bg_color(pix, u["rect"], u.get("color")))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            for u in units:
                r = pymupdf.Rect(u["rect"])
                body = markup_to_html(u["text"])
                css = (f"* {{margin:0;padding:0;font-family:sans-serif;font-size:{u['size']}pt;line-height:1.15;"
                       f"color:{_hex(u['color'])};"
                       f"font-weight:{'bold' if u['bold'] else 'normal'};}}")
                box = r + (-0.5, -0.5, 0.5, 0.5)
                spare, _ = page.insert_htmlbox(box, body, css=css, scale_low=0.3)
                if spare < 0:       # didn't fit even shrunk: let it use extra room below
                    page.insert_htmlbox(pymupdf.Rect(box.x0, box.y0, min(page.rect.x1 - 4, box.x1 + box.width * 0.5),
                                                     min(page.rect.y1 - 4, box.y1 + box.height * 2)),
                                        body, css=css, scale_low=0.3)
        out = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    return out


def embed_layout(pdf_bytes: bytes, source_id: str, pages_units: dict) -> bytes:
    """Attach the unit list (+ the original's doc id) so the translation can be
    edited and re-rendered later."""
    import pymupdf
    payload = {"source_id": source_id,
               "pages": {str(k): v for k, v in pages_units.items()}}
    doc = pymupdf.open("pdf", pdf_bytes)
    try:
        doc.embfile_add(EMBED_NAME, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


def read_layout(path):
    """-> payload dict or None (not a layout translation)."""
    import pymupdf
    doc = pymupdf.open(str(path))
    try:
        if EMBED_NAME in doc.embfile_names():
            return json.loads(doc.embfile_get(EMBED_NAME).decode("utf-8"))
    except Exception:
        return None
    finally:
        doc.close()
    return None


def build_translated_units(src_path, translate_batch, target_has_cjk: bool):
    """Extract units from every page of src_path and translate the ones that
    need it. `translate_batch(list[str]) -> list[str]` (same length).
    -> (pages_units, blank_pages): each unit carries 'src' (original text) and
    'text' (translation, '' when left as is)."""
    import pymupdf
    doc = pymupdf.open(str(src_path))
    pages_units, flat, blank = {}, [], 0
    try:
        for i, page in enumerate(doc, start=1):
            units = extract_units(page)
            if not units:
                blank += 1
            for u in units:
                u["src"] = u["text"]
                if _needs_translation(u["text"], target_has_cjk):
                    flat.append(u)
                else:
                    u["text"] = ""
            pages_units[i] = units
    finally:
        doc.close()
    if flat:
        outs = translate_batch([u["src"] for u in flat])
        for u, t in zip(flat, outs):
            u["text"] = (t or "").strip()
    return pages_units, blank

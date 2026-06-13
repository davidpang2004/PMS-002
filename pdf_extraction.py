"""
PDF text extraction and parameter discovery for the DMS.

Three stages of extraction, in order of cost:

  1. pypdf text extraction — instant, free, works for any text-based PDF.
     Most engineering documents (datasheets, MTRs from modern systems, exports
     from CAD/CAM tools) have a real text layer and need nothing else.

  2. OCR via Tesseract — slow (~2-10s/page), free, only needed for scanned/image PDFs.
     We detect whether OCR is needed (text layer is empty/sparse) and only run
     it then, and only if Tesseract is actually installed on the user's system.

  3. Parameter extraction via regex — given a body of text, look for engineering
     parameters the user cares about (Yield Strength, Hardness, etc.) and pull
     out their values. Conservative — we'd rather miss a value than pull a wrong
     one.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Tesseract availability detection
# ---------------------------------------------------------------------------
_TESSERACT_PATH: Optional[str] = None
_TESSERACT_CHECKED: bool = False
_TESSERACT_LANGS: Optional[list] = None
_OCR_LANG_STRING: Optional[str] = None


def tesseract_path() -> Optional[str]:
    """Return the path to Tesseract if installed, or None.

    Cached after first call so we don't pay the 'which' lookup repeatedly.
    On Mac, also checks Homebrew's typical install paths since 'shutil.which'
    sometimes misses them depending on the launching environment.
    """
    global _TESSERACT_PATH, _TESSERACT_CHECKED
    if _TESSERACT_CHECKED:
        return _TESSERACT_PATH

    _TESSERACT_CHECKED = True

    # First: standard PATH lookup
    found = shutil.which("tesseract")
    if found:
        _TESSERACT_PATH = found
        return found

    # Mac fallback paths (Homebrew on Intel and Apple Silicon)
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _TESSERACT_PATH = candidate
            return candidate

    # Windows fallback paths. The UB-Mannheim installer offers both a
    # machine-wide install (Program Files) and a per-user install (LocalAppData),
    # and may not add tesseract to PATH — so we check the common locations.
    win_candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            win_candidates.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
            win_candidates.append(os.path.join(base, "Programs", "Tesseract-OCR", "tesseract.exe"))
    for candidate in win_candidates:
        if os.path.isfile(candidate):
            _TESSERACT_PATH = candidate
            return candidate

    return None


def tesseract_available() -> bool:
    return tesseract_path() is not None


def tesseract_version() -> Optional[str]:
    """Return the Tesseract version string (or None if not installed)."""
    p = tesseract_path()
    if not p:
        return None
    try:
        result = subprocess.run(
            [p, "--version"], capture_output=True, text=True, timeout=5,
        )
        # Tesseract prints version on first line of stderr
        first_line = (result.stderr or result.stdout).splitlines()[0]
        return first_line.strip()
    except (subprocess.TimeoutExpired, OSError, IndexError):
        return None


def tesseract_languages() -> list:
    """Return the list of language codes Tesseract has installed (cached).

    e.g. ['eng', 'chi_sim', 'chi_tra', 'osd']. Empty list if none/unknown.
    """
    global _TESSERACT_LANGS
    if _TESSERACT_LANGS is not None:
        return _TESSERACT_LANGS
    _TESSERACT_LANGS = []
    p = tesseract_path()
    if not p:
        return _TESSERACT_LANGS
    try:
        result = subprocess.run(
            [p, "--list-langs"], capture_output=True, text=True, timeout=8,
        )
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        langs = []
        for line in out.splitlines():
            s = line.strip()
            # Skip the header line ("List of available languages ...") and blanks
            if not s or " " in s or ":" in s:
                continue
            langs.append(s)
        _TESSERACT_LANGS = langs
    except (subprocess.TimeoutExpired, OSError):
        _TESSERACT_LANGS = []
    return _TESSERACT_LANGS


def ocr_languages() -> str:
    """Build the Tesseract -l language string to use for OCR (cached).

    Priority:
      1. DMS_OCR_LANG env var, if set (e.g. "eng+chi_sim") — full manual control.
      2. Otherwise, auto-build from installed languages: always include English
         if present, plus Simplified and Traditional Chinese when installed, so
         mixed English/Chinese engineering documents read correctly.
      3. Fall back to "eng" if detection finds nothing usable.
    """
    global _OCR_LANG_STRING
    if _OCR_LANG_STRING is not None:
        return _OCR_LANG_STRING

    override = os.environ.get("DMS_OCR_LANG", "").strip()
    if override:
        _OCR_LANG_STRING = override
        return _OCR_LANG_STRING

    installed = set(tesseract_languages())
    # Preferred order: English first (engineering labels), then Chinese variants.
    preferred = ["eng", "chi_sim", "chi_tra"]
    chosen = [l for l in preferred if l in installed]
    if not chosen:
        # No preferred packs found; if any non-osd language exists, use the
        # first one, else default to eng (Tesseract errors clearly if missing).
        non_osd = [l for l in installed if l != "osd"]
        chosen = [non_osd[0]] if non_osd else ["eng"]
    _OCR_LANG_STRING = "+".join(chosen)
    return _OCR_LANG_STRING
# Tesseract's default page segmentation can read a page COLUMN-by-column
# (all rows of the left column, then the right column), which separates a
# label from the value sitting beside it. Engineering certificates are almost
# always meant to be read ROW-by-row. To guarantee row order regardless of how
# Tesseract groups blocks, we ask for TSV output (every word with its x/y
# pixel position) and rebuild the lines ourselves: cluster words by vertical
# position into rows, then sort each row left-to-right. This both fixes the
# reading order and keeps each label on the same line as its value.

def _reconstruct_rows_from_tsv(tsv_text: str) -> str:
    """Turn Tesseract TSV output into row-major text using word coordinates."""
    import csv
    words = []
    try:
        reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
        for r in reader:
            txt = (r.get("text") or "").strip()
            if not txt:
                continue
            try:
                if int(r.get("level", "0")) != 5:   # 5 = word-level row
                    continue
                conf = float(r.get("conf", "-1"))
                left = int(r["left"]); top = int(r["top"])
                width = int(r["width"]); height = int(r["height"])
            except (ValueError, KeyError, TypeError):
                continue
            if conf < 0:        # -1 marks a non-text/empty box
                continue
            words.append({"left": left, "top": top, "width": width,
                          "height": height, "text": txt})
    except Exception:
        return ""

    if not words:
        return ""

    # Threshold for "same row": a fraction of the median word height. Rows in a
    # form are normally spaced more than this apart, so they stay distinct.
    heights = sorted(w["height"] for w in words)
    med_h = heights[len(heights) // 2] or 12
    thresh = max(6.0, med_h * 0.6)

    # Greedy vertical clustering. Process words top-to-bottom; assign each to an
    # existing row whose mean top is within threshold, else start a new row.
    words.sort(key=lambda w: (w["top"], w["left"]))
    rows: list[dict] = []
    for w in words:
        placed = False
        for row in rows:
            if abs(w["top"] - row["top_mean"]) <= thresh:
                row["words"].append(w)
                row["top_sum"] += w["top"]
                row["top_mean"] = row["top_sum"] / len(row["words"])
                placed = True
                break
        if not placed:
            rows.append({"words": [w], "top_sum": w["top"], "top_mean": w["top"]})

    rows.sort(key=lambda r: r["top_mean"])
    lines = []
    for row in rows:
        ws = sorted(row["words"], key=lambda w: w["left"])
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


def _ocr_image_file(img_path: str, timeout: int = 120) -> str:
    """OCR an image file and return text in row-major order.

    Uses Tesseract TSV output + coordinate-based line reconstruction so the
    result reads left-to-right, top-to-bottom even on multi-column layouts.
    Falls back to plain text output if TSV is unavailable for any reason.
    """
    tess = tesseract_path()
    if not tess:
        return ""
    lang = ocr_languages()
    # PSM 6 = "assume a single uniform block of text", which discourages
    # Tesseract from inventing columns; combined with TSV reconstruction this
    # is robust for label/value forms.
    try:
        result = subprocess.run(
            [tess, img_path, "stdout", "-l", lang, "--psm", "6", "tsv"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        rebuilt = _reconstruct_rows_from_tsv(result.stdout or "")
        if rebuilt.strip():
            return rebuilt.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Fallback: plain text output (original behavior).
    try:
        result = subprocess.run(
            [tess, img_path, "-", "-l", lang, "--psm", "6"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return (result.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Broken text-layer detection
# ---------------------------------------------------------------------------
_VOWEL_RE = re.compile(r"[aeiouyAEIOUY]")
_VOWELS = set("aeiouAEIOU")

# Characters that legitimately show up a lot in engineering text: letters,
# digits, and ordinary punctuation/units. Anything outside this set counts as
# "odd" — broken CMap output is dominated by braces, tildes, carets, etc.
_OK_SYMBOLS = set(".,:;%/()-+#°&'\"=±×x<>[]@*")


def _cjk_count(text: str) -> int:
    """Count CJK (Chinese/Japanese/Korean) characters in text.

    Covers the main CJK Unified Ideographs block plus the common extension and
    compatibility ranges, so Chinese OCR output is recognized as real text.
    """
    n = 0
    for ch in text:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or      # CJK Unified Ideographs
                0x3400 <= o <= 0x4DBF or  # Extension A
                0xF900 <= o <= 0xFAFF or  # Compatibility Ideographs
                0x3000 <= o <= 0x303F):   # CJK symbols & punctuation
            n += 1
    return n


def _is_wordish(tok: str) -> bool:
    """Does a single token look like a real word (vs. CMap gibberish)?

    Real words: mostly letters, a sane vowel ratio, and at most a couple of
    upper/lower case switches. Broken-CMap tokens like 'XzWXjdzW' or 'gyjFXzXyj'
    fail on vowel ratio and/or case-switching even though they're letter-heavy.
    A token containing CJK characters counts as a real word.
    """
    if _cjk_count(tok) > 0:
        return True
    letters = [c for c in tok if c.isalpha()]
    L = len(letters)
    if L < 2:
        return False
    if L / len(tok) < 0.6:               # too many digits/symbols → not a word
        return False
    vr = sum(c in _VOWELS for c in letters) / L
    if vr < 0.20 or vr > 0.85:           # real words sit in this band
        return False
    switches = sum(1 for a, b in zip(letters, letters[1:])
                   if a.isupper() != b.isupper())
    if switches >= 3:                    # rAnDoM case → not a word
        return False
    return True


def _looks_like_garbage(text: str) -> bool:
    """Detect a corrupted text layer (gibberish like '}jF}jzd&}[X}€dg').

    Some PDFs — especially ones that have been through a bad OCR-injection
    tool — carry a text layer whose character codes don't map to real Unicode
    (a missing/broken ToUnicode CMap). The page renders correctly because the
    glyph shapes are intact, but extraction returns nonsense. When we detect
    that, we want to ignore the text layer and OCR the rendered image instead.

    Conservative by design: we'd rather occasionally OCR a good PDF (costs only
    time) than accept gibberish (corrupts the result and the search index).
    """
    t = (text or "").strip()
    if len(t) < 20:
        return False  # too little to judge; handled by the length heuristic

    # If the text contains a meaningful amount of CJK, treat it as real. CJK
    # has no spaces or vowels, so the Latin-oriented heuristics below don't
    # apply; a broken CMap virtually never yields valid CJK codepoints.
    nonspace_all = [c for c in t if not c.isspace()]
    if nonspace_all:
        cjk = _cjk_count(t)
        if cjk >= 4 and cjk / len(nonspace_all) > 0.10:
            return False

    tokens = t.split()
    if not tokens:
        return True

    word_ratio = sum(_is_wordish(tok) for tok in tokens) / len(tokens)

    # Fraction of "odd" symbols among non-space chars. CJK characters are not
    # "odd" — exclude them so Chinese text isn't penalized.
    nonspace = nonspace_all
    if not nonspace:
        return True
    odd = sum(1 for c in nonspace
              if not (c.isalnum() or c in _OK_SYMBOLS) and _cjk_count(c) == 0)
    odd_ratio = odd / len(nonspace)

    # Garbage signature: almost no real words, with the symbol soup that
    # broken CMaps produce. The second clause catches text with essentially
    # no words at all even when symbols are sparse.
    if word_ratio < 0.20 and odd_ratio > 0.12:
        return True
    if word_ratio < 0.05:
        return True
    return False


def _extract_page_text(page) -> str:
    """Extract a page's text-layer content in row-major (visual) order.

    PDFs store text in an internal draw order that is frequently column-major
    or otherwise scrambled — a multi-column form can come out as every label
    first, then every value, so a label is no longer beside its value. pypdf's
    "layout" extraction mode reorders text by its position on the page, so
    each row reads left-to-right and a label stays on the same line as its
    value. Falls back to the default extractor if layout mode is unavailable
    or yields nothing (older pypdf, or pages it can't lay out).
    """
    try:
        t = page.extract_text(extraction_mode="layout")
        if t and t.strip():
            return t
    except Exception:
        pass
    try:
        return page.extract_text() or ""
    except Exception:
        return ""


def extract_text_from_pdf(
    pdf_path: Path, max_pages: int = 50, force_ocr: bool = False
) -> tuple[str, dict]:
    """Extract text from a PDF.

    If force_ocr is True (and OCR is available), the text layer is ignored and
    every page is OCR'd from its rendered image — useful when a PDF carries a
    corrupted text layer that still passes the gibberish check.

    Returns (text, info) where info is a dict with diagnostic keys:
      - 'pages': total pages in the PDF
      - 'pages_extracted': how many pages we read
      - 'method': 'text-layer' | 'ocr' | 'mixed' | 'garbage' | 'failed'
      - 'has_text_layer': True if at least some pages had extractable text
      - 'used_ocr': True if OCR was used on at least one page
      - 'ocr_available': whether Tesseract was found at all
      - 'garbage_text_layer': True if a text layer was present but unreadable
    """
    info = {
        "pages": 0,
        "pages_extracted": 0,
        "method": "failed",
        "has_text_layer": False,
        "used_ocr": False,
        "ocr_available": tesseract_available(),
        "garbage_text_layer": False,
    }

    try:
        from pypdf import PdfReader
    except ImportError:
        info["error"] = "pypdf not installed"
        return "", info

    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        info["error"] = f"Could not open PDF: {e}"
        return "", info

    info["pages"] = len(reader.pages)
    pages_to_read = min(len(reader.pages), max_pages)
    info["pages_extracted"] = pages_to_read

    text_parts: list[str] = []
    pages_with_text = 0
    pages_via_ocr = 0
    pages_garbage = 0

    for i in range(pages_to_read):
        try:
            page_text = _extract_page_text(reader.pages[i])
        except Exception:
            page_text = ""

        non_ws = len(page_text.strip())
        # A page's text layer is usable only if it's substantial AND not the
        # gibberish that broken-CMap PDFs produce.
        is_garbage = _looks_like_garbage(page_text)
        usable_text = (non_ws >= 30) and not is_garbage and not force_ocr
        if is_garbage:
            pages_garbage += 1

        if usable_text:
            text_parts.append(page_text)
            pages_with_text += 1
        elif info["ocr_available"]:
            # Either an image-only page, a corrupted text layer, or force_ocr:
            # OCR the rendered image and prefer it if it reads cleanly.
            ocr_text = _ocr_page(pdf_path, i)
            if ocr_text and not _looks_like_garbage(ocr_text):
                text_parts.append(ocr_text)
                pages_via_ocr += 1
            elif non_ws >= 30 and not is_garbage:
                # OCR couldn't help (e.g. no rasterizer) but the text layer
                # was actually fine — keep it.
                text_parts.append(page_text)
                pages_with_text += 1
            elif ocr_text:
                text_parts.append(ocr_text)
                pages_via_ocr += 1
            # else: both empty/garbage → contribute nothing for this page
        else:
            # OCR not available. Keep a clean text layer if we have one;
            # never emit gibberish into the result.
            if non_ws >= 30 and not is_garbage:
                text_parts.append(page_text)
                pages_with_text += 1

    info["has_text_layer"] = pages_with_text > 0
    info["used_ocr"] = pages_via_ocr > 0
    info["garbage_text_layer"] = pages_garbage > 0 and pages_via_ocr == 0 and pages_with_text == 0
    if pages_via_ocr and pages_with_text:
        info["method"] = "mixed"
    elif pages_via_ocr:
        info["method"] = "ocr"
    elif pages_with_text:
        info["method"] = "text-layer"
    elif pages_garbage:
        info["method"] = "garbage"
    else:
        info["method"] = "failed"

    return "\n\n".join(text_parts), info


def _ocr_page(pdf_path: Path, page_index: int) -> str:
    """OCR a single page. Returns extracted text or '' on failure.

    Pipeline: render page to PNG via pypdf+Pillow, then run Tesseract.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from PIL import Image
    except ImportError:
        return ""

    tess = tesseract_path()
    if not tess:
        return ""

    # Step 1: extract the single page to a small PDF
    try:
        reader = PdfReader(str(pdf_path))
        if page_index >= len(reader.pages):
            return ""
        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        single_page_pdf = io.BytesIO()
        writer.write(single_page_pdf)
        single_page_pdf.seek(0)
    except Exception:
        return ""

    # Step 2: convert that single page to an image
    # We use Pillow's PDF support if available; otherwise we'd need
    # pdf2image+poppler, which is yet another external dependency.
    # Pillow alone can read PDFs only with an extra lib (pdfplumber / pymupdf).
    # As a pragmatic fallback, we use pypdf+reportlab to produce a JPEG.
    image_bytes = _render_pdf_page_to_image(single_page_pdf.getvalue())
    if not image_bytes:
        return ""

    # Step 3: run Tesseract on the image
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(image_bytes)
            img_path = f.name

        try:
            return _ocr_image_file(img_path, timeout=60)
        finally:
            try:
                os.unlink(img_path)
            except OSError:
                pass
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _render_pdf_page_to_image(pdf_bytes: bytes) -> Optional[bytes]:
    """Render a single-page PDF to a PNG image suitable for OCR.

    Tries multiple backends in order of preference. Returns PNG bytes or None.
    """
    # Backend 1: pdf2image + poppler (best quality, separate install)
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(pdf_bytes, dpi=200)
        if images:
            buf = io.BytesIO()
            images[0].save(buf, format="PNG")
            return buf.getvalue()
    except ImportError:
        pass
    except Exception:
        pass

    # Backend 2: PyMuPDF (single Python install, includes its own renderer)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=200)
        png_bytes = pix.tobytes("png")
        doc.close()
        return png_bytes
    except ImportError:
        pass
    except Exception:
        pass

    # No backend available — OCR can't proceed for this page
    return None


# ---------------------------------------------------------------------------
# Image OCR
# ---------------------------------------------------------------------------
# Image file extensions Tesseract can read directly (via the Leptonica
# image readers Tesseract is built against). We normalize everything through
# Pillow first so odd formats / orientations / CMYK get cleaned up.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


def extract_text_from_image(image_path: Path) -> tuple[str, dict]:
    """OCR a single image file (JPEG/PNG/TIFF/...).

    Returns (text, info) with the same diagnostic shape used by
    extract_text_from_pdf, so callers can treat PDFs and images uniformly.
    """
    info = {
        "pages": 1,
        "pages_extracted": 1,
        "method": "failed",
        "has_text_layer": False,   # images never have a text layer
        "used_ocr": False,
        "ocr_available": tesseract_available(),
    }

    tess = tesseract_path()
    if not tess:
        info["error"] = "Tesseract is not installed"
        return "", info

    # Normalize the image through Pillow: convert to RGB, which sidesteps
    # CMYK / palette / alpha quirks that occasionally trip up Tesseract.
    png_path = None
    try:
        try:
            from PIL import Image, ImageOps
        except ImportError:
            info["error"] = "Pillow not installed"
            return "", info

        with Image.open(str(image_path)) as im:
            im = ImageOps.exif_transpose(im)        # respect camera rotation
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                png_path = f.name
            im.save(png_path, format="PNG")

        text = _ocr_image_file(png_path, timeout=120)
        if text:
            info["used_ocr"] = True
            info["method"] = "ocr"
        else:
            info["method"] = "ocr"   # OCR ran, just found nothing legible
        return text, info
    except (subprocess.TimeoutExpired, OSError) as e:
        info["error"] = f"OCR failed: {e}"
        return "", info
    except Exception as e:
        info["error"] = f"Could not read image: {e}"
        return "", info
    finally:
        if png_path:
            try:
                os.unlink(png_path)
            except OSError:
                pass


def extract_text_from_file(file_path: Path, max_pages: int = 50,
                           force_ocr: bool = False) -> tuple[str, dict]:
    """Dispatch to the right extractor based on the file's extension.

    PDFs go through the pypdf + OCR-fallback pipeline; images go straight
    to Tesseract. Anything else is reported as unsupported. force_ocr forces
    image OCR on PDFs even when a (possibly corrupt) text layer exists.
    """
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path, max_pages=max_pages, force_ocr=force_ocr)
    if ext in IMAGE_EXTS:
        return extract_text_from_image(file_path)
    return "", {
        "pages": 0,
        "pages_extracted": 0,
        "method": "failed",
        "has_text_layer": False,
        "used_ocr": False,
        "ocr_available": tesseract_available(),
        "error": f"Unsupported file type for OCR: {ext or '(none)'}",
    }


def ocr_status() -> dict:
    """Report OCR capability so the UI can tell the user what works.

    rasterizer matters only for *scanned* PDFs: text-based PDFs and plain
    image files don't need it.
    """
    raster = None
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        raster = "PyMuPDF"
    except ImportError:
        try:
            import pdf2image  # noqa: F401
            raster = "pdf2image"
        except ImportError:
            raster = None

    langs = tesseract_languages()
    has_chinese = any(l in langs for l in ("chi_sim", "chi_tra"))
    return {
        "tesseract_available": tesseract_available(),
        "tesseract_version": tesseract_version(),
        "pdf_rasterizer": raster,          # None => scanned-PDF OCR unavailable
        "image_ocr": tesseract_available(),
        "scanned_pdf_ocr": tesseract_available() and raster is not None,
        "languages": langs,                # installed Tesseract language packs
        "ocr_lang": ocr_languages() if tesseract_available() else "",
        "chinese_available": has_chinese,
    }


# ---------------------------------------------------------------------------
# Parameter extraction (regex-based)
# ---------------------------------------------------------------------------
# We support a handful of value formats commonly seen in engineering docs:
#   "Yield Strength: 250 MPa"
#   "Yield strength = 250MPa"
#   "Yield Strength    250 MPa"
#   "Min. yield strength       36 ksi"
#   "Yield Strength (MPa) 250"
#   "Yield Strength, MPa: 250"
#
# We're conservative: we want "no match" rather than wrong values. False
# positives are worse than false negatives because the user has to clean
# up extracted data anyway.

# Common engineering units we recognize. ORDERING MATTERS — longer/more
# specific patterns must come BEFORE shorter overlapping ones, so the regex
# engine prefers them. Otherwise "g/cm³" gets matched as just "g" because
# regex alternation is greedy left-to-right, not longest-match.
_UNIT_PATTERNS = [
    # Density (compound, must precede mass)
    r"kg/m\^?3", r"kg/m³", r"g/cm\^?3", r"g/cm³", r"lb/in\^?3", r"lb/ft\^?3",
    # Velocity / flow (compound, must precede length)
    r"m/s", r"ft/s", r"mph", r"kph",
    # Energy/torque
    r"ft[-\s]?lb(?:s)?", r"ft·lb", r"N·m", r"Nm", r"J",
    # Stress/pressure (longer first)
    r"GPa", r"MPa", r"kPa", r"ksi", r"psi", r"bar", r"Pa",
    # Hardness scales
    r"HRC", r"HRB", r"HV", r"HB", r"BHN", r"Shore\s?[A-D]?",
    # Temperature
    r"°C", r"°F", r"deg\s?C", r"deg\s?F", r"K",
    # Length/dimension (longer first)
    r"mm", r"cm", r"in(?:ch(?:es)?)?", r"ft", r"m", r"\"",
    # Mass
    r"kg", r"lb(?:s)?", r"lbm", r"g",
    # Percent / ratio
    r"%", r"pct",
    # Misc electrical
    r"MHz", r"kHz", r"Hz", r"kW", r"hp", r"V", r"A", r"W",
]

# Precompile a unit alternative
_UNIT_ALT = "(?:" + "|".join(_UNIT_PATTERNS) + ")"

# A number: integer or decimal, optional sign, optional thousands separators
_NUMBER = r"[-+]?\d{1,3}(?:[,]\d{3})*(?:\.\d+)?|[-+]?\d+(?:\.\d+)?"


def extract_parameters(text: str, parameters: list[str]) -> dict[str, dict]:
    """Find values for each requested parameter in `text`.

    Returns dict keyed by parameter name, each value being:
      {
        "value": "250",         # the matched numeric string (string, not float)
        "unit": "MPa",          # the unit if found, else ""
        "raw": "Yield Strength: 250 MPa",   # the matched text snippet
        "confidence": "high" | "medium" | "low",
      }

    Parameters not found are simply absent from the result dict.
    """
    if not text or not parameters:
        return {}

    # Normalize the input: collapse multi-space to single, but preserve
    # newlines so line-anchored regexes still work.
    normalized = re.sub(r"[ \t]+", " ", text)

    results: dict[str, dict] = {}
    for param in parameters:
        match = _find_parameter_value(normalized, param)
        if match:
            results[param] = match
    return results


def _find_parameter_value(text: str, param_name: str) -> Optional[dict]:
    """Search for a single parameter and return its value if found.

    Strategy: try a sequence of patterns from strict (high confidence) to
    permissive (low confidence). Stop at the first match.
    """
    # Build a flexible match for the parameter name itself: case-insensitive,
    # allow extra whitespace, allow common prefixes like "Min.", "Max.", "Avg",
    # and allow common synonyms hardcoded for a few common parameters.
    name_alts = _name_alternatives(param_name)
    # Wrap each alternative in word boundaries to prevent matching substrings
    # — critical for short names like "E" (Young's modulus) which would
    # otherwise match every "E" in the text.
    name_pattern = "(?:" + "|".join(r"\b" + re.escape(a) + r"\b" for a in name_alts) + ")"

    # Allow optional qualifier prefixes (Min., Max., Avg, Average)
    qualifier = r"(?:(?:min(?:imum|\.)?|max(?:imum|\.)?|avg|average|nominal|typ(?:ical|\.)?)\s+)?"

    # Allow parenthetical units inside the parameter name (e.g., "Yield (MPa)")
    paren_unit = r"(?:\s*\(\s*" + _UNIT_ALT + r"\s*\))?"

    # Pattern 1 (HIGH confidence): "Name [unit?]: value [unit?]"
    p1 = (
        r"(?:^|\n|[\s,;])"
        + qualifier
        + name_pattern
        + paren_unit
        + r"\s*[:=]\s*"
        + r"(" + _NUMBER + r")"
        + r"\s*(" + _UNIT_ALT + r")?"
    )
    m = re.search(p1, text, re.IGNORECASE)
    if m:
        return _build_result(m, "high")

    # Pattern 2 (MEDIUM confidence): "Name [unit?] value [unit?]" (no separator)
    # Only after a line break or sentence boundary, to avoid mid-paragraph noise.
    p2 = (
        r"(?:^|\n)\s*"
        + qualifier
        + name_pattern
        + paren_unit
        + r"\s+"
        + r"(" + _NUMBER + r")"
        + r"\s*(" + _UNIT_ALT + r")?"
    )
    m = re.search(p2, text, re.IGNORECASE | re.MULTILINE)
    if m:
        return _build_result(m, "medium")

    # Pattern 3 (LOW confidence): name followed by a number anywhere within
    # a window of 30 chars. Useful for table-like layouts.
    p3 = (
        name_pattern
        + r"[^\n]{0,30}?"
        + r"(" + _NUMBER + r")"
        + r"\s*(" + _UNIT_ALT + r")?"
    )
    # For LOW confidence matches, require the unit to be in the expected
    # set for this parameter. This filters out false positives like
    # "Charpy impact at -20°C" matching the temperature instead of energy.
    expected = _expected_units(param_name)
    for m in re.finditer(p3, text, re.IGNORECASE):
        unit = ""
        if m.lastindex and m.lastindex >= 2 and m.group(m.lastindex):
            unit = m.group(m.lastindex).strip()
        if expected:
            # Require either no unit (we'll accept and note it) or an expected unit
            if unit and unit not in expected:
                continue
        return _build_result(m, "low")

    return None


# Per-parameter expected units. Used to filter low-confidence matches and
# avoid false positives where the parameter name appears near an unrelated
# value (e.g., "Charpy impact at -20°C 45J" matching the -20°C).
_EXPECTED_UNITS: dict[str, set[str]] = {
    "yield strength": {"MPa", "GPa", "kPa", "ksi", "psi"},
    "ultimate strength": {"MPa", "GPa", "kPa", "ksi", "psi"},
    "tensile strength": {"MPa", "GPa", "kPa", "ksi", "psi"},
    "hardness": {"HRC", "HRB", "HV", "HB", "BHN"},
    "density": {"kg/m³", "kg/m^3", "g/cm³", "g/cm^3", "lb/in^3", "lb/in³", "lb/ft^3", "lb/ft³"},
    "modulus": {"GPa", "MPa", "ksi", "psi"},
    "elongation": {"%", "pct"},
    "impact energy": {"J", "Nm", "N·m", "ft-lb", "ft lb", "ft·lb"},
    "thickness": {"mm", "cm", "in", "inch", "inches", "m", '"'},
    "diameter": {"mm", "cm", "in", "inch", "inches", "m", '"'},
}


def _expected_units(param_name: str) -> set[str]:
    """Return the set of expected units for a parameter, or empty if unknown."""
    return _EXPECTED_UNITS.get(param_name.strip().lower(), set())


def _build_result(m: re.Match, confidence: str) -> dict:
    value = m.group(1).replace(",", "")  # strip thousands separators
    unit = ""
    # The unit is the LAST group in our patterns
    if m.lastindex and m.lastindex >= 2:
        unit_match = m.group(m.lastindex)
        if unit_match:
            unit = unit_match.strip()
    raw = m.group(0).strip()
    # Trim raw to a reasonable length for display
    if len(raw) > 100:
        raw = raw[:97] + "..."
    return {
        "value": value,
        "unit": unit,
        "raw": raw,
        "confidence": confidence,
    }


# Synonyms for common engineering parameters. The user can still override
# by adding a parameter with whatever name they prefer; this just helps
# the regex find more variations of the most common ones.
_PARAM_SYNONYMS: dict[str, list[str]] = {
    "yield strength": [
        "yield strength", "yield stress", "yield point",
        "yield",  # minimal — only matches if standalone
        "σy", "Re", "Rp0.2", "Rp 0.2",
    ],
    "ultimate strength": [
        "ultimate strength", "ultimate tensile strength", "tensile strength",
        "UTS", "Rm",
    ],
    "hardness": [
        "hardness", "Brinell hardness", "Rockwell hardness", "Vickers hardness",
        "BHN",
    ],
    "density": [
        "density", "mass density", "ρ", "specific gravity",
    ],
    "modulus": [
        "modulus", "Young's modulus", "elastic modulus", "modulus of elasticity",
        "E",
    ],
    "elongation": [
        "elongation", "elongation at break", "% elongation", "A50", "A5",
    ],
    "impact energy": [
        "impact energy", "Charpy impact", "Charpy V-notch", "CVN", "Izod impact",
    ],
    "yield point": ["yield point"],
    "thickness": ["thickness", "wall thickness", "plate thickness"],
    "diameter": ["diameter", "OD", "outside diameter", "Ø"],
}


def _name_alternatives(param_name: str) -> list[str]:
    """Return the parameter name plus any known synonyms."""
    key = param_name.strip().lower()
    if key in _PARAM_SYNONYMS:
        # Always include the user's exact name first
        seen = {param_name}
        out = [param_name]
        for syn in _PARAM_SYNONYMS[key]:
            if syn not in seen:
                seen.add(syn)
                out.append(syn)
        return out
    return [param_name]


# ---------------------------------------------------------------------------
# General key-string extraction (user-defined keys)
# ---------------------------------------------------------------------------
# Unlike extract_parameters() above (which is numeric/unit-aware and uses
# hard-coded engineering synonyms), this works with ARBITRARY user-defined key
# strings and captures whatever string OR value follows the key — an ID, a
# date, a phrase, a number with units, anything. Keys that aren't found come
# back as "NF". This is the engine behind the "extract key parameters" feature.

NOT_FOUND = "NF"


def _clean_following_value(s: str) -> str:
    """Tidy the text captured right after a key.

    Removes the label/value separator (a colon or equals sign), dot leaders
    ("Pressure......10,000"), and en/em-dash or bullet leaders — but NOT a
    plain hyphen-minus, so negative values like "-20°C" survive.
    """
    s = s.strip()
    s = re.sub(r"^[\s:=#：﹕︰]+", "", s)    # leading colon/equals/hash (incl. full-width ：)
    s = re.sub(r"^\.{2,}\s*", "", s)       # dot leaders in tables
    s = re.sub(r"^[·•‣▪–—]\s*", "", s)     # bullet / en-dash / em-dash leaders
    s = re.sub(r"[\s.:;：]+$", "", s)       # trailing separators / whitespace
    # Tesseract often inserts spaces between individual CJK characters; remove
    # a space only when it sits between two CJK chars, so "中 石油 工程" → "中石油工程"
    # while "35CrMo 合金 钢" keeps the space after the Latin run.
    s = re.sub(r"(?<=[\u3400-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u9fff\uf900-\ufaff])", "", s)
    return s.strip()


def _find_following_value(text: str, key: str, max_value_len: int = 200,
                          same_line_only: bool = True) -> Optional[str]:
    """Find `key` in `text` (case-insensitive) and return the value after it.

    - Whitespace inside the key is matched flexibly, so OCR turning
      "Assembly Id" into "Assembly  Id" or a line-broken "Assembly\\nId"
      still matches.
    - Word boundaries are applied only at alphanumeric edges, so short keys
      don't match inside longer words.
    - The value is taken from the SAME line as the key (after any separator and
      spaces), which is the common "Label:  value" / "Label   value" layout.
    - Only if `same_line_only` is False and the same line has nothing after the
      key do we fall back to the next non-empty line (useful for table layouts
      where OCR puts a column header above its value).

    Returns the cleaned value string, or None if the key isn't present at all.
    """
    tokens = key.split()
    if not tokens:
        return None

    # Build a pattern that tolerates flexible whitespace. Between space-split
    # tokens we already allow \s+. Additionally, OCR frequently inserts spaces
    # *between individual CJK characters* (e.g. "客户名称" → "客户 名 称"), so
    # within each token we also allow optional whitespace between adjacent CJK
    # characters. Latin runs inside a token stay glued together.
    def _token_to_regex(tok: str) -> str:
        parts = []
        chars = list(tok)
        for i, ch in enumerate(chars):
            parts.append(re.escape(ch))
            if i < len(chars) - 1:
                # Allow optional whitespace after a CJK char or before the next
                # CJK char (covers OCR splitting CJK glyphs apart).
                if _cjk_count(ch) > 0 or _cjk_count(chars[i + 1]) > 0:
                    parts.append(r"\s*")
        return "".join(parts)

    flex = r"\s+".join(_token_to_regex(t) for t in tokens)
    # Word boundaries only make sense for ASCII alphanumerics; CJK has no \b,
    # so don't anchor when the edge char is CJK.
    def _edge(ch: str) -> str:
        return r"\b" if (ch.isascii() and (ch.isalnum() or ch == "_")) else ""
    pre = _edge(tokens[0][:1])
    post = _edge(tokens[-1][-1:])
    pattern = re.compile(pre + flex + post, re.IGNORECASE)

    m = pattern.search(text)
    if not m:
        return None  # key truly absent → caller maps to NF

    after = text[m.end():]
    nl = after.find("\n")
    same_line = after if nl == -1 else after[:nl]
    val = _clean_following_value(same_line)

    # Same-line was empty AND the caller allows it → look at the next line(s).
    if not val and not same_line_only and nl != -1:
        for ln in after[nl + 1:].splitlines():
            c = _clean_following_value(ln)
            if c:
                val = c
                break

    if len(val) > max_value_len:
        val = val[:max_value_len].rstrip()
    return val


def extract_key_strings(text: str, keys: list[str],
                        max_value_len: int = 200,
                        same_line_only: bool = True) -> dict[str, dict]:
    """Extract a value for each user-defined key string.

    For every key: if it appears in `text`, capture the string/value that
    follows it ON THE SAME LINE; otherwise (or if nothing usable follows)
    report "NF". Set same_line_only=False to also search the next line when
    the same line has no value.

    Returns an ordered dict keyed by the original key string:
      { key: {"value": <str or "NF">, "found": <bool>} }

    `found` is True only when a non-empty value was captured, so the UI can
    distinguish "matched with a value" from "NF".
    """
    results: dict[str, dict] = {}
    if not keys:
        return results

    body = text or ""
    for raw in keys:
        key = (raw or "").strip()
        if not key:
            continue
        if key in results:          # de-dupe, keep first
            continue
        val = (_find_following_value(body, key, max_value_len, same_line_only)
               if body else None)
        if val:
            results[key] = {"value": val, "found": True}
        else:
            results[key] = {"value": NOT_FOUND, "found": False}
    return results


# ---------------------------------------------------------------------------
# Convenience: full extraction in one call
# ---------------------------------------------------------------------------
def process_pdf(pdf_path: Path, parameters: list[str]) -> dict:
    """Run the full pipeline: extract text, then parameters.

    Returns:
      {
        "text_info": {...},       # diagnostic info about text extraction
        "extracted": {...},       # parameter -> {value, unit, raw, confidence}
        "text_excerpt": "...",    # first ~500 chars for debugging
      }
    """
    text, info = extract_text_from_pdf(pdf_path)
    extracted = extract_parameters(text, parameters)
    return {
        "text_info": info,
        "extracted": extracted,
        "text_excerpt": text[:500] if text else "",
    }

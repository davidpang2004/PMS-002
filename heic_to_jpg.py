#!/usr/bin/env python3
"""Convert HEIC / HEIF photos to JPG -- standalone, no DMS needed.

Usage examples
--------------
    python3 heic_to_jpg.py IMG_0001.HEIC IMG_0002.HEIC          # specific files
    python3 heic_to_jpg.py ~/Pictures/iPhone                    # every HEIC in a folder
    python3 heic_to_jpg.py ~/Pictures/iPhone -r                 # ... including subfolders
    python3 heic_to_jpg.py photos/ -o jpg_out -q 95             # choose output folder / quality
    python3 heic_to_jpg.py photos/ --overwrite --delete-original

With no -o, each JPG is written next to its HEIC (same name, .jpg).
With -o, JPGs go into that folder (subfolder structure is kept for -r).

Needs:  pip3 install pillow pillow-heif
The photo's EXIF data (date, GPS, camera...) is kept, and the rotation stored
in EXIF is applied to the pixels so the JPG always displays upright.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    LIBS_OK = True
except ImportError:
    LIBS_OK = False          # the CLI reports this; DMS shows it in its dialog

HEIC_EXTS = {".heic", ".heif", ".hif"}


def find_inputs(paths: list[str], recursive: bool) -> list[tuple[Path, Path]]:
    """Return (file, root) pairs; root is the folder the file was found under
    (used to keep subfolder structure under -o)."""
    found: list[tuple[Path, Path]] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in HEIC_EXTS and not f.name.startswith("._"):
                    found.append((f, p))
        elif p.is_file():
            if p.suffix.lower() in HEIC_EXTS:
                found.append((p, p.parent))
            else:
                print(f"skip (not HEIC/HEIF): {p}")
        else:
            print(f"not found: {p}")
    return found


def convert(src: Path, dst: Path, quality: int) -> None:
    with Image.open(src) as im:
        exif = im.info.get("exif")
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)        # bake EXIF rotation into the pixels
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        kwargs = {"quality": quality, "optimize": True}
        if icc:
            kwargs["icc_profile"] = icc
        if exif:
            # Rotation is already applied, so reset the Orientation tag to "normal".
            try:
                ex = Image.Exif()
                ex.load(exif)
                ex[0x0112] = 1
                kwargs["exif"] = ex.tobytes()
            except Exception:
                kwargs["exif"] = exif
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", **kwargs)


def run_conversion(inputs, recursive=False, output=None, quality=92, overwrite=False,
                   delete_original=False, progress=None) -> dict:
    """Convert HEIC/HEIF files. Used by both this script's CLI and DMS.

    progress(done, total, message) is called after every file if given.
    Returns {total, converted, skipped, failed, items:[{src,dst,status,error}]}.
    """
    if not LIBS_OK:
        raise RuntimeError("Missing libraries. Install them with: pip3 install pillow pillow-heif")
    items = find_inputs(list(inputs), recursive)
    out_root = Path(output).expanduser() if output else None
    result = {"total": len(items), "converted": 0, "skipped": 0, "failed": 0, "items": []}
    for i, (src, root) in enumerate(items, 1):
        if out_root:
            try:
                rel = src.relative_to(root).with_suffix(".jpg")
            except ValueError:
                rel = Path(src.name).with_suffix(".jpg")
            dst = out_root / rel
        else:
            dst = src.with_suffix(".jpg")
        rec = {"src": str(src), "dst": str(dst), "status": "converted", "error": ""}
        if dst.exists() and not overwrite:
            rec["status"] = "skipped"
            result["skipped"] += 1
        else:
            try:
                convert(src, dst, quality)
                result["converted"] += 1
                if delete_original:
                    src.unlink()
            except Exception as e:                   # keep going on a bad file
                rec["status"], rec["error"] = "failed", str(e)
                result["failed"] += 1
        result["items"].append(rec)
        if progress:
            progress(i, len(items), f"{src.name}: {rec['status']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert HEIC/HEIF photos to JPG.")
    ap.add_argument("inputs", nargs="+", help="HEIC files and/or folders")
    ap.add_argument("-o", "--output", help="output folder (default: next to each original)")
    ap.add_argument("-q", "--quality", type=int, default=92, help="JPEG quality 1-100 (default 92)")
    ap.add_argument("-r", "--recursive", action="store_true", help="include subfolders of input folders")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing JPG (default: skip it)")
    ap.add_argument("--delete-original", action="store_true", help="delete each HEIC after it converts successfully")
    args = ap.parse_args()

    if not LIBS_OK:
        sys.exit("Missing libraries. Install them with:\n    pip3 install pillow pillow-heif")
    if not 1 <= args.quality <= 100:
        ap.error("--quality must be between 1 and 100")

    def show(done, total, msg):
        print(f"[{done}/{total}] {msg}")

    res = run_conversion(args.inputs, args.recursive, args.output, args.quality,
                         args.overwrite, args.delete_original, progress=show)
    if not res["total"]:
        print("No HEIC/HEIF files found.")
        return 1
    print(f"\nDone: {res['converted']} converted, {res['skipped']} skipped, {res['failed']} failed.")
    return 0 if res["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

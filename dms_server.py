#!/usr/bin/env python3
"""
DMS Server — Document Management System backend.

A small Flask server that:
  • Serves the PMS web app (dms.html) on http://localhost:8001
  • Stores tree structure + document index in <storage_root>/index.json
  • Stores uploaded documents in <storage_root>/docs/<DOC-ID>.<ext>
  • Stores its own settings (chosen storage path) in ~/.pms_dms_config.json

Usage:
    python3 dms_server.py            # starts on default port 8001
    python3 dms_server.py --port 9000

First run: open http://localhost:8001 in Chrome/Edge/Firefox/Safari, click the
folder icon in the header, type the path you want to use for storage (e.g.
/Users/yourname/Documents/Company01), click Save. From then on, all files
go to that folder.

Migration path to a real web server later: this same script runs on any
machine with Python — drop it on a Linux box, point a domain at it, and
you have a network-accessible DMS. The frontend doesn't change at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import sys
import tempfile
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Timer

import re
import time as _time
from collections import deque, defaultdict
from urllib.parse import quote as _url_quote

try:
    from flask import (
        Flask, request, jsonify, send_file, send_from_directory,
        abort, Response, redirect,
    )
except ImportError:
    print("ERROR: Flask is not installed.")
    print()
    print("Install it with:")
    print("    pip3 install flask")
    print()
    print("Or, if you prefer, install with the system Python:")
    print("    python3 -m pip install flask")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration — where the server stores its own settings (NOT documents)
# ---------------------------------------------------------------------------
CONFIG_PATH = Path.home() / ".pms_dms_config.json"
# When packaged with PyInstaller, the launcher sets DMS_RESOURCE_DIR so we can
# find dms.html inside the temp extraction folder. In dev mode (running this
# script directly), fall back to the script's own directory.
SCRIPT_DIR = Path(os.environ.get("DMS_RESOURCE_DIR") or Path(__file__).resolve().parent)

DEFAULT_CONFIG = {
    # Where the DMS stores its data. Empty until the user picks one.
    "storage_path": "",
    # SHA256 hash of the (salt + password). Empty = no password required.
    "password_hash": "",
    "password_salt": "",
}

DEFAULT_KEY_PARAMETERS = [
    "人物",
    "事件",
    "单位",
    "家人",
    "朋友",
    "旅游",
]


def normalize_key_parameters(items):
    """Return a clean, de-duplicated list while preserving the user's order."""
    seen = set()
    out = []
    for raw in items or []:
        val = str(raw or "").strip()
        if not val:
            continue
        key = val.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out


def load_config() -> dict:
    """Load the server config or fall back to defaults."""
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# Set by the launcher when a project path is passed as a command-line argument.
# Takes precedence over the shared ~/.dms_server_config.json storage_path.
_storage_path_override: str = ""


def get_storage_root():
    """Return the configured storage root as a Path, or None if not set."""
    if _storage_path_override:
        return Path(_storage_path_override).expanduser().resolve()
    cfg = load_config()
    p = cfg.get("storage_path", "").strip()
    if not p:
        return None
    return Path(p).expanduser().resolve()


def get_docs_dir():
    root = get_storage_root()
    if not root:
        return None
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    return docs


def get_index_path():
    root = get_storage_root()
    if not root:
        return None
    return root / "index.json"


def read_index() -> dict:
    """Read index.json; return empty index if file doesn't exist yet."""
    p = get_index_path()
    if not p or not p.exists():
        return {"tree": None, "docIndex": [], "keyParameters": list(DEFAULT_KEY_PARAMETERS)}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tree": None, "docIndex": [], "keyParameters": list(DEFAULT_KEY_PARAMETERS)}


def write_index(idx: dict) -> None:
    p = get_index_path()
    if not p:
        raise RuntimeError("Storage path is not configured")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, indent=2))


def _safe_filename_part(name: str) -> str:
    """Return a filesystem-safe version of *name* for embedding in a stored path."""
    safe = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '_', name).strip('. ')
    if len(safe) > 80:
        suffix = Path(safe).suffix
        safe = Path(safe).stem[:80 - len(suffix)] + suffix
    return safe or "file"


def _safe_folder_name(name: str) -> str:
    """Return a filesystem-safe folder name from a node name (no extension logic)."""
    safe = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '_', name).strip('. ')
    return (safe[:60] if len(safe) > 60 else safe) or "node"


def _reverse_geocode(lat: float, lon: float) -> "str | None":
    """Return a concise location string for lat/lon via Nominatim, or raw coords on failure."""
    try:
        import urllib.request, json as _json, ssl
        url = (f"https://nominatim.openstreetmap.org/reverse"
               f"?format=json&lat={lat}&lon={lon}&zoom=14&addressdetails=1")
        req = urllib.request.Request(url, headers={"User-Agent": "DMS/1.0"})
        # Use certifi CA bundle when available (needed inside PyInstaller bundles)
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            data = _json.loads(resp.read())
        addr = data.get("address", {})
        parts = []
        for key in ("village", "town", "city", "county", "state", "province", "country"):
            val = addr.get(key)
            if val and val not in parts:
                parts.append(val)
                if len(parts) == 3:
                    break
        return ", ".join(parts) if parts else data.get("display_name", "")
    except Exception as e:
        print(f"[DMS] geocode failed ({lat:.4f},{lon:.4f}): {e}")
        # Fall back to raw coordinates so something is always saved
        return f"{lat:.5f}, {lon:.5f}"


def _read_photo_gps(data: bytes) -> "tuple[float, float] | tuple[None, None]":
    """Return (lat, lon) from EXIF GPS tags using Pillow, or (None, None).
    Supports JPEG, HEIC, TIFF, PNG and any format Pillow can open."""
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(data))
        # Try modern getexif() first (works for HEIC/TIFF/PNG), fall back to _getexif() for JPEG
        try:
            exif_obj = img.getexif()
            exif = dict(exif_obj) if exif_obj else None
            gps_info = exif_obj.get_ifd(34853) if exif_obj else None  # GPSInfo IFD
        except Exception:
            exif = img._getexif() if hasattr(img, "_getexif") else None
            gps_info = exif.get(34853) if exif else None
        if not gps_info:
            return None, None
        def _to_deg(val):
            return float(val[0]) + float(val[1]) / 60 + float(val[2]) / 3600
        lat = _to_deg(gps_info.get(2, (0, 0, 0)))
        lon = _to_deg(gps_info.get(4, (0, 0, 0)))
        if gps_info.get(1) == "S":
            lat = -lat
        if gps_info.get(3) == "W":
            lon = -lon
        return lat, lon
    except Exception:
        return None, None


def _find_node_by_name(root: dict, name: str) -> "dict | None":
    """Return the first node in the tree whose name matches (depth-first)."""
    if not root:
        return None
    if root.get("name") == name:
        return root
    for child in (root.get("children") or []):
        found = _find_node_by_name(child, name)
        if found:
            return found
    return None


def _find_node_by_id(root: dict, node_id: str) -> "dict | None":
    """Return the first node in the tree whose id matches node_id."""
    if not root or not node_id:
        return None
    if root.get("id") == node_id:
        return root
    for child in (root.get("children") or []):
        found = _find_node_by_id(child, node_id)
        if found:
            return found
    return None


def _get_or_create_child_node(parent_node: dict, child_name: str) -> dict:
    """Return the direct child named child_name, creating it in-place if absent."""
    for child in (parent_node.get("children") or []):
        if child.get("name") == child_name:
            return child
    new_node = {
        "id": f"NODE-{secrets.token_hex(3).upper()}",
        "name": child_name,
        "children": [],
        "documents": [],
    }
    parent_node.setdefault("children", []).append(new_node)
    return new_node


def _read_photo_date(data: bytes, mime: str) -> tuple:
    """Return (year, month) strings from EXIF if available, else today's date."""
    year, month = None, None
    if mime.startswith("image/"):
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(data))
            exif = img._getexif() if hasattr(img, "_getexif") else None
            if exif:
                for tag_id in (36867, 36868, 306):
                    val = exif.get(tag_id, "")
                    if val and isinstance(val, str) and len(val) >= 7:
                        parts = val.split(":")
                        if len(parts) >= 2 and parts[0].isdigit() and len(parts[1]) >= 2:
                            year, month = parts[0], parts[1][:2]
                            break
        except Exception:
            pass
    if not year:
        now = datetime.now()
        year, month = now.strftime("%Y"), now.strftime("%m")
    return year, month


def _get_node_path_parts(tree: dict, node_id: str) -> list:
    """Return ordered list of safe folder names from root down to node_id."""
    def _walk(node, target, path):
        part = _safe_folder_name(node.get("name") or node.get("id") or "node")
        current = path + [part]
        if node.get("id") == target:
            return current
        for child in (node.get("children") or []):
            result = _walk(child, target, current)
            if result is not None:
                return result
        return None
    return _walk(tree, node_id, []) or []


def _path_exists_for_node(tree: dict | None, node_id: str | None) -> bool:
    return bool(tree and node_id and _get_node_path_parts(tree, node_id))


def _get_node_docs_dir(node_id: str | None, tree: dict | None = None) -> "Path | None":
    """Return (and create) the docs subdirectory for a given tree node."""
    docs_dir = get_docs_dir()
    if not docs_dir or not node_id:
        return docs_dir
    if tree is None:
        tree = read_index().get("tree")
    if not tree:
        return docs_dir
    parts = _get_node_path_parts(tree, node_id)
    if not parts:
        return docs_dir
    subdir = docs_dir.joinpath(*parts)
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir


_PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif", "bmp"}


def _is_photo_file(ext: str, mime: str) -> bool:
    return (ext or "").lower() in _PHOTO_EXTS or (mime or "").startswith("image/")


def _valid_photo_year_month(year: str | None, month: str | None) -> bool:
    return bool(
        year and month
        and len(year) == 4 and year.isdigit()
        and len(month) == 2 and month.isdigit()
        and 1 <= int(month) <= 12
    )


def _photo_year_month_dir(
    docs_dir: Path,
    tree: dict | None,
    year: str,
    month: str,
    base_node_id: str | None = None,
) -> Path:
    """Return docs/<base>/<year>/<month>, creating folders as needed.

    Resolution order for the base:
      1. base_node_id if supplied and found in tree
      2. tree root
    """
    base_dir = docs_dir
    if tree:
        base_node = None
        if base_node_id:
            base_node = _find_node_by_id(tree, base_node_id)
        if base_node is None:
            base_node = tree
        parts = _get_node_path_parts(tree, base_node.get("id"))
        if parts:
            base_dir = docs_dir.joinpath(*parts)
    out_dir = base_dir / _safe_folder_name(year) / _safe_folder_name(month)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _find_or_create_photo_month_node(
    tree: dict | None,
    base_node_id: str | None,
    year: str,
    month: str,
) -> "str | None":
    """Ensure the tree contains year/month children and return the month node id.

    Resolution order for the base:
      1. base_node_id if supplied and found in tree
      2. tree root
    """
    if not tree:
        return base_node_id
    base_node = _find_node_by_id(tree, base_node_id or "") if base_node_id else None
    if base_node is None:
        base_node = tree
    year_node = _get_or_create_child_node(base_node, year)
    month_node = _get_or_create_child_node(year_node, month)
    return month_node.get("id")


def _prune_empty_legacy_photo_dirs(docs_dir: Path) -> None:
    """Remove empty legacy MyPhoto folders created by older upload logic."""
    if not docs_dir.exists():
        return
    for path in sorted(docs_dir.rglob("*"), reverse=True):
        if not path.is_dir():
            continue
        if not any(part in {"MyPhoto", "Photos"} for part in path.relative_to(docs_dir).parts):
            continue
        try:
            if any(path.iterdir()):
                continue
            path.rmdir()
        except OSError:
            continue


def _migrate_legacy_photo_files(docs_dir: Path, tree: dict | None) -> None:
    """Move any orphaned files from legacy MyPhoto folders into the visible tree folder."""
    if not docs_dir.exists():
        return

    target_dir = docs_dir
    if tree:
        stack = [tree]
        while stack:
            node = stack.pop()
            if node.get("name") == "New Project":
                target_dir = _get_node_docs_dir(node.get("id"), tree) or docs_dir
                break
            for child in reversed(node.get("children") or []):
                stack.append(child)

    legacy_root = docs_dir / "MyPhoto"
    if not legacy_root.exists():
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(legacy_root.rglob("*")):
        if not path.is_file():
            continue
        destination = target_dir / path.name
        if destination.exists():
            stem = path.stem
            suffix = path.suffix
            counter = 1
            while True:
                candidate = target_dir / f"{stem}-{counter}{suffix}"
                if not candidate.exists():
                    destination = candidate
                    break
                counter += 1
        try:
            path.rename(destination)
        except OSError:
            continue


def _create_local_folder_structure(tree: dict | None) -> "Path | None":
    """Create the on-disk folder hierarchy that mirrors the current tree.

    Do not delete general folders here. Users may have files in folders that
    are no longer represented by the tree, and root renames can temporarily
    leave old folders behind until indexed files are moved.
    """
    docs_dir = get_docs_dir()
    if not docs_dir:
        return None
    docs_dir.mkdir(parents=True, exist_ok=True)

    if not tree:
        _prune_empty_legacy_photo_dirs(docs_dir)
        return docs_dir

    _migrate_legacy_photo_files(docs_dir, tree)

    stack = [tree]
    while stack:
        node = stack.pop()
        node_id = node.get("id")
        if node_id:
            _get_node_docs_dir(node_id, tree)
        for child in reversed(node.get("children") or []):
            stack.append(child)

    _prune_empty_legacy_photo_dirs(docs_dir)
    return docs_dir


def _sync_doc_files_to_tree_paths(tree: dict | None) -> None:
    """Move indexed files into the folder path for their current tree node.

    This repairs root/folder renames on disk. For example, if files were under
    docs/Old Root/2024/05 and the root is renamed to Family Photo, the files
    move to docs/Family Photo/2024/05. Files that are not in index.json are
    never moved or deleted.
    """
    if not tree:
        return
    docs_dir = get_docs_dir()
    if not docs_dir:
        return
    idx = read_index()
    for entry in (idx.get("docIndex") or []):
        doc_id = entry.get("id") or ""
        node_id = entry.get("originalNodeId") or entry.get("nodeId") or ""
        if not doc_id or not node_id:
            continue
        target_dir = _get_node_docs_dir(node_id, tree) or docs_dir
        try:
            matches = list(docs_dir.rglob(f"{doc_id}__*"))
            if not matches:
                matches = list(docs_dir.rglob(f"{doc_id}*"))
            if not matches:
                continue
            current = matches[0]
            if current.parent.resolve() == target_dir.resolve():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = target_dir / current.name
            if destination.exists():
                stem = current.stem
                suffix = current.suffix
                counter = 1
                while True:
                    candidate = target_dir / f"{stem}-{counter}{suffix}"
                    if not candidate.exists():
                        destination = candidate
                        break
                    counter += 1
            current.rename(destination)
        except OSError as e:
            print(f"[DMS] Warning: could not move {doc_id} into current folder path: {e}")


def _migrate_flat_docs() -> None:
    """Move any flat docs/{id}__* files into their per-node subdirectories."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return
    flat_files = [f for f in docs_dir.iterdir() if f.is_file()]
    if not flat_files:
        return
    idx = read_index()
    tree = idx.get("tree")
    if not tree:
        return
    moved = 0
    for entry in (idx.get("docIndex") or []):
        doc_id = entry.get("id") or ""
        node_id = entry.get("originalNodeId") or ""
        if not doc_id or not node_id:
            continue
        match = next((f for f in flat_files if f.name.startswith(f"{doc_id}__")), None)
        if not match:
            continue
        parts = _get_node_path_parts(tree, node_id)
        if not parts:
            continue
        target_dir = docs_dir.joinpath(*parts)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / match.name
        if not target.exists():
            match.rename(target)
            moved += 1
    if moved:
        print(f"[DMS] Migrated {moved} flat doc(s) into per-node subdirectories.")


def ext_for(mime: str, fallback_name: str) -> str:
    """Map MIME type to file extension; fall back to filename extension."""
    mime_map = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/svg+xml": "svg",
        "image/bmp": "bmp",
        "image/tiff": "tif",
        "image/heic": "heic",
        "image/heif": "heif",
        "text/plain": "txt",
        "text/csv": "csv",
        "text/html": "html",
        "text/xml": "xml",
        "application/json": "json",
        "application/xml": "xml",
        "application/zip": "zip",
        "application/x-zip-compressed": "zip",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-powerpoint": "ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    }
    if mime in mime_map:
        return mime_map[mime]
    if "." in fallback_name:
        return fallback_name.rsplit(".", 1)[-1].lower()
    return "bin"


# ---------------------------------------------------------------------------
# Password & session management
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str) -> str:
    """Slow-ish keyed hash. Not bcrypt, but stdlib-only and good enough for
    a local-machine app where the threat model is 'someone walks up and
    opens the browser tab'."""
    h = (salt + password).encode("utf-8")
    for _ in range(100_000):
        h = hashlib.sha256(h).digest()
    return h.hex()


def password_required() -> bool:
    cfg = load_config()
    return bool(cfg.get("password_hash"))


def verify_password(password: str) -> bool:
    cfg = load_config()
    if not cfg.get("password_hash"):
        return True  # no password set
    expected = cfg.get("password_hash", "")
    salt = cfg.get("password_salt", "")
    return hash_password(password, salt) == expected


# Active session tokens (in-memory only — restarting the server logs everyone out)
_SESSION_TOKENS: set[str] = set()

# Remote upload tokens — {token: {"node_id": str, "expiry": float}}
_REMOTE_TOKENS: dict[str, dict] = {}


def is_authenticated() -> bool:
    if not password_required():
        return True
    token = request.cookies.get("dms_session", "")
    return token in _SESSION_TOKENS


def issue_session_token() -> str:
    token = secrets.token_urlsafe(32)
    if len(_SESSION_TOKENS) > 10_000:
        _SESSION_TOKENS.clear()
    _SESSION_TOKENS.add(token)
    return token


# ---------------------------------------------------------------------------
# Hierarchy file parser
# ---------------------------------------------------------------------------
_LEVEL_LABEL_RE = re.compile(r'^level\s*\d+\s*$', re.IGNORECASE)


def parse_hierarchy_file(text: str):
    """
    Parse a hierarchy definition file.

    Format
    ------
    Blank lines and lines matching /^level\\s*\\d+$/i are ignored (readability
    labels only — not used as source of truth).

    Each node line is one of:
        NodeID                  <- root node  (no parent)
        NodeID  ParentNodeID    <- child node

    NodeID / ParentNodeID may contain any non-whitespace characters
    (letters, digits, #, -, _, ., etc.).

    Returns
    -------
    nodes  : dict  { node_id: parent_id_or_None }
    errors : list of human-readable error strings
    root   : str or None  (None when validation fails)
    """
    errors = []
    nodes  = {}        # node_id -> parent_id | None
    seen   = {}        # node_id -> line number  (duplicate detection)

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if _LEVEL_LABEL_RE.match(line):
            continue                            # skip readability labels

        parts = line.split()
        if len(parts) == 1:
            node_id, parent_id = parts[0], None
        elif len(parts) == 2:
            node_id, parent_id = parts[0], parts[1]
        else:
            errors.append(
                f"Line {lineno}: expected 1 or 2 tokens, got {len(parts)} — {raw!r}"
            )
            continue

        if node_id in seen:
            errors.append(
                f"Line {lineno}: duplicate NodeID '{node_id}' "
                f"(first seen on line {seen[node_id]})"
            )
            continue

        seen[node_id]  = lineno
        nodes[node_id] = parent_id

    # Structural validation
    roots = [nid for nid, pid in nodes.items() if pid is None]
    if not roots:
        errors.append(
            "No root node found. "
            "A root node has no ParentNodeID (it appears alone on its line)."
        )
    elif len(roots) > 1:
        errors.append(
            f"Multiple root nodes found: {roots}. Exactly one root is required."
        )

    for nid, pid in nodes.items():
        if pid is not None and pid not in nodes:
            errors.append(
                f"NodeID '{nid}' references unknown ParentNodeID '{pid}'."
            )

    def _has_cycle(start):
        visited = set()
        cur = start
        while cur is not None:
            if cur in visited:
                return True
            visited.add(cur)
            cur = nodes.get(cur)
        return False

    for nid in nodes:
        if _has_cycle(nid):
            errors.append(
                f"Circular reference detected involving NodeID '{nid}'. "
                "Check for parent-child loops."
            )
            break

    root = roots[0] if len(roots) == 1 else None
    return nodes, errors, root


def build_tree_order(nodes: dict, root: str) -> list:
    """
    Return (node_id, parent_id) pairs in BFS order — every parent is
    guaranteed to appear before its children.
    """
    children = defaultdict(list)
    for nid, pid in nodes.items():
        if pid is not None:
            children[pid].append(nid)

    order = []
    queue = deque([root])
    while queue:
        cur = queue.popleft()
        order.append((cur, nodes[cur]))
        for child in sorted(children[cur]):
            queue.append(child)
    return order


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per upload


# Endpoints that don't require authentication (login itself, plus auth status check)
_PUBLIC_ENDPOINTS = {
    "auth_status",
    "auth_login",
    "auth_logout",
    "mobile_upload_page",   # mobile page handles its own auth in JS
    "mobile_upload",        # allow upload without session on local network
}


@app.before_request
def enforce_auth():
    """Require a valid session cookie when password is enabled.

    Public endpoints (login, etc.) are always accessible. Static files (the
    main page) get a friendly login HTML; API endpoints get JSON 401.
    """
    # Allow login & status checks regardless
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None

    if not password_required():
        return None

    if is_authenticated():
        return None

    # Not authenticated. Decide what to send back.
    path = request.path
    if path.startswith("/api/"):
        return jsonify({"error": "Authentication required", "code": "auth_required"}), 401

    # For the main page, serve the login screen
    return Response(_LOGIN_PAGE_HTML, mimetype="text/html"), 401


_LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DMS — Sign in</title>
<style>
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background: #fafaf9; margin: 0; height: 100vh;
         display: flex; align-items: center; justify-content: center; }
  .card { background: white; border: 1px solid #d6d3d1; border-radius: 6px;
          padding: 28px; width: 360px; box-shadow: 0 2px 4px rgba(0,0,0,.04); }
  .kicker { font-size: 10px; letter-spacing: .2em; text-transform: uppercase;
            color: #78716c; margin-bottom: 4px; }
  h1 { font-size: 20px; margin: 0 0 16px; color: #1c1917; font-weight: 600; }
  label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
          color: #57534e; margin-bottom: 6px; }
  input[type=password] { width: 100%; padding: 8px 10px; font-size: 14px;
          border: 1px solid #d6d3d1; border-radius: 4px; box-sizing: border-box; }
  input[type=password]:focus { outline: none; border-color: #b45309; }
  button { width: 100%; margin-top: 12px; padding: 9px;
           background: #b45309; color: white; border: 0; border-radius: 4px;
           font-size: 14px; font-weight: 500; cursor: pointer; }
  button:hover { background: #92400e; }
  .error { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca;
           padding: 8px; border-radius: 4px; font-size: 12px; margin-top: 10px; display: none; }
</style>
</head>
<body>
<form class="card" onsubmit="return doLogin(event)">
  <div class="kicker">Engineering Documentation</div>
  <h1>Sign in to DMS</h1>
  <label for="pw">Password</label>
  <input id="pw" type="password" autofocus>
  <button type="submit">Sign in</button>
  <div id="err" class="error"></div>
</form>
<script>
async function doLogin(e) {
  e.preventDefault();
  const pw = document.getElementById('pw').value;
  const err = document.getElementById('err');
  err.style.display = 'none';
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pw})
  });
  if (r.ok) {
    window.location.href = '/';
  } else {
    let msg = 'Incorrect password.';
    try { msg = (await r.json()).error || msg; } catch {}
    err.textContent = msg;
    err.style.display = 'block';
    document.getElementById('pw').select();
  }
  return false;
}
</script>
</body>
</html>
"""


# ---- Static page -----------------------------------------------------------
@app.route("/")
def index_page():
    """Serve the main DMS HTML page."""
    html_path = SCRIPT_DIR / "dms.html"
    if not html_path.exists():
        return (
            "<h1>dms.html not found</h1>"
            f"<p>Expected at: <code>{html_path}</code></p>"
            "<p>Make sure dms.html is in the same folder as this server script.</p>",
            500,
        )
    response = send_file(html_path, mimetype="text/html")
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ---- Mobile upload page ----------------------------------------------------
_MOBILE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>DMS Upload</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#fafaf9;margin:0;padding:16px;color:#1c1917;min-height:100vh}
.card{background:white;border:1px solid #d6d3d1;border-radius:14px;
      padding:22px;max-width:480px;margin:0 auto;
      box-shadow:0 2px 10px rgba(0,0,0,.06)}
h1{font-size:19px;margin:0 0 3px;font-weight:700}
.sub{font-size:13px;color:#78716c;margin:0 0 14px}
.lbl{display:block;font-size:11px;font-weight:700;text-transform:uppercase;
     letter-spacing:.08em;color:#57534e;margin-bottom:5px;margin-top:12px}
select,input[type=password]{width:100%;padding:11px 12px;font-size:16px;
      border:1px solid #d6d3d1;border-radius:8px;background:white;color:#1c1917;
      -webkit-appearance:none;appearance:none}
input[type=file]{display:block;width:100%;font-size:16px;
      padding:10px 12px;border:1px solid #d6d3d1;border-radius:8px;
      background:white;color:#1c1917}
input[type=file]::file-selector-button{
      background:#f5f5f4;color:#1c1917;border:0;border-right:1px solid #d6d3d1;
      padding:8px 14px;margin-right:12px;border-radius:6px 0 0 6px;
      font-size:15px;font-weight:500;cursor:pointer}
.btn{display:block;width:100%;padding:14px;margin-top:18px;
     background:#b45309;color:white;border:0;border-radius:9px;
     font-size:17px;font-weight:700;cursor:pointer;
     -webkit-appearance:none;appearance:none;text-align:center}
.btn:active{background:#92400e}
.ok{margin-top:12px;padding:12px 14px;border-radius:8px;font-size:14px;
    background:#f0fdf4;color:#166534;border:1px solid #86efac}
.er{margin-top:12px;padding:12px 14px;border-radius:8px;font-size:14px;
    background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
#login{display:none}
</style>
</head>
<body>
<div class="card">
  <h1>QC Document Management</h1>
  <p class="sub">Upload documents from your phone</p>

  __BANNER__

  <!-- Login — shown only when a password is required -->
  <div id="login">
    <span class="lbl">Password</span>
    <input id="pw" type="password" placeholder="Enter password"
           autocomplete="current-password"
           onkeydown="if(event.key==='Enter')doLogin()"
           style="margin-bottom:4px">
    <button class="btn" onclick="doLogin()">Sign In</button>
    <div id="lmsg"></div>
  </div>

  <!-- Native HTML form — no JavaScript needed for upload -->
  <form id="upload" method="POST" action="/api/mobile/upload"
        enctype="multipart/form-data">

    __FOLDER_SELECTOR__
    __HIDDEN_TOKEN__

    <span class="lbl">📁 Choose files from library</span>
    <input type="file" name="file" multiple
           accept="image/*,application/pdf,text/*,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.zip">

    <span class="lbl">📷 Take a photo with camera</span>
    <input type="file" name="file" accept="image/*" capture="environment">

    <button type="submit" class="btn"
            onclick="this.textContent='Sending…';this.style.background='#555'">
      Upload
    </button>
  </form>
  <p style="margin-top:16px;font-size:10px;color:#ccc;text-align:center">v7</p>
</div>
<script>
async function init(){
  try{
    var r=await fetch('/api/auth/status');
    var d=await r.json();
    if(d.required&&!d.authenticated){
      document.getElementById('login').style.display='block';
      document.getElementById('upload').style.display='none';
    }
  }catch(e){}
}
async function doLogin(){
  var pw=document.getElementById('pw').value;
  var msg=document.getElementById('lmsg');
  try{
    var r=await fetch('/api/auth/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw})});
    if(r.ok){
      document.getElementById('login').style.display='none';
      document.getElementById('upload').style.display='block';
    }else{
      msg.className='er';msg.textContent='Incorrect password.';
    }
  }catch(e){msg.className='er';msg.textContent='Connection error.';}
}
init();
</script>
</body>
</html>"""


def _collect_nodes() -> list[tuple[str, str]]:
    """Return [(node_id, display_label), ...] for every tree node."""
    result: list[tuple[str, str]] = []
    try:
        if get_storage_root():
            idx = read_index()
            tree = idx.get("tree")
            if tree:
                stack = [(tree, "")]
                while stack:
                    node, prefix = stack.pop(0)
                    name = node.get("name") or node.get("id", "unknown")
                    label = f"{prefix} › {name}" if prefix else name
                    result.append((node["id"], label))
                    for child in node.get("children", []):
                        stack.append((child, label))
    except Exception:
        pass
    return result


def _build_folder_selector(selected_id: str = "", locked: bool = False) -> str:
    """Return the folder-selector HTML block for the mobile upload form.

    When *locked* is True the folder was pre-assigned via a remote token and
    the user should not be able to change it — render a read-only label plus a
    hidden input instead of a <select>.
    """
    nodes = _collect_nodes()

    def _safe(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    if locked and selected_id:
        folder_name = next((lbl for nid, lbl in nodes if nid == selected_id), selected_id)
        return (
            '<span class="lbl">Uploading to folder</span>'
            f'<p style="margin:4px 0 12px;font-size:15px;font-weight:600">{_safe(folder_name)}</p>'
            f'<input type="hidden" name="node_id" value="{_safe(selected_id)}">'
        )

    lines = ['<option value="">— No folder (unattached) —</option>']
    for nid, label in nodes:
        sel = ' selected' if nid == selected_id else ''
        lines.append(f'<option value="{_safe(nid)}"{sel}>{_safe(label)}</option>')
    return (
        '<span class="lbl">Attach to folder / component</span>'
        f'<select name="node_id">{"".join(lines)}</select>'
    )


@app.route("/mobile")
@app.route("/mobile/<path:_version>")
def mobile_upload_page(_version=None):
    """Serve the mobile-friendly upload page.

    Folder options are server-side rendered; the URL version segment forces
    iOS Safari past its page cache. Success/error banners come via ?ok= / ?err=.
    """
    ok  = request.args.get("ok",  "")
    err = request.args.get("err", "")
    if ok:
        banner = f'<div class="ok">&#10003; {ok}</div>'
    elif err:
        banner = f'<div class="er">&#9888; {err}</div>'
    else:
        banner = ""

    html = (_MOBILE_PAGE_HTML
            .replace("__FOLDER_SELECTOR__", _build_folder_selector())
            .replace("__HIDDEN_TOKEN__", "")
            .replace("__BANNER__", banner))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---- Remote-token endpoints ------------------------------------------------

def _purge_expired_tokens() -> None:
    now = _time.time()
    expired = [t for t, info in _REMOTE_TOKENS.items() if info["expiry"] < now]
    for t in expired:
        del _REMOTE_TOKENS[t]


@app.route("/api/remote-token", methods=["POST"])
def create_remote_token():
    """Create a 24-hour upload token tied to a specific folder.

    Body: {"node_id": "NODE-XXXX"}  (empty string = unattached)
    Response: {"token": "<hex>"}
    """
    _purge_expired_tokens()
    data = request.get_json(silent=True) or {}
    node_id = (data.get("node_id") or "").strip()
    token = secrets.token_hex(16)
    _REMOTE_TOKENS[token] = {"node_id": node_id, "expiry": _time.time() + 86400}
    return jsonify({"token": token})


@app.route("/mobile/r/<token>")
def remote_mobile_page(token: str):
    """Token-gated mobile upload page for remote access via ngrok."""
    info = _REMOTE_TOKENS.get(token)
    if not info or _time.time() > info["expiry"]:
        return Response(
            "<!DOCTYPE html><html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>Link expired</h2><p>This upload link has expired or is invalid. "
            "Please ask for a new one.</p></body></html>",
            status=410,
            mimetype="text/html",
        )

    ok  = request.args.get("ok",  "")
    err = request.args.get("err", "")
    if ok:
        banner = f'<div class="ok">&#10003; {ok}</div>'
    elif err:
        banner = f'<div class="er">&#9888; {err}</div>'
    else:
        banner = ""

    node_id = info["node_id"]
    hidden_token = f'<input type="hidden" name="token" value="{token}">'
    html = (_MOBILE_PAGE_HTML
            .replace("__FOLDER_SELECTOR__", _build_folder_selector(node_id, locked=bool(node_id)))
            .replace("__HIDDEN_TOKEN__", hidden_token)
            .replace("__BANNER__", banner))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---- Mobile upload API -----------------------------------------------------
@app.route("/api/mobile/upload", methods=["POST"])
def mobile_upload():
    """Handle the native-form upload from the mobile page.

    Accepts multipart/form-data with one or more 'file' parts and a 'node_id'.
    On success redirects back to /mobile/<v>?ok=... so the user sees a
    confirmation without needing JavaScript.
    """
    v = int(_time.time())
    token = (request.form.get("token") or "").strip()

    # If a remote token was submitted, validate it and use its node_id.
    token_node_id = ""
    if token:
        info = _REMOTE_TOKENS.get(token)
        if not info or _time.time() > info["expiry"]:
            return redirect(f"/mobile/{v}?err={_url_quote('Upload link has expired. Please request a new one.')}")
        token_node_id = info["node_id"]

    def _redirect_err(msg: str):
        if token and _REMOTE_TOKENS.get(token):
            return redirect(f"/mobile/r/{token}?err={_url_quote(msg)}")
        return redirect(f"/mobile/{v}?err={_url_quote(msg)}")

    def _redirect_ok(msg: str):
        if token and _REMOTE_TOKENS.get(token):
            return redirect(f"/mobile/r/{token}?ok={_url_quote(msg)}")
        return redirect(f"/mobile/{v}?ok={_url_quote(msg)}")

    docs_dir = get_docs_dir()
    if not docs_dir:
        return _redirect_err("Storage path not configured — open DMS on Mac first")

    all_files = request.files.getlist("file")
    all_files = [f for f in all_files if f and f.filename]
    if not all_files:
        return _redirect_err("No files received — please choose at least one file")

    node_id = token_node_id or (request.form.get("node_id") or "").strip()
    idx = read_index()
    tree = idx.get("tree")
    photo_root_node_id = idx.get("photoRootNodeId", "") or node_id or ""
    names: list[str] = []

    for f in all_files:
        doc_id = "DOC-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()
        mime = f.mimetype or "application/octet-stream"
        # iOS often sends application/octet-stream for text/office files;
        # use mimetypes to get a proper type from the filename instead.
        if mime in ("application/octet-stream", "") and f.filename:
            guessed = mimetypes.guess_type(f.filename)[0]
            if guessed:
                mime = guessed
        ext = ext_for(mime, f.filename or "")
        orig_name = f.filename or f"upload.{ext}"
        safe_name = _safe_filename_part(orig_name)
        if not Path(safe_name).suffix:
            safe_name = f"{safe_name}.{ext}"

        data = f.read()
        is_photo = _is_photo_file(ext, mime)
        photo_year, photo_month = (None, None)
        if is_photo:
            photo_year, photo_month = _read_photo_date(data, mime)

        if is_photo and _valid_photo_year_month(photo_year, photo_month):
            attach_node_id = _find_or_create_photo_month_node(
                tree, photo_root_node_id or None, photo_year, photo_month)
            out_dir = (
                _get_node_docs_dir(attach_node_id, tree)
                if tree and _path_exists_for_node(tree, attach_node_id)
                else _photo_year_month_dir(
                    docs_dir, tree, photo_year, photo_month,
                    base_node_id=photo_root_node_id or None,
                )
            ) or docs_dir
        elif node_id and tree is not None:
            attach_node_id = node_id
            out_dir = _get_node_docs_dir(attach_node_id, tree) or docs_dir
        else:
            attach_node_id = node_id
            out_dir = docs_dir

        out_path = out_dir / f"{doc_id}__{safe_name}"
        out_path.write_bytes(data)

        doc_entry = {
            "id": doc_id,
            "name": f.filename or f"upload.{ext}",
            "mime": mime,
            "size": out_path.stat().st_size,
            "uploadedAt": datetime.utcnow().isoformat() + "Z",
            "originalNodeId": attach_node_id or None,
            "metadata": {},
        }
        idx.setdefault("docIndex", []).append(doc_entry)

        if attach_node_id and idx.get("tree"):
            def _attach(node, _doc_id=doc_id, _node_id=attach_node_id):
                if node.get("id") == _node_id:
                    node.setdefault("documents", []).append({"id": _doc_id})
                    return True
                return any(_attach(c) for c in node.get("children", []))
            _attach(idx["tree"])

        names.append(f.filename or doc_entry["name"])

    write_index(idx)
    ok_msg = f"Uploaded {len(names)} file(s): {', '.join(names)}"
    return _redirect_ok(ok_msg)


# ---- Authentication --------------------------------------------------------
@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    """Tell the client whether a password is required and whether they're in."""
    return jsonify({
        "password_required": password_required(),
        "authenticated": is_authenticated(),
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True) or {}
    pw = data.get("password", "")
    if not password_required():
        # No password set — issue a token anyway so the client behaves consistently
        token = issue_session_token()
        resp = jsonify({"ok": True, "no_password": True})
        resp.set_cookie("dms_session", token, httponly=True, samesite="Strict")
        return resp
    if not pw or not verify_password(pw):
        return jsonify({"error": "Incorrect password"}), 401
    token = issue_session_token()
    resp = jsonify({"ok": True})
    resp.set_cookie("dms_session", token, httponly=True, samesite="Strict")
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.cookies.get("dms_session", "")
    _SESSION_TOKENS.discard(token)
    resp = jsonify({"ok": True})
    resp.delete_cookie("dms_session")
    return resp


@app.route("/api/auth/set-password", methods=["POST"])
def auth_set_password():
    """Set, change, or clear the password.

    Body: { "current_password": "...", "new_password": "..." }
    - To set initially: current_password may be empty
    - To change: must provide correct current_password
    - To clear: new_password = "" (still requires current_password if one exists)
    """
    # If a password is already set, we require auth to change it (defense in depth)
    if password_required() and not is_authenticated():
        return jsonify({"error": "Authentication required"}), 401

    data = request.get_json(force=True) or {}
    current = data.get("current_password", "")
    new = data.get("new_password", "")

    cfg = load_config()
    has_existing = bool(cfg.get("password_hash"))

    if has_existing:
        if not verify_password(current):
            return jsonify({"error": "Current password is incorrect"}), 401

    if new:
        if len(new) < 4:
            return jsonify({"error": "Password must be at least 4 characters"}), 400
        salt = secrets.token_hex(16)
        cfg["password_salt"] = salt
        cfg["password_hash"] = hash_password(new, salt)
    else:
        # Clearing the password
        cfg["password_salt"] = ""
        cfg["password_hash"] = ""
        # Invalidate all existing sessions when password is cleared/changed
        _SESSION_TOKENS.clear()

    save_config(cfg)

    # Re-issue a session for the requester so they're not locked out
    token = issue_session_token()
    resp = jsonify({"ok": True, "password_now_set": bool(new)})
    resp.set_cookie("dms_session", token, httponly=True, samesite="Strict")
    return resp


# ---- Settings (storage path) ----------------------------------------------
@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return current settings + diagnostics about the storage folder."""
    cfg = load_config()
    root = get_storage_root()
    info = {
        "storage_path": cfg.get("storage_path", ""),
        "resolved_path": str(root) if root else "",
        "configured": bool(root),
        "exists": bool(root and root.exists()),
        "writable": False,
        "doc_count": 0,
        "error": None,
    }
    if root and root.exists():
        # Probe writability with a temp file
        try:
            test = root / ".dms_write_test"
            test.write_text("ok")
            test.unlink()
            info["writable"] = True
        except OSError as e:
            info["error"] = f"Folder exists but is not writable: {e}"
        # Count docs in the docs/ subfolder
        try:
            docs = root / "docs"
            if docs.exists():
                info["doc_count"] = sum(1 for _ in docs.iterdir() if _.is_file())
        except OSError:
            pass
    elif root and not root.exists():
        info["error"] = "Folder does not exist yet — it will be created on save."
    return jsonify(info)


@app.route("/api/settings", methods=["POST"])
def set_settings():
    """Update the storage path. Creates the folder if it doesn't exist."""
    data = request.get_json(force=True) or {}
    new_path = (data.get("storage_path") or "").strip()
    if not new_path:
        return jsonify({"error": "storage_path is required"}), 400

    p = Path(new_path).expanduser().resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
        # Probe writability
        test = p / ".dms_write_test"
        test.write_text("ok")
        test.unlink()
        # Ensure docs/ subfolder exists too
        (p / "docs").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create or write to folder: {e}"}), 400

    cfg = load_config()
    cfg["storage_path"] = str(p)
    save_config(cfg)
    return jsonify({"ok": True, "resolved_path": str(p)})


# ---- Tree ------------------------------------------------------------------
@app.route("/api/tree", methods=["GET"])
def get_tree():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    idx = read_index()
    return jsonify({"tree": idx.get("tree"), "photoRootNodeId": idx.get("photoRootNodeId", "")})


@app.route("/api/tree", methods=["PUT"])
def put_tree():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    data = request.get_json(force=True) or {}
    idx = read_index()
    idx["tree"] = data.get("tree")
    write_index(idx)
    _create_local_folder_structure(idx.get("tree"))
    _sync_doc_files_to_tree_paths(idx.get("tree"))
    return jsonify({"ok": True})


@app.route("/api/photo-root", methods=["PUT"])
def put_photo_root():
    """Set or clear the photo root node id.

    Body: { "node_id": "NODE-XXXX" }  — empty string clears the setting.
    """
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    data = request.get_json(force=True) or {}
    node_id = (data.get("node_id") or "").strip()
    idx = read_index()
    if node_id:
        idx["photoRootNodeId"] = node_id
    else:
        idx.pop("photoRootNodeId", None)
    write_index(idx)
    return jsonify({"ok": True, "photoRootNodeId": node_id})


# ---- Document index --------------------------------------------------------
@app.route("/api/doc-index", methods=["GET"])
def get_doc_index():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    idx = read_index()
    return jsonify({"docIndex": idx.get("docIndex", [])})


@app.route("/api/doc-index", methods=["PUT"])
def put_doc_index():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    data = request.get_json(force=True) or {}
    idx = read_index()
    idx["docIndex"] = data.get("docIndex", [])
    write_index(idx)
    return jsonify({"ok": True})


# ---- Global key parameters -------------------------------------------------
@app.route("/api/key-parameters", methods=["GET"])
def get_key_parameters():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    idx = read_index()
    keys = idx.get("keyParameters")
    if not keys:
        keys = DEFAULT_KEY_PARAMETERS
    return jsonify({"keyParameters": normalize_key_parameters(keys)})


@app.route("/api/key-parameters", methods=["PUT"])
def put_key_parameters():
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503
    data = request.get_json(force=True) or {}
    idx = read_index()
    idx["keyParameters"] = normalize_key_parameters(data.get("keyParameters", []))
    write_index(idx)
    return jsonify({"ok": True, "keyParameters": idx["keyParameters"]})


# ---- Documents (binary upload/download) -----------------------------------
@app.route("/api/docs/<doc_id>", methods=["GET"])
def get_doc(doc_id):
    """Return the binary content of a document (PDF, image, etc.)."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    # Search recursively — files may be in per-node subdirectories.
    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    if not matches:
        abort(404)
    file_path = matches[0]

    # Look up the original filename from the index for proper Content-Disposition
    idx = read_index()
    meta = next((d for d in idx.get("docIndex", []) if d.get("id") == doc_id), None)
    download_name = meta["name"] if meta else file_path.name

    stored_mime = meta.get("mime") if meta else None
    if stored_mime and stored_mime != "application/octet-stream":
        mime = stored_mime
    else:
        mime = mimetypes.guess_type(str(file_path))[0] or stored_mime or "application/octet-stream"

    return send_file(
        file_path,
        mimetype=mime,
        download_name=download_name,
        as_attachment=False,
    )


@app.route("/api/docs", methods=["POST"])
def post_doc():
    """Upload a document. Expects multipart form: file=<binary>, doc_id=<DOC-ID>."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    if "file" not in request.files:
        return jsonify({"error": "Missing 'file' part"}), 400
    f = request.files["file"]
    doc_id = request.form.get("doc_id", "").strip()
    if not doc_id:
        return jsonify({"error": "Missing 'doc_id' field"}), 400
    # Defensive: prevent path traversal
    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        return jsonify({"error": "Invalid doc_id"}), 400

    node_id = request.form.get("node_id", "").strip() or None

    mime = f.mimetype or "application/octet-stream"
    # Improve MIME from file extension if browser sent generic type
    orig_name = f.filename or "upload"
    if mime in ("application/octet-stream", "") and orig_name:
        guessed = mimetypes.guess_type(orig_name)[0]
        if guessed:
            mime = guessed
    ext = ext_for(mime, orig_name)
    safe_name = _safe_filename_part(orig_name)
    if not Path(safe_name).suffix:
        safe_name = f"{safe_name}.{ext}"

    photo_year = request.form.get("photo_year", "").strip() or None
    photo_month = request.form.get("photo_month", "").strip() or None
    route_mode = request.form.get("route_mode", "year-month").strip() or "year-month"
    photo_base_node_id = request.form.get("photo_base_node_id", "").strip() or None
    file_data = f.read()

    is_photo = _is_photo_file(ext, mime)
    _valid_date = _valid_photo_year_month(photo_year, photo_month)
    if is_photo and not _valid_date and route_mode != "current-folder":
        photo_year, photo_month = _read_photo_date(file_data, mime)
        _valid_date = _valid_photo_year_month(photo_year, photo_month)
    photo_lat = request.form.get("photo_lat", "").strip() or None
    photo_lon = request.form.get("photo_lon", "").strip() or None

    print(
        f"[DMS] upload: {orig_name!r} mime={mime!r} photo={photo_year}/{photo_month} "
        f"valid={_valid_date} route={route_mode!r} node={node_id!r} base={photo_base_node_id!r}"
    )
    tree = read_index().get("tree")
    if is_photo and _valid_date and route_mode != "current-folder":
        node_parts = _get_node_path_parts(tree, node_id) if tree and node_id else []
        date_parts = [_safe_folder_name(photo_year), _safe_folder_name(photo_month)]
        if len(node_parts) >= 2 and node_parts[-2:] == date_parts:
            out_dir = docs_dir.joinpath(*node_parts)
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = _photo_year_month_dir(
                docs_dir, tree, photo_year, photo_month,
                base_node_id=photo_base_node_id or node_id,
            )
    elif node_id:
        out_dir = _get_node_docs_dir(node_id, tree) or docs_dir
    else:
        out_dir = docs_dir

    out_path = out_dir / f"{doc_id}__{safe_name}"
    out_path.write_bytes(file_data)

    location = None
    if photo_lat and photo_lon:
        try:
            location = _reverse_geocode(float(photo_lat), float(photo_lon))
            if location:
                print(f"[DMS] GPS location: {location}")
        except (ValueError, Exception):
            pass
    if location is None:
        # Client couldn't extract GPS (e.g. HEIC, or memory pressure on large folder uploads)
        # — try server-side Pillow fallback for any image format
        lat, lon = _read_photo_gps(file_data)
        if lat is not None:
            try:
                location = _reverse_geocode(lat, lon)
                if location:
                    print(f"[DMS] GPS location (server fallback): {location}")
            except Exception:
                pass

    return jsonify({"ok": True, "filename": out_path.name, "size": out_path.stat().st_size,
                    "path": str(out_path), "location": location})


@app.route("/api/docs/<doc_id>/preview", methods=["GET"])
def get_doc_preview(doc_id):
    """Return a browser-viewable image, converting HEIC/HEIF to JPEG on the fly."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        return jsonify({"error": "Invalid doc_id"}), 400

    matches = list(docs_dir.rglob(f"{doc_id}__*")) or list(docs_dir.rglob(f"{doc_id}*"))
    if not matches:
        abort(404)
    file_path = matches[0]

    ext = file_path.suffix.lower().lstrip(".")
    if ext in ("heic", "heif"):
        try:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            from PIL import Image
            from io import BytesIO
            img = Image.open(file_path).convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=88)
            buf.seek(0)
            return Response(buf.read(), mimetype="image/jpeg")
        except Exception as e:
            print(f"[DMS] HEIC preview conversion failed: {e}")
            return jsonify({"error": f"Cannot preview HEIC: {e}. Install pillow-heif: pip install pillow-heif"}), 500

    # Non-HEIC: serve the file directly
    idx = read_index()
    meta = next((d for d in idx.get("docIndex", []) if d.get("id") == doc_id), None)
    download_name = meta["name"] if meta else file_path.name
    stored_mime = meta.get("mime") if meta else None
    mime = (stored_mime if stored_mime and stored_mime != "application/octet-stream"
            else mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
    return send_file(file_path, mimetype=mime, download_name=download_name, as_attachment=False)


@app.route("/api/docs/<doc_id>", methods=["DELETE"])
def delete_doc(doc_id):
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    for m in matches:
        try:
            m.unlink()
        except OSError as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "deleted": [m.name for m in matches]})


# ---- OCR / text extraction ------------------------------------------------
@app.route("/api/ocr/status", methods=["GET"])
def get_ocr_status():
    """Report whether OCR is available and what it can handle.

    The UI uses this to decide whether to show the OCR button as ready,
    or to explain to the user what they need to install.
    """
    try:
        import pdf_extraction
    except Exception as e:
        return jsonify({
            "tesseract_available": False,
            "tesseract_version": None,
            "pdf_rasterizer": None,
            "image_ocr": False,
            "scanned_pdf_ocr": False,
            "error": f"OCR module unavailable: {e}",
        })
    return jsonify(pdf_extraction.ocr_status())


@app.route("/api/docs/<doc_id>/ocr", methods=["POST"])
def post_doc_ocr(doc_id):
    """Run OCR / text extraction on a stored document and return the text.

    Works for PDFs (text layer first, OCR fallback for scanned pages) and
    for image files (JPEG/PNG/TIFF/... via Tesseract). The extracted text
    is returned to the client; persisting it into the doc index (so it
    becomes searchable) is the frontend's job via PUT /api/doc-index.
    """
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        return jsonify({"error": "Invalid doc_id"}), 400

    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    if not matches:
        return jsonify({"error": "Document not found on disk"}), 404
    file_path = matches[0]

    try:
        import pdf_extraction
    except Exception as e:
        return jsonify({"error": f"OCR module unavailable: {e}"}), 500

    # force=1 (query) or {"force_ocr": true} (JSON) ignores any text layer and
    # OCRs the rendered image — used when a PDF carries a corrupt text layer.
    force_ocr = request.args.get("force") in ("1", "true", "yes")
    if not force_ocr and request.is_json:
        force_ocr = bool((request.get_json(silent=True) or {}).get("force_ocr"))

    try:
        text, info = pdf_extraction.extract_text_from_file(file_path, force_ocr=force_ocr)
    except Exception as e:
        return jsonify({"error": f"OCR failed: {e}"}), 500

    status = pdf_extraction.ocr_status()
    is_pdf = file_path.suffix.lower() == ".pdf"

    # A helpful, actionable message keyed to what's actually missing.
    hint = ""
    def _ocr_requirements_msg(is_pdf_doc):
        """List exactly which OCR components are missing, so the user installs
        everything needed in one go (PDF OCR needs Tesseract AND a rasterizer)."""
        missing = []
        if not status["tesseract_available"]:
            missing.append("Tesseract OCR engine (Windows: UB-Mannheim installer; "
                           "Mac: 'brew install tesseract')")
        if is_pdf_doc and not status["pdf_rasterizer"]:
            missing.append("a PDF rasterizer to turn the page into an image "
                           "('pip install PyMuPDF' in the same Python you run the app)")
        if not missing:
            return ""
        return "OCR needs " + (" and ".join(missing)) + "."

    if info.get("garbage_text_layer"):
        # Text layer existed but was gibberish, and OCR couldn't replace it.
        req = _ocr_requirements_msg(is_pdf)
        if req:
            hint = ("This PDF's text layer is corrupted, so the page must be read "
                    "by OCR instead. " + req + " Then click OCR again.")
        else:
            hint = "This PDF's text layer is unreadable and OCR found no legible text."
    elif not text:
        req = _ocr_requirements_msg(is_pdf)
        if req:
            hint = "No usable text layer was found, so OCR is needed. " + req
        else:
            hint = "No readable text was found in this document."

    return jsonify({
        "ok": True,
        "doc_id": doc_id,
        "text": text,
        "char_count": len(text),
        "forced": force_ocr,
        "info": info,
        "hint": hint,
        # capability snapshot so the UI can tell the user precisely what's set up
        "ocr_capability": status,
    })


@app.route("/api/docs/<doc_id>/extract-keys", methods=["POST"])
def post_doc_extract_keys(doc_id):
    """Extract user-defined key strings from a stored document.

    Body (JSON):
      {
        "keys": ["Assembly Id", "Max Working Pressure", ...],  # required
        "force_ocr": false,      # optional: ignore text layer, OCR the image
        "same_line_only": true,  # optional (default true): take value from the
                                 #   same line; set false to also try next line
        "text": "..."            # optional: extract from THIS text instead of
                                 #   re-reading the file (e.g. OCR text the user
                                 #   already reviewed/corrected in the panel)
      }

    Returns { ok, doc_id, results: { key: {value, found} }, info, hint }.
    Keys that aren't found come back with value "NF".
    """
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        return jsonify({"error": "Invalid doc_id"}), 400

    payload = request.get_json(silent=True) or {}
    keys = payload.get("keys") or []
    if not isinstance(keys, list) or not any(str(k).strip() for k in keys):
        return jsonify({"error": "Provide a non-empty 'keys' list"}), 400

    try:
        import pdf_extraction
    except Exception as e:
        return jsonify({"error": f"Extraction module unavailable: {e}"}), 500

    supplied_text = payload.get("text")
    info = {}
    if isinstance(supplied_text, str) and supplied_text.strip():
        # Extract from the text the client already has (post-OCR, possibly edited).
        text = supplied_text
        info = {"method": "supplied", "used_ocr": False}
    else:
        matches = list(docs_dir.rglob(f"{doc_id}__*")) or list(docs_dir.rglob(f"{doc_id}*"))
        if not matches:
            return jsonify({"error": "Document not found on disk"}), 404
        force_ocr = bool(payload.get("force_ocr"))
        try:
            text, info = pdf_extraction.extract_text_from_file(matches[0], force_ocr=force_ocr)
        except Exception as e:
            return jsonify({"error": f"Text extraction failed: {e}"}), 500

    try:
        same_line_only = payload.get("same_line_only", True)
        results = pdf_extraction.extract_key_strings(
            text, keys, same_line_only=bool(same_line_only))
    except Exception as e:
        return jsonify({"error": f"Key extraction failed: {e}"}), 500

    found_n = sum(1 for v in results.values() if v.get("found"))
    hint = ""
    if not text:
        hint = ("No text could be read from this document, so every key is NF. "
                "Try OCR first (or 强制 OCR for a scanned/corrupt PDF).")
    elif found_n == 0:
        hint = ("None of the keys were found. Check spelling/case, or confirm "
                "the key strings actually appear as labels in this document.")

    return jsonify({
        "ok": True,
        "doc_id": doc_id,
        "results": results,
        "found_count": found_n,
        "total_keys": len(results),
        "info": info,
        "hint": hint,
        # Diagnostics: what text did we actually read? Lets the UI show a
        # preview when nothing matched, so the user can see whether the issue
        # is empty/garbled text, a column-split layout, or a real mismatch.
        "text_char_count": len(text or ""),
        "text_preview": (text or "")[:600],
    })


# ---- Databook generation --------------------------------------------------
@app.route("/api/databook", methods=["POST"])
def post_databook():
    """
    Build a databook PDF from a user-specified selection of documents.

    Request JSON:
      {
        "title": "Optional title",
        "subtitle": "Optional subtitle",
        "selection": [
          { "nodeId": "NODE-...", "docIds": ["DOC-...", "DOC-..."] },
          ...
        ]
      }

    Response: PDF binary stream.
    """
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503

    data = request.get_json(force=True) or {}
    selection = data.get("selection") or []
    title = data.get("title", "").strip() or "Engineering Databook"
    subtitle = data.get("subtitle", "").strip()

    if not selection:
        return jsonify({"error": "No documents selected"}), 400

    docs_dir = get_docs_dir()
    idx = read_index()

    try:
        from databook import build_databook
        pdf_bytes = build_databook(
            selection=selection,
            docs_dir=docs_dir,
            doc_index=idx.get("docIndex", []),
            tree=idx.get("tree"),
            title=title,
            subtitle=subtitle,
        )
    except ImportError as e:
        return jsonify({
            "error": (
                "Databook libraries not installed. Run:\n"
                "    pip3 install pypdf reportlab Pillow\n"
                "Then restart the server.\n\nDetail: " + str(e)
            )
        }), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Databook generation failed: {e}"}), 500

    # Build an ASCII-safe fallback name (non-ASCII chars → underscore) for the
    # Content-Disposition header, which must be 7-bit ASCII per RFC 7230.
    ascii_name = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_ ")) else "_" for c in title
    )[:60].strip()
    ascii_filename = f"{ascii_name or 'Databook'}.pdf"

    # Also expose the original UTF-8 name via the RFC 5987 filename* parameter
    # so browsers that support it will use the proper Unicode filename.
    from urllib.parse import quote as _pct_encode
    utf8_filename = _pct_encode(f"{title.strip() or 'Databook'}.pdf", safe="")

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{utf8_filename}"
            ),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# ---- Combine documents into a single PDF saved in the same folder ----------
@app.route("/api/docs/combine-to-folder", methods=["POST"])
def combine_to_folder():
    """
    Combine selected documents into one PDF, save it to a target folder node,
    and unlink the source documents from their respective nodes (the underlying
    files are kept in storage — only the folder references are removed).

    Request JSON:
      {
        "node_id":  "NODE-XXXX",           // folder that receives the combined file
        "title":    "My Combined Doc",     // filename (without .pdf)
        "selection": [{nodeId, docIds}]    // same format as /api/databook
      }

    Response: { "ok": true, "doc_id": "DOC-...", "name": "...", "size": N }
    """
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503

    data = request.get_json(force=True) or {}
    node_id = (data.get("node_id") or "").strip()
    title = (data.get("title") or "").strip()
    selection = data.get("selection") or []

    if not node_id:
        return jsonify({"error": "node_id is required"}), 400
    if not selection:
        return jsonify({"error": "No documents selected"}), 400

    docs_dir = get_docs_dir()
    idx = read_index()

    try:
        from databook import build_databook
        pdf_bytes = build_databook(
            selection=selection,
            docs_dir=docs_dir,
            doc_index=idx.get("docIndex", []),
            tree=idx.get("tree"),
            title=title or "Combined",
            subtitle="",
        )
    except ImportError as e:
        return jsonify({
            "error": (
                "Databook libraries not installed. Run:\n"
                "    pip3 install pypdf reportlab Pillow\n"
                "Then restart the server.\n\nDetail: " + str(e)
            )
        }), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Combine failed: {e}"}), 500

    # Persist the combined PDF in the docs directory
    new_doc_id = "DOC-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()
    display_name = title.strip() or "Combined"
    if not display_name.lower().endswith(".pdf"):
        display_name += ".pdf"
    safe_fn = _safe_filename_part(display_name)
    out_path = docs_dir / f"{new_doc_id}__{safe_fn}"
    out_path.write_bytes(pdf_bytes)

    # Add the new combined doc to docIndex
    new_doc = {
        "id": new_doc_id,
        "name": display_name,
        "mime": "application/pdf",
        "size": len(pdf_bytes),
        "uploadedAt": datetime.utcnow().isoformat() + "Z",
        "originalNodeId": node_id,
        "metadata": {},
    }
    idx.setdefault("docIndex", []).append(new_doc)

    # Build a map of node_id → {doc_ids to unlink} from the selection
    to_unlink: dict[str, set] = {}
    for entry in selection:
        nid = (entry.get("nodeId") or "").strip()
        dids = entry.get("docIds") or []
        if nid and dids:
            to_unlink.setdefault(nid, set()).update(dids)

    # Walk the tree: unlink source docs and attach the new combined doc
    def _update_node(node):
        nid = node.get("id", "")
        if nid in to_unlink:
            node["documents"] = [
                d for d in (node.get("documents") or [])
                if d.get("id") not in to_unlink[nid]
            ]
        if nid == node_id:
            node.setdefault("documents", []).append({"id": new_doc_id})
        for child in (node.get("children") or []):
            _update_node(child)

    if idx.get("tree"):
        _update_node(idx["tree"])

    write_index(idx)

    return jsonify({
        "ok": True,
        "doc_id": new_doc_id,
        "name": display_name,
        "size": len(pdf_bytes),
    })


# ---- Metadata CSV export ---------------------------------------------------

def _build_node_lookup(tree) -> dict:
    """Return {node_id: {"sn": str}} for every node in the tree."""
    lookup: dict[str, dict] = {}
    if not tree:
        return lookup
    stack = [tree]
    while stack:
        node = stack.pop(0)
        lookup[node["id"]] = {"sn": node.get("sn", "")}
        for child in node.get("children", []):
            stack.append(child)
    return lookup


@app.route("/api/export-csv", methods=["GET"])
def export_metadata_csv():
    """Download a CSV file with one row per document and metadata as columns."""
    import io as _io
    import csv as _csv

    idx = read_index()
    docs = idx.get("docIndex", [])
    node_lookup = _build_node_lookup(idx.get("tree"))

    # Collect unique metadata keys in order of first appearance
    seen_keys: set[str] = set()
    meta_keys: list[str] = []
    for doc in docs:
        for k in doc.get("metadata", {}).keys():
            if k not in seen_keys:
                seen_keys.add(k)
                meta_keys.append(k)

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["Document Name", "Upload Date", "Serial Number"] + meta_keys)

    for doc in docs:
        node_id = doc.get("originalNodeId", "")
        sn = node_lookup.get(node_id, {}).get("sn", "")
        uploaded = (doc.get("uploadedAt") or "")[:10]  # YYYY-MM-DD only
        meta = doc.get("metadata", {})
        row = [doc.get("name", ""), uploaded, sn] + [meta.get(k, "") for k in meta_keys]
        writer.writerow(row)

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM lets Excel open UTF-8 correctly

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = get_storage_root()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (root.name if root else "dms"))[:40]
    filename = f"{safe or 'dms'}-metadata-{stamp}.csv"

    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
        },
    )


# ---- Project file management (export / import / new) ---------------------
@app.route("/api/project/export", methods=["GET"])
def export_project():
    """Bundle index.json + docs/ into a .dms zip saved next to index.json."""
    root = get_storage_root()
    if not root or not root.exists():
        return jsonify({"error": "Storage path not configured"}), 503

    # Build a zip in memory
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "format": "dms-project",
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": str(root),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        index_path = root / "index.json"
        if index_path.exists():
            zf.write(index_path, arcname="index.json")

        docs_dir = root / "docs"
        if docs_dir.exists():
            for f in docs_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname="docs/" + f.relative_to(docs_dir).as_posix())

    zip_bytes = buf.getvalue()

    # Save the .dms file in the same folder as index.json
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name)[:40]
    filename = f"{safe or 'dms-project'}-{stamp}.dms"
    out_path = root / filename
    out_path.write_bytes(zip_bytes)

    return jsonify({"ok": True, "path": str(out_path), "filename": filename})


@app.route("/api/project/import", methods=["POST"])
def import_project():
    """
    Import a .dms file into a target folder and switch the DMS to use it.
    Multipart form fields:
      file: the .dms file
      target_path: where to extract it (will be created)
      mode: 'new' (target must be empty) | 'overwrite' (delete existing contents first)
    """
    if "file" not in request.files:
        return jsonify({"error": "Missing 'file' part"}), 400

    f = request.files["file"]
    target_path = (request.form.get("target_path") or "").strip()
    mode = (request.form.get("mode") or "overwrite").strip()

    if not target_path:
        current = get_storage_root()
        if not current:
            return jsonify({"error": "No active project folder. Create a project first."}), 400
        target_path = str(current)

    target = Path(target_path).expanduser().resolve()

    # Read uploaded file into a temp file for zipfile to read
    tmp_zip = tempfile.NamedTemporaryFile(suffix=".dms", delete=False)
    try:
        f.save(tmp_zip.name)
        tmp_zip.close()

        # Validate it's a real .dms zip with a manifest
        try:
            with zipfile.ZipFile(tmp_zip.name, "r") as zf:
                names = zf.namelist()
                if "manifest.json" not in names:
                    return jsonify({"error": "Not a valid .dms project file (missing manifest.json)"}), 400
                manifest = json.loads(zf.read("manifest.json"))
                if manifest.get("format") != "dms-project":
                    return jsonify({"error": "Not a DMS project file"}), 400
        except zipfile.BadZipFile:
            return jsonify({"error": "Uploaded file is not a valid .dms (zip) file"}), 400

        # Handle the target folder
        if target.exists():
            if any(target.iterdir()):
                if mode == "overwrite":
                    # Delete contents but keep the folder itself
                    for item in target.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                else:
                    return jsonify({
                        "error": (
                            f"Target folder '{target}' is not empty. "
                            "Either choose an empty folder, or set mode='overwrite' "
                            "to replace its contents."
                        )
                    }), 400
        else:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return jsonify({"error": f"Cannot create target folder: {e}"}), 400

        # Extract everything except the manifest into the target
        with zipfile.ZipFile(tmp_zip.name, "r") as zf:
            for name in zf.namelist():
                if name == "manifest.json":
                    continue
                # Defensive: prevent zip-slip (../../etc)
                norm = os.path.normpath(name)
                if norm.startswith("..") or os.path.isabs(norm):
                    return jsonify({"error": f"Refusing to extract suspicious path: {name}"}), 400
                zf.extract(name, target)

        # Switch DMS to use this folder
        cfg = load_config()
        cfg["storage_path"] = str(target)
        save_config(cfg)

        # Inventory what landed
        inventory = {
            "imported_to": str(target),
            "source_manifest": manifest,
            "files_in_root": sorted(p.name for p in target.iterdir()),
        }
        docs_dir = target / "docs"
        if docs_dir.exists():
            inventory["doc_count"] = sum(1 for _ in docs_dir.iterdir() if _.is_file())

        return jsonify({"ok": True, **inventory})
    finally:
        try:
            os.unlink(tmp_zip.name)
        except OSError:
            pass


@app.route("/api/project/new", methods=["POST"])
def new_project():
    """Create a fresh empty project at the given path and switch to it."""
    data = request.get_json(force=True) or {}
    target_path = (data.get("target_path") or "").strip()
    if not target_path:
        return jsonify({"error": "target_path is required"}), 400

    target = Path(target_path).expanduser().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            return jsonify({
                "error": (
                    f"Folder '{target}' is not empty. "
                    "New projects must start in an empty (or non-existent) folder."
                )
            }), 400
        (target / "docs").mkdir(parents=True, exist_ok=True)
        # Write a truly fresh empty project. Do NOT use tree=None here:
        # the frontend treats tree=None as first-run demo mode and seeds the
        # sample pump assembly, which makes a new project look like old data.
        fresh_tree = {
            "id": "NODE-" + secrets.token_hex(3).upper(),
            "name": "New Project",
            "description": "",
            "children": [],
            "documents": [],
        }
        (target / "index.json").write_text(json.dumps({
            "tree": fresh_tree,
            "docIndex": [],
            "keyParameters": [],
        }, indent=2))
    except OSError as e:
        return jsonify({"error": f"Cannot create folder: {e}"}), 400

    cfg = load_config()
    cfg["storage_path"] = str(target)
    save_config(cfg)
    return jsonify({"ok": True, "storage_path": str(target)})


# ---- Hierarchy import -----------------------------------------------------
@app.route("/api/hierarchy/preview", methods=["POST"])
def hierarchy_preview():
    """
    Validate a hierarchy definition file and return a preview of what nodes
    would be created — without touching the filesystem.

    Request JSON:  { "text": "<file contents>" }

    Response (valid):
      { "ok": true, "root": "BOP001", "count": 6,
        "order": [{"id": "BOP001", "parent": null, "depth": 0}, ...] }

    Response (invalid):
      { "ok": false, "errors": ["Line 4: ...", ...] }
    """
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")

    nodes, errors, root = parse_hierarchy_file(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    order = build_tree_order(nodes, root)

    depth_map = {root: 0}
    for nid, pid in order:
        if pid is not None:
            depth_map[nid] = depth_map.get(pid, 0) + 1

    preview = [
        {"id": nid, "parent": pid, "depth": depth_map.get(nid, 0)}
        for nid, pid in order
    ]
    return jsonify({"ok": True, "root": root, "count": len(order), "order": preview})


@app.route("/api/hierarchy/create", methods=["POST"])
def hierarchy_create():
    """
    Validate a hierarchy definition file and build the corresponding node tree
    in the DMS index.json.  Each node in the hierarchy becomes a DMS tree node.
    Existing nodes whose names match are left untouched (idempotent).

    Request JSON:  { "text": "<file contents>" }

    Response (success):
      { "ok": true, "root": "BOP001", "created": [...], "skipped": [...] }

    Response (validation / storage error):
      { "ok": false, "errors": [...] }
    """
    if not get_storage_root():
        return jsonify({"error": "Storage path not configured"}), 503

    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")

    nodes, errors, root = parse_hierarchy_file(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    order = build_tree_order(nodes, root)

    # Read the existing index so we can merge the new hierarchy into it
    idx = read_index()

    # Build a name->node map of existing top-level nodes to avoid duplicates.
    # We re-use existing node IDs when the name matches exactly.
    def _find_child_by_name(parent_node, name):
        for c in (parent_node.get("children") or []):
            if c.get("name") == name:
                return c
        return None

    # node_id_map: hierarchy NodeID -> DMS node dict
    node_id_map = {}
    created = []
    skipped = []

    existing_tree = idx.get("tree")

    for hier_id, parent_hier_id in order:
        # Display name is the last path component so "A/B/C" shows as "C".
        # Bare names like "BOP001" are unchanged (backward compatible).
        display_name = hier_id.rsplit("/", 1)[-1]

        if parent_hier_id is None:
            # Root node
            if existing_tree and existing_tree.get("name") == display_name:
                # Root already exists with this name — reuse it
                node_id_map[hier_id] = existing_tree
                skipped.append(hier_id)
            else:
                # Create a new root (replaces the current tree root)
                new_node = {
                    "id": "NODE-" + secrets.token_hex(3).upper(),
                    "name": display_name,
                    "description": "",
                    "children": [],
                    "documents": [],
                }
                node_id_map[hier_id] = new_node
                # We'll attach the old tree as a child if it exists, or just replace
                if existing_tree:
                    # Preserve old tree as a child so no data is lost
                    new_node["children"].append(existing_tree)
                idx["tree"] = new_node
                created.append(hier_id)
        else:
            parent_node = node_id_map.get(parent_hier_id)
            if parent_node is None:
                return jsonify({
                    "ok": False,
                    "errors": [f"Internal error: parent '{parent_hier_id}' not found in map."]
                })

            existing_child = _find_child_by_name(parent_node, display_name)
            if existing_child:
                node_id_map[hier_id] = existing_child
                skipped.append(hier_id)
            else:
                new_node = {
                    "id": "NODE-" + secrets.token_hex(3).upper(),
                    "name": display_name,
                    "description": "",
                    "children": [],
                    "documents": [],
                }
                parent_node.setdefault("children", []).append(new_node)
                node_id_map[hier_id] = new_node
                created.append(hier_id)

    write_index(idx)

    return jsonify({
        "ok": True,
        "root": root,
        "created": created,
        "skipped": skipped,
    })


# ---- ZIP bulk-import ------------------------------------------------------
@app.route("/api/docs/import-zip", methods=["POST"])
def import_zip_docs():
    """
    Accept a ZIP of documents and distribute each file to the tree node whose
    name matches the prefix before '#' in the filename.

    e.g.  "SA-100#Drawing-001.pdf"  → node named "SA-100"

    Multipart form field:  file = <zip binary>

    Response (success):
      {
        "ok": true,
        "allocated": [{"filename": "...", "node_name": "...", "doc_id": "..."}, ...],
        "unmatched": [{"filename": "...", "reason": "..."}, ...]
      }
    """
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503
    if "file" not in request.files:
        return jsonify({"error": "Missing 'file' part"}), 400

    f = request.files["file"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        f.save(tmp.name)
        tmp.close()

        try:
            with zipfile.ZipFile(tmp.name, "r") as _zf:
                pass
        except zipfile.BadZipFile:
            return jsonify({"error": "Not a valid ZIP file"}), 400

        idx = read_index()

        # Build lookup maps by walking the full tree.
        # name_map: {bare_name: [node, ...]} — all nodes with that name
        # path_map: {"Parent/Child": node}   — full path from root (root name excluded)
        name_map: dict = {}
        path_map: dict = {}

        def _walk(node, ancestors):
            if not node:
                return
            name = node.get("name", "")
            if name:
                name_map.setdefault(name, []).append(node)
                # Build path excluding root (ancestors[0] is root name, omitted for brevity)
                path_parts = ancestors[1:] + [name] if ancestors else [name]
                path_map["/".join(path_parts)] = node
                # Also store full path including root
                path_map["/".join(ancestors + [name])] = node
            for child in (node.get("children") or []):
                _walk(child, ancestors + ([name] if name else []))

        _walk(idx.get("tree"), [])

        doc_index: list = idx.get("docIndex", [])
        allocated = []
        unmatched = []

        def _zip_open(path, enc):
            """Open a ZipFile trying enc first, falling back to default."""
            try:
                return zipfile.ZipFile(path, "r", metadata_encoding=enc)
            except TypeError:
                # Python < 3.11 doesn't support metadata_encoding
                return zipfile.ZipFile(path, "r")

        # macOS Finder ZIPs store filenames as raw UTF-8 without setting the
        # UTF-8 flag (bit 11), so Python misreads them as CP437.
        # metadata_encoding="utf-8" (Python 3.11+) fixes this transparently.
        # We try UTF-8 first (macOS / modern tools); fall back to GBK for
        # ZIPs created by older Windows tools with Chinese locale.
        def _best_encoding(path):
            for enc in ("utf-8", "gbk"):
                try:
                    with _zip_open(path, enc) as zf:
                        for info in zf.infolist():
                            info.filename.encode("utf-8")  # will raise if garbled
                    return enc
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
            return "utf-8"  # last resort

        chosen_enc = _best_encoding(tmp.name)

        with _zip_open(tmp.name, chosen_enc) as zf:
            for entry in zf.infolist():
                if entry.is_dir():
                    continue

                filename = Path(entry.filename).name
                # Skip macOS metadata files injected by Finder
                if not filename or filename.startswith("._") or "__MACOSX" in entry.filename:
                    continue

                hash_pos = filename.find("#")
                if hash_pos <= 0:
                    unmatched.append({"filename": filename, "reason": "No '#' separator in filename"})
                    continue

                folder_name = filename[:hash_pos].strip()
                if "/" in folder_name:
                    node = path_map.get(folder_name)
                    if not node:
                        unmatched.append({"filename": filename, "reason": f"No folder at path '{folder_name}'"})
                        continue
                else:
                    matches = name_map.get(folder_name, [])
                    if not matches:
                        unmatched.append({"filename": filename, "reason": f"No folder named '{folder_name}'"})
                        continue
                    node = matches[0]

                # Generate a unique doc ID matching the client-side format
                doc_id = f"DOC-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"

                data = zf.read(entry.filename)
                ext = Path(filename).suffix.lstrip(".").lower() or "bin"
                safe_name = _safe_filename_part(Path(filename).name)
                if not Path(safe_name).suffix:
                    safe_name = f"{safe_name}.{ext}"

                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

                _photo_exts = {"jpg","jpeg","png","heic","heif","tif","tiff","webp","gif","bmp"}
                _is_photo = ext in _photo_exts or mime.startswith("image/")
                location = None
                if _is_photo:
                    lat, lon = _read_photo_gps(data)
                    if lat is not None:
                        location = _reverse_geocode(lat, lon)
                out_dir = _get_node_docs_dir(node["id"], idx.get("tree")) or docs_dir
                target_node = node

                out_path = out_dir / f"{doc_id}__{safe_name}"
                out_path.write_bytes(data)

                target_node.setdefault("documents", []).append({"id": doc_id})

                doc_index.append({
                    "id": doc_id,
                    "name": filename,
                    "mime": mime,
                    "size": len(data),
                    "uploadedAt": datetime.now().isoformat() + "Z",
                    "originalNodeId": target_node["id"],
                    "metadata": {"Location": {"design": "", "actual": location}} if location else {},
                    "sn": folder_name,
                })

                allocated.append({
                    "filename": filename,
                    "node_name": folder_name,
                    "doc_id": doc_id,
                })

        idx["docIndex"] = doc_index
        write_index(idx)

        return jsonify({"ok": True, "allocated": allocated, "unmatched": unmatched})

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---- Quit -----------------------------------------------------------------
@app.route("/api/quit", methods=["POST"])
def quit_app():
    """Shut down the DMS server and exit the process."""
    import threading
    threading.Thread(target=lambda: __import__("os")._exit(0), daemon=True).start()
    return jsonify({"ok": True})


# ---- Diagnostics ----------------------------------------------------------
@app.route("/api/diag", methods=["GET"])
def diag():
    """Return diagnostic info about the server and storage folder."""
    cfg = load_config()
    root = get_storage_root()
    out = {
        "config_path": str(CONFIG_PATH),
        "config": cfg,
        "script_dir": str(SCRIPT_DIR),
        "storage_root": str(root) if root else None,
        "storage_root_exists": bool(root and root.exists()),
        "files_in_root": [],
        "files_in_docs": [],
    }
    if root and root.exists():
        try:
            out["files_in_root"] = sorted(p.name for p in root.iterdir())
        except OSError:
            pass
        docs = root / "docs"
        if docs.exists():
            try:
                out["files_in_docs"] = sorted(p.name for p in docs.iterdir())
            except OSError:
                pass
    return jsonify(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def open_browser(port: int):
    webbrowser.open(f"http://localhost:{port}/")


def main():
    parser = argparse.ArgumentParser(description="DMS Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1, localhost-only)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    cfg = load_config()
    print()
    print("=" * 60)
    print("  Document Management System — local server")
    print("=" * 60)
    print(f"  Server:        http://{args.host}:{args.port}/")
    print(f"  Config file:   {CONFIG_PATH}")
    if cfg.get("storage_path"):
        print(f"  Storage path:  {cfg['storage_path']}")
    else:
        print(f"  Storage path:  (not configured — set it in the web UI)")
    print()
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)
    print()

    if not args.no_browser:
        Timer(1.0, open_browser, args=[args.port]).start()

    # Migrate any existing flat docs into per-node subdirectories.
    try:
        _migrate_flat_docs()
    except Exception as e:
        print(f"[DMS] Migration warning: {e}")

    # Use the built-in dev server. Fine for local single-user use.
    # For a real deployment, run via gunicorn or similar.
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

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
import base64
import hashlib
import io
import json
import mimetypes
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Timer, Lock as _Lock

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
    # SMTP email settings (stored in plaintext – local machine only)
    "email_smtp_host": "",
    "email_smtp_port": 587,
    "email_smtp_user": "",
    "email_smtp_pass": "",
    "email_from": "",
    "email_ssl": False,   # True → SMTP_SSL (port 465); False → STARTTLS (port 587)
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
_tunnel_base_url: str = ""   # set by launcher after cloudflared starts
_tunnel_start_fn = None      # injected by launcher: callable() -> public_url str
_tunnel_start_lock = _Lock() # prevents two requests from starting the tunnel simultaneously


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


def _get_lan_ip() -> str:
    """Return the best LAN IP for phone access.

    On macOS, prefers physical Wi-Fi/Ethernet interfaces (en0, en1, en2 …)
    over VPN tunnels (utun*, ppp*, tun*).  A VPN IP is unreachable from a
    phone on the same Wi-Fi, so the simple UDP-trick approach is unreliable
    when a VPN is active.
    """
    import subprocess as _sp
    if sys.platform == "darwin":
        try:
            out = _sp.check_output(["ifconfig", "-a"], text=True, stderr=_sp.DEVNULL)
            physical, all_ips = [], []
            iface = ""
            for line in out.splitlines():
                if line and line[0] not in (" ", "\t"):
                    iface = line.split(":")[0]
                elif "inet " in line:
                    parts = line.split()
                    try:
                        ip = parts[parts.index("inet") + 1]
                    except (ValueError, IndexError):
                        continue
                    # Filter by the IP itself, not the whole line (broadcast can contain "127.")
                    if ip and not ip.startswith("127."):
                        all_ips.append(ip)
                        if iface.startswith("en"):   # en0/en1 = Wi-Fi or Ethernet
                            physical.append(ip)
            candidates = physical or all_ips
            if candidates:
                return candidates[0]
        except Exception:
            pass
    elif sys.platform.startswith("win"):
        try:
            out = _sp.check_output(
                ["powershell", "-Command",
                 "(Get-NetIPAddress -AddressFamily IPv4 | "
                 "Where-Object { $_.IPAddress -notmatch '^127\\.' -and "
                 "$_.PrefixOrigin -ne 'WellKnown' } | "
                 "Sort-Object InterfaceMetric | "
                 "Select-Object -First 1).IPAddress"],
                text=True, stderr=_sp.DEVNULL,
            ).strip()
            if out:
                return out
        except Exception:
            pass
    # Fallback: route-based detection (may return VPN IP)
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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

    # Build a single file map (one rglob) so we don't rglob once per document.
    file_map: dict[str, Path] = {}
    for f in docs_dir.rglob("*"):
        if not f.is_file():
            continue
        name = f.name
        sep = name.find("__")
        key = name[:sep] if sep != -1 else f.stem
        if key not in file_map:
            file_map[key] = f

    for entry in (idx.get("docIndex") or []):
        doc_id = entry.get("id") or ""
        node_id = entry.get("originalNodeId") or entry.get("nodeId") or ""
        if not doc_id or not node_id:
            continue
        target_dir = _get_node_docs_dir(node_id, tree) or docs_dir
        try:
            current = file_map.get(doc_id)
            if not current or not current.exists():
                continue
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
            file_map[doc_id] = destination
        except OSError as e:
            print(f"[DMS] Warning: could not move {doc_id} into current folder path: {e}")


def _collect_node_ids(tree: dict | None) -> set:
    """Return the set of all node IDs present in *tree*."""
    if not tree:
        return set()
    ids = {tree["id"]}
    for child in (tree.get("children") or []):
        ids |= _collect_node_ids(child)
    return ids


def _try_rmdir_empty(path: Path) -> None:
    """Remove *path* and any empty subdirectories beneath it.
    If any files remain after trying to clear children, the folder is kept.
    """
    if not path.is_dir():
        return
    for child in list(path.iterdir()):
        if child.is_dir():
            _try_rmdir_empty(child)
    try:
        remaining = list(path.iterdir())
        if not remaining:
            path.rmdir()
            print(f"[DMS] Removed empty folder: {path}")
    except OSError as e:
        print(f"[DMS] Could not remove folder {path}: {e}")


def _sync_folder_moves(old_tree: dict | None, new_tree: dict | None) -> None:
    """Move node folders on disk when a node is reparented in the tree.

    When the user drags a folder to a new parent in the UI, this moves the
    entire on-disk folder (and all its contents) from the old path to the new
    path, keeping the physical directory structure in sync with the tree.

    Nodes are processed shallowest-first so that if a parent moves and carries
    its children along, the children's old paths won't exist anymore and will be
    skipped rather than double-moved.
    """
    docs_dir = get_docs_dir()
    if not docs_dir or not old_tree or not new_tree:
        return

    def _index_paths(node: dict, tree: dict, out: dict) -> None:
        nid = node.get("id")
        if nid:
            parts = _get_node_path_parts(tree, nid)
            if parts:
                out[nid] = docs_dir.joinpath(*parts)
        for child in (node.get("children") or []):
            _index_paths(child, tree, out)

    old_paths: dict[str, Path] = {}
    new_paths: dict[str, Path] = {}
    _index_paths(old_tree, old_tree, old_paths)
    _index_paths(new_tree, new_tree, new_paths)

    moved = [
        (old_paths[nid], new_paths[nid])
        for nid in old_paths
        if nid in new_paths and old_paths[nid].resolve() != new_paths[nid].resolve()
    ]
    # Shallowest first: moving a parent carries its children automatically.
    moved.sort(key=lambda x: len(x[0].parts))

    for old_folder, new_folder in moved:
        if not old_folder.exists():
            continue  # already carried by a parent move

        if new_folder.exists():
            # Strip macOS / Windows metadata-only files so they don't
            # block an otherwise-empty destination.
            for meta in new_folder.rglob("*"):
                if meta.is_file() and meta.name in (".DS_Store", "desktop.ini", "Thumbs.db"):
                    try:
                        meta.unlink()
                    except OSError:
                        pass

            real_items = [p for p in new_folder.iterdir()
                          if p.name not in (".DS_Store", "desktop.ini", "Thumbs.db")]

            if not real_items:
                # Empty placeholder — remove it so shutil.move can rename cleanly.
                try:
                    _try_rmdir_empty(new_folder)
                except OSError:
                    pass

            if new_folder.exists():
                # Destination still has real content — merge item by item.
                for item in list(old_folder.iterdir()):
                    dest = new_folder / item.name
                    if dest.exists():
                        continue  # leave existing content untouched
                    try:
                        shutil.move(str(item), str(dest))
                    except OSError as e:
                        print(f"[DMS] Warning: could not merge {item.name}: {e}")
                _try_rmdir_empty(old_folder)
                print(f"[DMS] Merged folder contents: {old_folder.name}  →  {new_folder}")
                continue

        try:
            new_folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_folder), str(new_folder))
            print(f"[DMS] Moved folder: {old_folder.name}  →  {new_folder}")
        except OSError as e:
            print(f"[DMS] Warning: could not move folder {old_folder}: {e}")


def _cleanup_stale_node_folders(old_tree: dict | None, new_tree: dict | None) -> None:
    """Delete on-disk folders whose nodes were removed or relocated in new_tree.

    Called after _sync_doc_files_to_tree_paths so the files have already been
    moved; this step just removes any directories that are now empty.

    Two cases are handled:
    • Node removed from tree entirely (merge / delete) → delete its old folder.
    • Node still in tree but at a different path (drag to new parent) → delete
      the old (now-empty) folder at the previous location.
    """
    if not old_tree:
        return
    docs_dir = get_docs_dir()
    if not docs_dir:
        return

    new_ids = _collect_node_ids(new_tree)

    # Pre-compute on-disk paths for every node in the new tree so we can
    # detect path changes for nodes that were moved (not removed).
    new_paths: dict[str, Path] = {}
    if new_tree:
        def _index_new_paths(node: dict) -> None:
            nid = node.get("id", "")
            if nid:
                parts = _get_node_path_parts(new_tree, nid)
                if parts:
                    new_paths[nid] = docs_dir.joinpath(*parts)
            for child in (node.get("children") or []):
                _index_new_paths(child)
        _index_new_paths(new_tree)

    def _walk_old(node: dict) -> None:
        node_id = node.get("id", "")
        if node_id:
            old_parts = _get_node_path_parts(old_tree, node_id)
            if old_parts:
                old_folder = docs_dir.joinpath(*old_parts)
                if node_id not in new_ids:
                    # Node removed — clean up its old folder
                    _try_rmdir_empty(old_folder)
                else:
                    new_folder = new_paths.get(node_id)
                    if new_folder and old_folder.resolve() != new_folder.resolve():
                        # Node moved to a new path — clean up old location
                        _try_rmdir_empty(old_folder)
        for child in (node.get("children") or []):
            _walk_old(child)

    _walk_old(old_tree)


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
    """Parse a hierarchy definition file using the CSV format."""
    details, errors, root = parse_hierarchy_file_with_details(text)
    return {
        node_id: details[node_id]["parent_id"]
        for node_id in details
    }, errors, root


def parse_hierarchy_file_with_details(text: str):
    """
    Parse a hierarchy definition file as CSV.

    Expected columns:
        1. folder name
        2. parent folder name  (omit or leave blank for root)
        3. description         (optional)

    Blank lines and lines matching /^level\\s*\\d+$/i are ignored.
    The first non-empty row is treated as the root folder and is assigned no parent.
    Subsequent rows with an empty parent column default to the root folder.

    Returns
    -------
    nodes  : dict  { node_id: {"parent_id": parent_id_or_None,
                               "description": description} }
    errors : list of human-readable error strings
    root   : str or None  (None when validation fails)
    """
    import csv as _csv
    errors = []
    nodes = {}        # node_id -> {parent_id, description}
    seen = {}         # node_id -> line number  (duplicate detection)

    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _LEVEL_LABEL_RE.match(line):
            continue
        rows.append(line)

    if not rows:
        return nodes, errors, None

    root_node_id = None
    root_description = ""
    for lineno, raw in enumerate(rows, 1):
        try:
            row = next(_csv.reader([raw]))
        except Exception:
            row = [raw]

        if not row:
            continue

        values = [c.strip() for c in row]
        if len(values) == 1:
            node_id = values[0]
            parent_id = None
            description = ""
        elif len(values) >= 2:
            node_id = values[0]
            parent_id = values[1] if values[1] else None
            description = values[2] if len(values) > 2 else ""
        else:
            node_id = ""
            parent_id = None
            description = ""

        if not node_id:
            continue

        if root_node_id is None:
            root_node_id = node_id
            root_description = description
            nodes[node_id] = {"parent_id": None, "description": description}
            seen[node_id] = lineno
            continue

        if parent_id is None:
            parent_id = root_node_id

        if node_id in seen:
            errors.append(
                f"Line {lineno}: duplicate folder name '{node_id}' "
                f"(first seen on line {seen[node_id]})"
            )
            continue

        seen[node_id] = lineno
        nodes[node_id] = {"parent_id": parent_id, "description": description}

    # Structural validation
    roots = [nid for nid, info in nodes.items() if info["parent_id"] is None]
    if not roots:
        errors.append(
            "No root node found. "
            "A root node has no parent folder."
        )
    elif len(roots) > 1:
        if root_node_id is None:
            errors.append(
                f"Multiple root nodes found: {roots}. Exactly one root is required."
            )

    for nid, info in nodes.items():
        pid = info["parent_id"]
        if pid is not None and pid not in nodes:
            errors.append(
                f"Folder '{nid}' references unknown parent folder '{pid}'."
            )

    def _has_cycle(start):
        visited = set()
        cur = start
        while cur is not None:
            if cur in visited:
                return True
            visited.add(cur)
            cur = nodes.get(cur, {}).get("parent_id")
        return False

    for nid in nodes:
        if _has_cycle(nid):
            errors.append(
                f"Circular reference detected involving folder '{nid}'."
            )
            break

    root = root_node_id or (roots[0] if len(roots) == 1 else None)
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
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB per upload


# Endpoints that don't require authentication (login itself, plus auth status check)
_PUBLIC_ENDPOINTS = {
    "auth_status",
    "auth_login",
    "auth_logout",
    "mobile_upload_page",   # mobile page handles its own auth in JS
    "mobile_upload",        # allow upload without session on local network
    "remote_mobile_page",   # token-gated remote upload page — token is the auth
    "download_token",       # one-time download links sent to phone
}


@app.after_request
def add_ngrok_header(response):
    """Tell ngrok to skip its browser-warning interstitial for all responses.

    On free ngrok plans, the first browser request is intercepted by an
    ngrok interstitial page. Setting this header causes ngrok to deliver
    the actual response directly, which is necessary for the mobile upload
    page to render on a phone.
    """
    response.headers.setdefault("ngrok-skip-browser-warning", "1")
    return response


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


# ---- Vendor static files (bundled JS/CSS — served locally so app works offline) ----
@app.route("/vendor/<path:filename>")
def vendor_static(filename):
    vendor_dir = SCRIPT_DIR / "vendor"
    return send_from_directory(vendor_dir, filename)


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

  <div id="banner">__BANNER__</div>
  <p style="margin-top:16px;font-size:10px;color:#ccc;text-align:center">v8</p>
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
  // Scroll banner into view if present (iOS keeps scroll position after redirect)
  var b=document.getElementById('banner');
  if(b) b.scrollIntoView({behavior:'smooth',block:'center'});
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

        if is_photo and _valid_photo_year_month(photo_year, photo_month) and not node_id:
            # Auto-route to year/month only when no folder was explicitly selected.
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
        try:
            out_path.write_bytes(data)
        except Exception as _e:
            return _redirect_err(f"Could not save file: {_e}")

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

    try:
        write_index(idx)
    except Exception as _e:
        return _redirect_err(f"Save failed: {_e}")
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


@app.route("/api/settings/check", methods=["GET"])
def check_storage_path():
    """Check whether an arbitrary path has existing DMS data (without switching to it)."""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "path is required"}), 400
    p = Path(raw).expanduser().resolve()
    has_data = (p / "index.json").exists()
    doc_count = 0
    if has_data:
        try:
            docs = p / "docs"
            if docs.exists():
                doc_count = sum(1 for _ in docs.iterdir() if _.is_file())
        except OSError:
            pass
    return jsonify({"resolved_path": str(p), "exists": p.exists(), "has_data": has_data, "doc_count": doc_count})


@app.route("/api/settings", methods=["POST"])
def set_settings():
    """Update the storage path. Creates the folder if it doesn't exist.

    Optional body flag ``copy_current`` (bool): when True and the new path has
    no existing index.json, copy the current index.json there so the user's
    current data migrates with them.
    """
    import shutil as _shutil
    data = request.get_json(force=True) or {}
    new_path = (data.get("storage_path") or "").strip()
    copy_current = bool(data.get("copy_current", False))
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

    new_index = p / "index.json"
    had_existing_data = new_index.exists()

    # If asked to migrate and the new folder is empty, copy current index.json
    if copy_current and not had_existing_data:
        old_root = get_storage_root()
        if old_root:
            old_index = old_root / "index.json"
            if old_index.exists():
                try:
                    _shutil.copy2(str(old_index), str(new_index))
                except OSError:
                    pass  # non-fatal — user will see an empty tree instead

    cfg = load_config()
    cfg["storage_path"] = str(p)
    save_config(cfg)
    return jsonify({"ok": True, "resolved_path": str(p), "had_existing_data": had_existing_data})


# ---- Tree ------------------------------------------------------------------
@app.route("/api/index-mtime", methods=["GET"])
def get_index_mtime():
    """Return the modification timestamp of index.json for change detection."""
    p = get_index_path()
    if not p or not p.exists():
        return jsonify({"mtime": 0})
    return jsonify({"mtime": p.stat().st_mtime})


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
    old_tree = idx.get("tree")   # snapshot before overwrite
    idx["tree"] = data.get("tree")
    write_index(idx)
    new_tree = idx.get("tree")
    # Run expensive disk-sync in background so the HTTP response returns immediately.
    def _background_sync(old=old_tree, new=new_tree):
        _sync_folder_moves(old, new)
        _create_local_folder_structure(new)
        _sync_doc_files_to_tree_paths(new)
        _cleanup_stale_node_folders(old, new)
    import threading as _threading
    _threading.Thread(target=_background_sync, daemon=True).start()
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


_DOWNLOAD_TOKENS: dict = {}   # token -> {doc_id, expiry}

@app.route("/dl/<token>")
def download_token(token: str):
    """Public one-time-ish download endpoint for links sent to phone."""
    import time as _t
    info = _DOWNLOAD_TOKENS.get(token)
    if not info or _t.time() > info["expiry"]:
        return Response("<html><body style='font-family:sans-serif;padding:40px'>"
                        "<h2>Link expired</h2><p>Please request a new link.</p>"
                        "</body></html>", status=410, mimetype="text/html")
    doc_id = info["doc_id"]
    docs_dir = get_docs_dir()
    if not docs_dir:
        abort(503)
    matches = list(docs_dir.rglob(f"{doc_id}__*")) or list(docs_dir.rglob(f"{doc_id}*"))
    idx = read_index()
    meta = next((d for d in idx.get("docIndex", []) if d.get("id") == doc_id), None)
    if not matches:
        fname = meta["name"] if meta else doc_id
        return Response(
            f"<html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>文件不存在</h2>"
            f"<p>无法找到文件：<strong>{fname}</strong></p>"
            f"<p>该文件可能已从服务器磁盘中删除或移走。</p>"
            f"</body></html>",
            status=404, mimetype="text/html"
        )
    file_path = matches[0]
    download_name = meta["name"] if meta else file_path.name
    mime = (meta.get("mime") if meta else None) or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return send_file(file_path, mimetype=mime, download_name=download_name, as_attachment=False)


@app.route("/api/send-to-phone", methods=["POST"])
def send_to_phone():
    """Send a document download link to a phone via Messages (Mac) or Phone Link (Windows)."""
    import socket, subprocess
    data = request.get_json(force=True) or {}
    doc_id = (data.get("doc_id") or "").strip()
    phone  = (data.get("phone")  or "").strip()
    if not doc_id or not phone:
        return jsonify({"error": "doc_id and phone are required"}), 400

    idx  = read_index()
    meta = next((d for d in idx.get("docIndex", []) if d.get("id") == doc_id), None)
    if not meta:
        return jsonify({"error": "Document not found"}), 404

    import time as _t
    token = secrets.token_hex(16)
    _DOWNLOAD_TOKENS[token] = {"doc_id": doc_id, "expiry": _t.time() + 3600}  # 1-hour link

    # Prefer the cloudflared tunnel URL (works from anywhere).
    # Call _tunnel_start_fn every time — it returns instantly if the tunnel is
    # already alive, and restarts it if the process has crashed.  This prevents
    # sending stale (dead) tunnel URLs after cloudflared drops unexpectedly.
    global _tunnel_base_url
    if _tunnel_start_fn is not None:
        with _tunnel_start_lock:
            try:
                _tunnel_base_url = _tunnel_start_fn()
            except Exception as _e:
                print(f"[DMS] Tunnel check/start failed: {_e} — falling back to LAN IP")
                _tunnel_base_url = ""

    is_tunnel = bool(_tunnel_base_url)
    if is_tunnel:
        download_url = f"{_tunnel_base_url}/dl/{token}"
        local_ip = None
        port = None
    else:
        local_ip = _get_lan_ip()
        host = request.host
        port = host.split(":")[-1] if ":" in host else "8765"
        download_url = f"http://{local_ip}:{port}/dl/{token}"

    doc_name = meta.get("name", doc_id)
    message = f"文件：{doc_name}\n下载：{download_url}"
    normalised = phone if phone.startswith("+") else "+" + phone

    # ---- Mac: send via Messages.app (AppleScript) --------------------------
    if sys.platform == "darwin":
        digits_suffix = normalised.lstrip("+")
        safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f"""
tell application "Messages"
    set _msg to "{safe_msg}"
    set _digits to "{digits_suffix}"
    set _norm to "{normalised}"
    set _sent to false
    repeat with c in chats
        try
            repeat with p in (participants of c)
                set h to handle of p
                if h ends with _digits or h is _norm then
                    send _msg to c
                    set _sent to true
                    exit repeat
                end if
            end repeat
        end try
        if _sent then exit repeat
    end repeat
    if not _sent then
        repeat with svc in services
            set sType to service type of svc as string
            if sType is in {{"iMessage", "SMS", "RCS"}} then
                try
                    send _msg to buddy _norm of svc
                    set _sent to true
                    exit repeat
                end try
            end if
        end repeat
    end if
    if not _sent then
        error "No Messages conversation found for " & _norm
    end if
end tell
"""
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return jsonify({"ok": True, "url": download_url, "method": "messages",
                            "local_ip": local_ip, "port": port, "is_tunnel": is_tunnel})
        return jsonify({"error": result.stderr.strip() or
                        "Messages app could not send. Is your iPhone linked to this Mac via Messages?"}), 500

    # ---- Windows: open Phone Link via sms: URI -----------------------------
    elif sys.platform == "win32":
        import urllib.parse as _urlparse
        # Build sms: URI — Phone Link handles this and pre-fills the compose window.
        sms_uri = f"sms:{normalised}?body={_urlparse.quote(message)}"
        try:
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-Command",
                 f"Start-Process '{sms_uri}'"],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return jsonify({"ok": True, "url": download_url, "method": "phone_link",
                            "local_ip": local_ip, "port": port, "is_tunnel": is_tunnel})
        except Exception as e:
            return jsonify({"ok": True, "url": download_url, "method": "url_only",
                            "local_ip": local_ip, "port": port, "is_tunnel": is_tunnel})

    # ---- Other (Linux etc.): return URL only --------------------------------
    else:
        return jsonify({"ok": True, "url": download_url, "method": "url_only",
                        "local_ip": local_ip, "port": port, "is_tunnel": is_tunnel})


def _email_esc(s: str) -> str:
    """Escape a string for embedding inside AppleScript double-quoted literals."""
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\r", "")
             .replace("\n", "\\n"))


def _send_via_applescript(to_email, subject, body, file_path):
    """Open Mac Mail.app with a pre-filled compose window (Mac-only fallback)."""
    import subprocess
    posix = _email_esc(str(file_path))
    lines = [
        'tell application "Mail"',
        f'    set newMsg to make new outgoing message with properties {{subject:"{_email_esc(subject)}", content:"{_email_esc(body)}", visible:true}}',
        '    tell newMsg',
        f'        make new to recipient at end of to recipients with properties {{address:"{_email_esc(to_email)}"}}',
        f'        make new attachment with properties {{file name:POSIX file "{posix}"}} at after the last paragraph',
        '    end tell',
        '    activate',
        'end tell',
    ]
    result = subprocess.run(
        ["osascript", "-e", "\n".join(lines)],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode == 0:
        return jsonify({"ok": True, "method": "applescript"})
    err = result.stderr.strip() or "无法打开 Mail 应用。请确保 Mac Mail 已安装并配置了邮件账户。"
    return jsonify({"error": err}), 500


def _send_via_smtp(to_email, subject, body, file_path, doc_name, cfg):
    """Send email with attachment via SMTP (cross-platform)."""
    import smtplib
    import mimetypes as _mimetypes
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders as _enc

    smtp_host = (cfg.get("email_smtp_host") or "").strip()
    smtp_port = int(cfg.get("email_smtp_port") or 587)
    smtp_user = (cfg.get("email_smtp_user") or "").strip()
    smtp_pass = (cfg.get("email_smtp_pass") or "").strip()
    from_addr = (cfg.get("email_from") or "").strip() or smtp_user
    use_ssl   = bool(cfg.get("email_ssl", False))

    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    mime_type = _mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    main_type, sub_type = mime_type.split("/", 1)
    part = MIMEBase(main_type, sub_type)
    with open(file_path, "rb") as fh:
        part.set_payload(fh.read())
    _enc.encode_base64(part)
    # RFC 2231 filename encoding handles non-ASCII (Chinese) characters correctly
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", doc_name))
    msg.attach(part)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as srv:
                if smtp_user and smtp_pass:
                    srv.login(smtp_user, smtp_pass)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                if smtp_user and smtp_pass:
                    srv.login(smtp_user, smtp_pass)
                srv.send_message(msg)
        return jsonify({"ok": True, "method": "smtp"})
    except Exception as exc:
        return jsonify({"error": f"SMTP 错误：{exc}"}), 500


@app.route("/api/email-settings", methods=["GET"])
def get_email_settings():
    cfg = load_config()
    return jsonify({
        "smtp_host": cfg.get("email_smtp_host", ""),
        "smtp_port": cfg.get("email_smtp_port", 587),
        "smtp_user": cfg.get("email_smtp_user", ""),
        "email_from": cfg.get("email_from", ""),
        "email_ssl":  cfg.get("email_ssl", False),
        "configured": bool(
            (cfg.get("email_smtp_host") or "").strip() and
            (cfg.get("email_smtp_user") or "").strip()
        ),
    })


@app.route("/api/email-settings", methods=["POST"])
def set_email_settings():
    data = request.get_json(force=True) or {}
    cfg = load_config()
    cfg["email_smtp_host"] = (data.get("smtp_host") or "").strip()
    cfg["email_smtp_port"] = int(data.get("smtp_port") or 587)
    cfg["email_smtp_user"] = (data.get("smtp_user") or "").strip()
    cfg["email_from"]      = (data.get("email_from") or "").strip()
    cfg["email_ssl"]       = bool(data.get("email_ssl", False))
    new_pass = (data.get("smtp_pass") or "").strip()
    if new_pass:
        cfg["email_smtp_pass"] = new_pass
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/email-settings/test", methods=["POST"])
def test_email_settings():
    """Send a test email using the provided settings (does not save them first)."""
    data = request.get_json(force=True) or {}
    # Use provided settings; fall back to saved config for password if blank
    cfg = load_config()
    test_cfg = {
        "email_smtp_host": (data.get("smtp_host") or "").strip(),
        "email_smtp_port": int(data.get("smtp_port") or 587),
        "email_smtp_user": (data.get("smtp_user") or "").strip(),
        "email_smtp_pass": (data.get("smtp_pass") or "").strip() or cfg.get("email_smtp_pass", ""),
        "email_from":      (data.get("email_from") or "").strip(),
        "email_ssl":       bool(data.get("email_ssl", False)),
    }
    to_addr = test_cfg["email_smtp_user"]
    if not test_cfg["email_smtp_host"] or not to_addr:
        return jsonify({"error": "请先填写 SMTP 服务器和用户名。"}), 400

    import smtplib
    from email.mime.text import MIMEText
    from_addr = test_cfg["email_from"] or to_addr
    msg = MIMEText("这是来自 DMS 的测试邮件，表明 SMTP 设置配置正确。", "plain", "utf-8")
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = "DMS 邮件设置测试"

    try:
        if test_cfg["email_ssl"]:
            with smtplib.SMTP_SSL(test_cfg["email_smtp_host"], test_cfg["email_smtp_port"], timeout=30) as srv:
                if test_cfg["email_smtp_user"] and test_cfg["email_smtp_pass"]:
                    srv.login(test_cfg["email_smtp_user"], test_cfg["email_smtp_pass"])
                srv.send_message(msg)
        else:
            with smtplib.SMTP(test_cfg["email_smtp_host"], test_cfg["email_smtp_port"], timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                if test_cfg["email_smtp_user"] and test_cfg["email_smtp_pass"]:
                    srv.login(test_cfg["email_smtp_user"], test_cfg["email_smtp_pass"])
                srv.send_message(msg)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": f"SMTP 错误：{exc}"}), 500


@app.route("/api/send-email", methods=["POST"])
def send_email():
    """Send a document as an email attachment.

    Uses SMTP when configured; falls back to Mac Mail.app on macOS.
    Body JSON: { "doc_id": "DOC-...", "to": "user@example.com",
                 "subject": "optional", "body": "optional" }
    """
    data = request.get_json(force=True) or {}
    doc_id   = (data.get("doc_id")  or "").strip()
    to_email = (data.get("to")      or "").strip()
    subject  = (data.get("subject") or "").strip()
    body     = (data.get("body")    or "").strip()

    if not doc_id or not to_email:
        return jsonify({"error": "doc_id and to are required"}), 400

    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    idx  = read_index()
    meta = next((d for d in idx.get("docIndex", []) if d.get("id") == doc_id), None)
    if not meta:
        return jsonify({"error": "Document not found"}), 404

    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    if not matches:
        fname = meta.get("name", doc_id) if meta else doc_id
        return jsonify({"error": f"文件不存在：{fname}（可能已从磁盘中删除或移走）"}), 404

    file_path = matches[0]
    doc_name  = meta.get("name", file_path.name)

    if not subject:
        subject = doc_name
    if not body:
        body = f"请见附件：{doc_name}"

    cfg = load_config()
    smtp_configured = (
        (cfg.get("email_smtp_host") or "").strip() and
        (cfg.get("email_smtp_user") or "").strip()
    )

    if smtp_configured:
        return _send_via_smtp(to_email, subject, body, file_path, doc_name, cfg)
    elif sys.platform == "darwin":
        return _send_via_applescript(to_email, subject, body, file_path)
    else:
        return jsonify({
            "error": "邮件未配置。请在项目菜单的邮件设置中配置 SMTP 服务器。"
        }), 400


@app.route("/api/download-smtp-guide", methods=["GET"])
def download_smtp_guide():
    """Serve the pre-generated SMTP setup guide (Word .docx, embedded as base64)."""
    import base64
    _GUIDE_B64 = (
        "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"
        "hcu/jv/4f1BLAwQUAAAACADdnNRcYHmC0zk1AABzrwYAGgAAAHdvcmQvc3R5bGVzV2l0aEVmZmVj"
        "dHMueG1s7X1dl6NGsu37+RW16sVPnpYAIcnLfc4SAsZey+Pxmfb4Pqur1F2arpLqSiq37V9/QJ+A"
        "EsiPSMiE7X6YKUAZkLkzc8cOiPj+f/54eb77fbndrTbr998M/zb45m65ftg8rtaf33/z71/jbyff"
        "3O32i/Xj4nmzXr7/5s/l7pv/+e//+v7rd7v9n8/L3V3y+/Xuu6+vD+/vn/b71+/evds9PC1fFru/"
        "vawetpvd5tP+bw+bl3ebT59WD8t3Xzfbx3fOYDg4/L/X7eZhudslxuaL9e+L3f2puZcNX2svi4fz"
        "/3UGg0ny92p9aeP2jjavy3Vy8tNm+7LYJ39uPye/2H55e/02afN1sV99XD2v9n+mbfmXZn5/f/+2"
        "XX93auPby32kv/kuuYHvfn95Pl+8qbr2eKOn/zn/Ystzk8efhJuHt5flen+4vXfb5XNyw5v17mn1"
        "eu032daSk0/nRiofOPOwX1+Hntqgh9vF1+R/rg3y3P7j8Ucvz8c7r25xOOAYkbSJyy94biFv83wn"
        "WfB9leuabOd+Vuvbv283b6/X1lZqrf24/nJpK1kGRNo6jVH20XZqN/PhafGaTKCXh+9+/LzebBcf"
        "n5M7Snr8LkXk/X//191dsjw9bh7C5afF2/N+lx45HNv+sj0dOx46Hzz/dfw73qz3u7uv3y12D6vV"
        "r8n9Ja2/rBJDP8zWu9V9cma52O1nu9UiezI6HUvPP6UXMn/5sNtnDgerx9X9u5z13V/JVb8vnt/f"
        "O87Nqfmu9OTzYv35fHK5/vbfH7L3mTn0MTH5/n6x/fbD7NrC9+8y3XD6I9dRiYFXVt+9Fvpu97p4"
        "WB1uZPFpv0zWtmT4U6vPqxQ0ztg///Gvt3TMFm/7Tf4uXrN3kTeZHikM6uG598ki9uG4FyUXLD/9"
        "tHn4snz8sE9OvL8/WE8O/vvHX7arzTZZ3N/fT6engx+WL6sfVo+Py/X7++H5wvXT6nH5/56W63/v"
        "lo/X4/8bH+b/qcWHzdt6f3ygSwc97x6jPx6Wr+minFyyXqTD/HP6q+f0J7uMsUMbb6vrLR0PFEwf"
        "Dv7/s93huaPKTD0tF+mufTestTYltOYwGxdvxyVqxyNqZ0TUjk/UzpionQlRO1PFdvabhyNSs224"
        "U56f3UCO72c3COP72Q2g+H52gx++n93Ahe9nN+jg+9kNGPh+djP29T97WBz+vvnhSAw1v672z8va"
        "9W1IsZye9pm7Xxbbxeft4vXpLuUFN6bqmvnw9nHPd9NDgpv+sN9uUvZbY8txCGxFL69Pi91qV2+N"
        "Yjh+TVne3d+3q8dae6OS/a3Gwi/Pi4fl0+b5cbm9+3X5x16qkZ83dx+OHKh+wAl65afV56f9XcKH"
        "H3ks+iUDwWXkp9VuX2+h5KG4LHANrl8C3RoL/1g+rt5ezj3FwZF8l8KOU2/HU7GTDgrPw4yUjXA8"
        "ia9iJB18nicZKxvheJKJshG33ojcKhUutl/45uJYbrbPN8+b7ae3Z+5VZSw35y92+B5GbtpfjHCt"
        "LWO5OZ9bhO9mDw+JQ8oDZdXVWMCU6rIsYIpmfRYwSLNQCxgkWLEFrMkt3f9a/r7anQm3+LjvMry3"
        "9hbdkg4RYjL/+7bZ15Nkh0K6+HG9X653yzs+ky4Fe83tpAKDT7ClClgj2FsFrBFssgLWFHdbfktE"
        "266AQYL9V8AawUYsYI1wR+bgfVQ7Mocpqh2ZwxTtjsxhkHZHbsaHErBG4EwJWCPcAjisEW4BzfhZ"
        "AtaItoB6S8RbAIdBwi2AwxrhFsBhjXAL4PDKqbYADlNUWwCHKdotgMMg7RbAYZBwC+CwRrgFcFgj"
        "3AI4rBFuARzWCLcA/ZobvyXiLYDDIOEWwGGNcAvgsEa4BXjNbQEcpqi2AA5TtFsAh0HaLYDDIOEW"
        "wGGNcAvgsEa4BXBYI9wCOKwRbgEc1oi2gHpLxFsAh0HCLYDDGuEWwGGNcAsYNbcFcJii2gI4TNFu"
        "ARwGabcADoOEWwCHNcItgMMa4RbAYY1wC+CwRrgFcFgj2gLqLRFvARwGCbcADmuEWwCHNcItwG9u"
        "C+AwRbUFcJii3QI4DNJuARwGCbcADmuEWwCHNcItgMMa4RbAYY1wC+CwRrQF1Fsi3gI4DBJuARzW"
        "CLcADmtyq0n6Dvbz8o77heUh5Vsm/K9Jk7wAfnzUfy0/LbfL9QPH6y0UVs/PKmCW4g30YLP5csf3"
        "SYBbghwxe6uPz6vN4aWoP28MjGvfYP/n/O6H5eWdysL3E4wbST94y37edjh2+u46uXz/52vS6mv2"
        "Na3H4zcLp3fLDxf++Hj5CO1ye+n93J2+FTydu9776S6uB7a7ZIqerh4M4rk/dePrDR6M1N/Z5V5O"
        "PTBk3831G7ar/Y+LZKz+uS694fXyj33pyefV+sv55Nn0/GmxzVxyHYjzhVO57jicznwRmfz1Zbl8"
        "/Tm5v3eFYz+t1std9uD1w8mPy0+bbdJ93uSAztN3lJc17nD15m2ffkT50+/Plzu53ELuI8rc163f"
        "l33buvhPxbet6cnSb1tzv7x+25oezn/bmo5j7o957vEf0v3g/CyuP4qnBwQf2jvsFe/vF4dN4no4"
        "3RjTORnnjGQ+n50UTmQ+np1ke+vUQwpgdqrB7GgEsyME5vz6ZwDIT58Hc4J82CGQe/FkGIRlIC+B"
        "tF8OaZ8W0m41pF2NkHb7BGmnb5CmgadXDU9PIzw9IXheSWlnIOvaDdlV7g8z4DyqhvNII5xHfYez"
        "Zz6cc7B0PDc+itMc7Hgc0wLVrwaqrxGoft+BOjIfqNxra6sgHleDeKwRxOO+g9jvEIi9QfqvCOJ9"
        "0o1XCP+6SvNEBcQInlQjeKIRwZO+I3hsPoLVhYZB4URGaBjQQnlaDeWpRihP+w7liflQ1roYa0X9"
        "QwKuxUMyDhWBmVOOqcun9ocMU8z5UJKNqgq8Q3HwVj/RPk3BVPE0hxRN9bGmu8N11fNOduLtPz7n"
        "oJv8/eM6nXlfT8G+45M8/rHIDXVy2Xz5/PyPRT6b5X7zWv3T48qy/LQ/XjYcTKou/LjZ7zcvHC1u"
        "D2/21DSZjlXxvk/HeOC5fnv5uNyeYpGlccNDbpaSsTwmbqEeRpmt5OfNOedW2a2ez/POF7UF/CYL"
        "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"
        "AAA="
    )
    data = base64.b64decode(_GUIDE_B64)
    from flask import make_response
    resp = make_response(data)
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    resp.headers["Content-Disposition"] = (
        'attachment; filename="DMS_SMTP_Guide.docx"; '
        "filename*=UTF-8''DMS_SMTP%E9%82%AE%E4%BB%B6%E8%AE%BE%E7%BD%AE%E6%8C%87%E5%8D%97.docx"
    )
    return resp



@app.route("/api/download-user-manual", methods=["GET"])
def download_user_manual():
    """Serve the pre-generated User Manual (Word .docx, embedded as base64)."""
    import base64
    _MANUAL_B64 = (
        "UEsDBBQAAAAIAFyg1FytUqWRlQEAAMoGAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbLWVTU/bQBCG"
        "7/0Vli8+IHtDDxWq4nAocCyRGkSvm/U4Wdgv7UwC+ffMOolV0VCHBi6RnJn3fR7bsj2+fLYmW0NE"
        "7V1dnFejIgOnfKPdoi7uZjflRZEhSddI4x3UxQawuJx8Gc82ATDjsMM6XxKF70KgWoKVWPkAjiet"
        "j1YSH8aFCFI9ygWIr6PRN6G8I3BUUurIJ+MraOXKUHb9zH93IvlDgEWe/dguJlada5sKuoE4mIlg"
        "8FVGhmC0ksRzsXbNK7NyZ1VxstvBpQ54xgtvENLkbcAud8tXM+oGsqmM9FNa3hJqheTtb2uEJrDT"
        "6AOeV/9uO6Dr21YraLxaWY5UfWnqg0gaevdDDpzrwIIpJ7MhXZQGmjK8j618hPfD9/cppY8kPvnY"
        "iF731NNNbcxVgMgPhjVVP7FSu0GPlskzOTf/cepDIn31oIRb2TlETn28RF89KIFAxHv48Q775mEF"
        "2hj4DIGu90j8vabldduComNMLJYpW/2VHaQRv5Fh+3v6C6erGUQ+wfzXp93lP8r3IqL7FE1eAFBL"
        "AwQUAAAACABcoNRceSZLQPgAAADeAgAACwAAAF9yZWxzLy5yZWxzrZLNSgMxEIDvPkXIJadutlVE"
        "pNleROhNpD7AmMzupm5+SKbavr1RRF1YFsEe5+/jY2bWm6Mb2CumbINXYlnVgqHXwVjfKfG0u1/c"
        "CJYJvIEheFTihFlsmov1Iw5AZSb3NmZWID4r3hPFWymz7tFBrkJEXyptSA6ohKmTEfQLdChXdX0t"
        "028Gb0ZMtjWKp6255Gx3ivg/tnRIYIBA6pBwEVOZTmQxFzikDklxE/RDSefPjqqQuZwWuvq7UGhb"
        "q/Eu6INDT1NeeCT0Bs28EsQ4Z7Q8p9G440fmLSQjzVd6zmZ13oNRf3DPHuwwsZfvWrWP2H0IydFb"
        "Nu9QSwMEFAAAAAgAXKDUXIiGC1NpAQAA0QIAABEAAABkb2NQcm9wcy9jb3JlLnhtbJ2Sy07DMBBF"
        "93xF1E1WifMQCEVJKgHqikpIFIHYufY0NU1sy542zd/jpG1aoCt2Ht87x/NwPt03tbcDY4WShR+H"
        "ke+BZIoLWRX+22IW3PueRSo5rZWEwu/A+tPyJmc6Y8rAi1EaDAqwngNJmzFdTNaIOiPEsjU01IbO"
        "IZ24Uqah6EJTEU3ZhlZAkii6Iw0g5RQp6YGBHomTI5KzEam3ph4AnBGooQGJlsRhTM5eBNPYqwmD"
        "cuFsBHYarlpP4ujeWzEa27YN23Swuvpj8jF/fh1aDYTsR8VgUuacZSiwBjIc7Xb5BQwPATNAUZlS"
        "d7hWMuCK7XNycd/PdgNdqwy3hwwOlhmh0e2orECCoQjcW3beb8SlscfU1OLcLXMlgD90ZLgzsBP9"
        "tss4J5dhfpzdoQ7Hdz1nhwmdlPf08Wkxm5RJFKdBnARJukjSLL7Nouizf/9H/hnYHCv4N/EEGOpn"
        "Dl4p03dD/vzC8htQSwMEFAAAAAgAXKDUXPTb2xfrAQAAbAQAABAAAABkb2NQcm9wcy9hcHAueG1s"
        "nVTLbtswELz7KwRddIppB0FRGJKC1kHRQ90asJKct9TKIkqRBLkx4n59+YgVOYYv9Yk7szv7tMr7"
        "10FmB7ROaFUVy/miyFBx3Qq1r4rH5tvN5yJzBKoFqRVWxRFdcV/Pyq3VBi0JdJlXUK7KeyKzYszx"
        "Hgdwc08rz3TaDkDetHumu05wfND8ZUBF7Hax+MTwlVC12N6YUTBPiqsD/a9oq3mozz01R+P16lmW"
        "lQ0ORgJh/TMEy3mraSjZiEYXTSAbMWC98MxoBGoLe3T1smTpEaBnbVsXPNMjQOseLHDy0wz4xArk"
        "F2Ok4EB+0PVGcKud7ijbABeKtOuzIFOyqVeI8o3tkL9YQcegOTUD/UMojMnSI5VqYW/B9BGfWIHc"
        "cZC49rOpO5AOS/YOBPo7Qtj8FkQq2kMHWh2Qk7aZE3+xym/z7Dc4DJOt8gNYAYry5PvmnbATlEBp"
        "HNm6ESR9ztE+RbHLsKtK4i6sIT2uxicklh37Yh8bK2Mp7lfn50PXWl1OW40VnzUaEXYl4YV+uQHl"
        "bycFlGs9GFBHdlriH/doGv0QLvFtMefg+XU9C+p3Bjh+uLMJHpftCWz9yYzLHoG4bN+XlT7NV98k"
        "O4ecF1V7bE+Rl8TbST+lT0e9vJsv/C8e8Amb+fMb/9X17B9QSwMEFAAAAAgAXKDUXBPa3GVONwAA"
        "RegBABEAAAB3b3JkL2RvY3VtZW50LnhtbO19a1MbV7bo9/sruvwlOXVOzNPYSZ3k3AQ/4qk4cQbP"
        "ZO7c3JoigG3OAKIAx8mpW7cERiAhCQnzBmEeBkMMSLKNjZAQVN37T5Le3a1PmZ9w19prd2sj8RAY"
        "pJZGSZURre5m77XWXu/Hv//HT+1tyo8tXd2tjo5PP6i6XPmB0tLR5Ghu7Xjw6Qd/unfzo2sfKN09"
        "jR3NjW2OjpZPP/i5pfuD//jsv/3740+aHU2P2ls6ehR4Q0f3J487mz699LCnp/OTiorupoct7Y3d"
        "l9tbm7oc3Y77PZebHO0Vjvv3W5taKh47uporqiurKvmnzi5HU0t3N/y5+saOHxu7L4nXtTtye1t7"
        "Y5P5sbqy8hr83tphvSN7RY7Olg748r6jq72xB37tegBPdP39UedH8M7Oxp7WH1rbWnt+xnfVWa/5"
        "8dNLj7o6PhHv+MhaBz7zCSzgkx/b28ybHcfdSwsVP8wnunJZJD1yXYCcL6+iq6UNFuzo6H7Y2pmG"
        "21nfBl8+NF9y7IalzT7urKp9P6Rf72p8DD/SL8xl+c30UHsbrfz4N1ZV5oARfIX1RC5LOPg3zZXI"
        "xPf4bKCRgfvg/WB7q8vxqDP9ttb3e9vtjr9b7wJGcJp3CRzJW+t+v8U0PGzshAPU3vTJ7Qcdjq7G"
        "H9pgRQBxBSny0mfAnX5wNP+MPzv5P3e7+I+Gnp/bWpTHn/zY2PbppXutPW0tlyrwi/9sMi82wXlo"
        "6cKrFdZj9A99bnK0ObrMm69eq7lSWUmv6P4v8+rHdeJx8UjPZ9fvNODvPXSVXp25tjMuoba2trKm"
        "PnMJtbWZS9AmBrXF53p4UQ8O6G8SemL+xBV1dzY2AajhnY33YUHw1kp5Ue+3iR8qcoRmTRY09bE1"
        "RXNvK5rHq7AB30Xs40K3nrHpq1V1WfirrszatMethTYUBQS0ovxvRamurK5jO1t1Wsh9NAAI0vi3"
        "en7uhAPS2figxXzx4bs4eEK+bGlEPaAq85C0tdzvea8jUlOdtb/ZsKKw5Pihm+n5oU38ELf/0Pad"
        "tanGRz2OS/AbsO3KoxbKH/nK4fg7fHG/tau7p97R9qi949NLVZfMK390PBa/tjWmv680L/Cv+W8d"
        "ji+/AHXI+u3P9Bt/lP/RytrPTfTJK77V1dqMHx/AT3g9rbi2plos+tDLFQee7KFXNdG/4sVNaUg0"
        "/9QoAHG1utJ6Xtx4LHlXVx6NzmOpcmNDjTn19QVFIaaih51qwnsIEivSCz95+VVVV6pPv3wZ8V2t"
        "Dx4eQ6LH7anm6NVXWDiwJSbiPo4Jtr+ecs4z10pqcq2oMVFbjJhADYe/DPltV0t3S9ePLZc+A4Z9"
        "uUpRUi8mtI1FFoywoTU1Nsw2p1jfmrEdYXv9RnhPT4bL+LIPvqoVRY0l9HFfam6JRXaN1cMkbRk7"
        "eZAwHs7XQIVVE+/Y8o62MEKKbFHj40ox4uOo01KD3I25Z1kibqGpjB37YAd4WWrQz0aSLOhHweNe"
        "SE0vlxFkHwTVAH/zTmjepL6aIOXAOkcs6GY778rIsg+yaiVhpO9OsMC28mHD1/9S1CiqK0YUoXrA"
        "ZmdN9cDycJUxYZfDUouKgRobUncXCEFl1NgHNaAVaPMreshbAqi5WlqoqTEZGnM90cajmj/8+66b"
        "ud6kxsIs0AdXft/1lPFlH3zVWgKoRPSBEsPPFTCARve04RVxqtxRtjyjxl6WhrV6rbSQVUfW6rJf"
        "IGu5v9h1uhJD0FV09qALoQQUh6JEDcV7xrjh8039H1H2sM1JIzLA3OtFjY2PixUbzLXJsZFa3NFn"
        "wyVghlZVFiMqjuJZddxBHdllg3HCkPLh5eb27uLW0UoNRdWEItdKGUV2RRG3S6MsIU5RceOmqrRw"
        "U2tyOMtnoHxY3/DnIj9ARYkkils/oXwcyWtTAlkexYsQ5lqnVDXXqu4ZZPHR4sZDdbHiQd2ZI1W5"
        "L4y+l8BIytlb3KgoypzBowTJxzxTzeUHRqU03Ll3V+CpFFhXieEJ9WV+ekrADVOcuOFixd9L7Gx9"
        "So+vsqF540myuFFRlKmCRx2Tqkoegk4Ms1d9gB/h8D+YqqYtjJQxZieMVXOMKX+9fVfRPDupwQD5"
        "BUqBz5UaqmpACPEMNeXu9Zu/77rJ8tQ8XjbgK/YgdVVJJa4BsihMzTYngeOx5JKxOsBm98ooKpTe"
        "YBZPsciAvtCr7s9pQ8WdlVtVlBlSJjZEARXwLi0Up6S1Mjbyj43UdD9LjsOpiMWM1d7UZDi1NFXc"
        "eDg5mQZ/UJVrp/XmIqnczbkG9ASQX+MFtT+03Hd0tYha3hNBXZW5mut3GlAHyap2B01Em4rAQtnK"
        "pLEfNBZ9LLSmhTZYKKqPvTX6R+C46zP9+ipwgbj8ONt5wwLR33d9amxUjQFXeAk3sKc+Fg2wZ142"
        "u5SamjfCe0Z48TdnHws/YdEBrW8NftXDk8RF4LWWoUF5/fjyRL+eGNTcQRLEemJUe9YPfyXlnDH2"
        "B7W3AWPVzabXqPrM2J+FBcMuENB8VXCnNrmQCgGkE2pyXI2PaCE/G1rE61z/YqtePeEGiYI1hrBT"
        "WNLgS7b9Cve4uKHPTsHCYMGnx9LJlfCHUmL1uVNidVZjADWWMF70HmmAH7fKr1q7e7541NbWIsrF"
        "T9h27vRI6E1XCwg8z7AnAbpovO3X4kFBa9NrgGFtYVtQB6cUROlYRPP1IsKng2TFsrc+IGTbbDIz"
        "U3UGTooWCaiJJH2lbSfY0AJSYHhG3fGo+4tabwS10PBbTGt90QdnSvf88psTTp8XgAIfmH9cTfr1"
        "Tc/hNkRB9pmZaQMbxT0NrWmBIAtMoAGkIGuY3dM9g2psk04+3G7hkLnWECLPnfrWkl12ZYVDYDua"
        "z6N5/dpIvza5ooXmcTPAIPt2xDbHt5Ch8vutLSlf3rhdr2gLu2w3YJctUXCagqEV5DrAMweseXwL"
        "ODh9rXlGmc8FlKpgmoGo6MFtTeyoe/tsGX5NAgCM/V59NWGbnUkxE9gSiQty0+uzW9rwCm6Snzk1"
        "GYK9gS4l3W+i7E5jExKq8l1rR7PjcbddNicr3tbmUCtPbKF5lBiWb6BTBrsgxNllD5wHcH8IURym"
        "GZtskK4DVljsBYv2gmRHDiH5S+yyCdkexW0EIimnB1WH6QRo5idYq8WrQJCqCDqE9upkzShfZ2LU"
        "DyfZVGJn2hubvmngPu1aRXNPqIkVOA1wsMVJhm/Sl22zBVObhfXXP+xytLeAfG9ovN/Y1QofbjQ/"
        "wN9vtnaB5v8TKu/Da4Y/qNCdNpL+1K1D/yWemtxCUbmwg+eW9JvxaGowYDh9oN8AMlgiboTBau3X"
        "x6eNwTcsOqJUKbe+OHojRWbu5dZoxiaH+syNV/Jkriqax6mFPFaRF1EUnAhQEdnuOHygFcJJYHOD"
        "skWn+QaB3oAtg9Egl7KA5glEqK9GQdL8BjQpbfY3px+sPhka2iTqPMRfVGD1u8Ded+HlLOwD65T+"
        "dF4MRTy3Xz9q/4Fapx3ywrqzntuAjw0maLcKAPxyY2cnwBl0IHQKuCf4tZafgNW4BReF67lsOU/r"
        "t9inUMJBbd11oi43tacvx7NJmXwGNtoA2A+AAKBEUCI07y8WnQIxAviF2hpaM/ZGMcS3OICW0+sp"
        "Y2vLpNk+wVD540TsamxUH0NmbL0NXTR7XrAlgTef6TQfubcfMnlM5g6PCrgonMYOribj1eY7sxlZ"
        "Fhgr/gQv7q742fGoq6OxvaXCbN/aXQEE/LfrjT2Ndtm2dYzOa+v1n3zPN/+9ufnvrc1/n/Pm803t"
        "glXvvIPjyla9anIfKBZo3mK3dJ6Zew6+Zb4JuNPYfmMkN5g7ajWeOkfWW7pdFK/V1Ur3nr5dovk8"
        "gPJhs7mApraWxi68gWsdaQjdb22Db2/euFlTfzWniIKpIpDCcODgPf4ETgrCBiAMm6/md/BAA/12"
        "wtk8oA99XF1bWXkj6yxlBSO0QBDExvFnMxdFK+vFxKTR5M0SScDd2fZr0HLU+Ig+O8WCPm1zRfaL"
        "kxdQaa1vczxqVq53tf6INsE3HS38I8hj0mnAEBWHhntnZM3p8INScVSQpcJG+mlOjebyoY2mV+Ee"
        "4LEOAPtL5ouz+fkzCdWc+dDBvszYzRkPscVzcmNT6b67pcuoaq5Un5JRcTK8eSpGVSszqtpj4Jwz"
        "b7rJ/8s8C1XXsljI9gpzbWsLAfT+cSetEQgx/zhveayBoji2IzMXuixZ4CdygAsG9hmkwvsC+0gt"
        "6jDovlD3VtF1IeU9AucljYW9GgdFpUIbe2e83ea8lpet7MJ3cXJCES6OzenKG5g/rq+xLZgDbyww"
        "81YTk8biGhr23F4DIwZkmWyroxwkQSbifYWH7xX4v9a28I2Pp56gL1Efeqc5ezm3cLPkKPP4jaE+"
        "DFYRc/i/2wd4Qy5wlRQEabe5b6M1m/Ud3uD9kF3N7inVH1XJqkDsCXvlBJLR+o8IrJ2gxLyXJ/RE"
        "6+8Asz5Gl8zpFRZrQi8BZ/zoHPB5Uk/DaC5JDEoWC4h3HuPDeDGP8lnesvwHjnMBGG4SMKvN7Z8d"
        "ZgeZ9wxxFYprCnLH62a8WWbqCCXO1Al68KvcctJmsOJM9BxgJXHgmUM5cBb79WmRANtdYP5FjC/6"
        "XCy4Lm4O+hGEy6ssGkDgcaFIRHo08Gi/RePaP02vZZsYUDn2Hi6M7zm0JoPStGLxEBJTUxMJYO+y"
        "HUwyzF4uaLTfuSIIh0VmIbAfcsoC26a2AGwzKPt3bboH2dUsPqe77fr1Vcz3g4Ot3EBxr+iLYSO8"
        "fLGeuH8WC/iCBp7UVNVds7+tdqIS+I/50V6F5JPsHQOaPFp1zQ1AVyrTngc7KfTnY+OcBf2WS8Ju"
        "IHl8dGQlm8eeF12UjHX468BTBaXu5rJkY3tJPcgNcGX6O5L+rBkCZbI7hOzU/bA2tpOhSZTp7Ax0"
        "dp4Nw0uT1EwA8Vr2qMztygR3BoITTiPeu/k8WjeXJNERkNL8LR4AaJXJ7WzkpsbiVEihLeyQyV+m"
        "uUNojnIcLWCx+Cjm3dqI11XdzK2u1D7EZ3VuLpwFYWewkQVBMJLEKgsMkZuYDbhY+GTKI09G5sby"
        "FkqqwVCS7KpLOT2pxSP8oifEkfLqy811UtkJjrf3Smz8zFrB+aUwEi4sclKQyjDYZf6l35z+tA1B"
        "pgO/J8MFqT/vpeJd9MSGn6WmXWkzd3eGEqukd15I3vY5A5tQfOGQzmB7At6UiSkcwxzILLJjROa0"
        "xQGENl0JDv++6zMiK7JHwQobyU4FqkbDkFN0wFha16Ixdcdl7s+nxvwsENF6l9jykag5ZZKmTb28"
        "hUjIPL0qk1NC5gmEfYDL1dbWVtbkMMxde7Om9Z8QNj/w4itXr9TU5pDbKWicByYFO5h8RxePYhyc"
        "sreN6JrmGqYHrXx75nLLHdNFReyAn15YtAmXZxu2eDILlCjrrJUefWvAINTEij62lgLzfmGQlqnN"
        "rdBK07FE94RIh3O9Q28nDy1iJVDSX/T1OprPoyYDtH+2/QJ4OUbjvBOwf+NFLwFCjo7YKMZIqxQl"
        "VbNh2AAhBM5ian1KjYcpAyGXZNp81WbCit1RFniRVUM2pE3wcPTonOF5Tav/fdfDZSagwLUiy0PQ"
        "iVNP8ppBcvKe5K0Yezv67pRiLh6LLqIH1u/xp+aW+IS1frY7b4Xgbbsj74k7Cg6f144u5qTM7WEy"
        "Fx1yXmpI2ygSlTXoA/U6Q2icowb7ok97FkKku7BKgjiJfNjY9muqAqWFyCoocB4sKlXN1iTm2nzY"
        "GCIelN9CTBS11ae+A1qsaw1OMz0IZJXx5ymHBXRZ+BNqfFUfm7dmdZ2VH+dV+Oc+vDcvIj8SkNku"
        "FrhwLyuxY1ofnOKGrwHmDS1drY1tCp1HOMtWrxJADzakASzujvMasz45u01p+JqTw9I6KAtWZgHv"
        "QbWqefbh7aDNpeaeiezB0IYRXxeLiA9YXaMUXIKtcpION/NkTz5HrL2ykCjbyMIrFj19/lFlZdVv"
        "zt6vvr6Fn0whe2zSUbmW7zxNx3ItX+aLiUBVM2WW0hhRUvCkWGo5Ivok8SYQ1A+LJZ+yV8/0Vwn4"
        "fGbzkGBSLAmzOU4ft4n0y2Uad16kHk9OV2PL+qrfXM4O2w1YTZ8OJplSPyhfulkA73EFfJ/MY27A"
        "YLq4oFFesJjP5oY1OaLpCP9QFl0RMEAF4L2XMiFhF1GWndssMhcP6os2Er5y4rVsLHHdwUpkls+H"
        "vZQHC+K0VstTbaVhmy49N+iRbBnOkofURKsnATUPyXFXNj8ecR+WdHAOYLuDQaU+x5wKwJGa3Ce2"
        "haGH4ajMvGymaZvsFjvwmRuo0BaxSI0YcgYfxgx77jYypmf0+L74Nme2nC8XBPVf5VayJWNEb0Hu"
        "kWDB4Uz1h1cSATbz6KY4L2lruUypVSecnbvXb4LZ8Ye7t+Dfu1/jv9jKE37cu30Tv7l1G//97sYX"
        "d+HHF3fwXzWxwgJDyr2/3FP0TV4xFdqgt2LUwD+ubSwK9hlysuVV0W6yurJSufPFKWyZcjXEe9lA"
        "xZEJlMHnD62G6Oe9NIMgt9a1QADI7HJn8/2j7YrcwFNTU1VaFdHVl2vhfClY8Qxnrbr2o8qaj855"
        "LsZZSOrGzfob1+wKs4avPyHPSyGoybaQKRyx2DmN8zN5hCs2Hhh7DTrop59/Uc9cGywQu3z5ciGo"
        "yLYgKyTLsXH/lX/Mj/kVbX5FD3kVRfk15BGTNeGXf8xPjihH5rrlgR/ZFGw5+Q6lheYtv7IW8ytl"
        "n+hxjQJ4L33sefw1XjbHJGBG6YktBCoyd1hA32G1Iqg3R99hXr0isnM67e7gy5Vaibi1sT3AS2o8"
        "BEgEe9xehrbYRmhN7n6B4d63vtRSv7HqJqNNEVYpT0TGyC51wnetsWicRWJmjolPNs/kR2y0Y2l2"
        "BCX4W3FrI7mh+ZdwOsMf7t64pVi2uK2jy7U8tSxzGor7oPpwcg7GxYWarUkEPM0MB20NLYgWFLxJ"
        "EnpuDp/SIprrwqEi7Yf7EH365p6efG0yOD9PaB5JLc3BZ30Tj5axP63GZ+klOBvKakotgSRf7sgL"
        "bvmayYRm+nGfvAEPgiO8yZa30m2bLSlgN7dYmnnyoT2EOmpbqyaG1ZhX83osP5hwRPN7eLq82wrS"
        "mH01otK3dmK2yGZpZZbPUuoKwhbX2cA0c+7aaMUZnYUJJ2lC8r/BZF9YNLbBKncNLmcaZGvd2Tpv"
        "wTMNLO4AokGbWwHpw6XDGnvlzJARZhsvH+9T9URfB8nl1RN+HB+WHKfQuxFZoduKNju9ViSoYTZB"
        "4bPTJGWBEqvSUwRFCpNP3Z1iLrceX+U5wsO6+52cfEa32Y6JHpTVmyJLTohmEYJz671Rw/MabDgw"
        "FEDRsZf4khPJFKqUNftVmeLBNmuFE5oKOSndMCP3UiSmcnQAbRnxdY6ImEv/JQ4SDUjP5gr/FUVJ"
        "je5hDFRECKM48UtK6SzQ0WVBH+aQHlT4KdKMms/BNVJOr+HkJWr+BSw/WZpjyxN4gHndEE09In3e"
        "0uFp33ZMkTjUGWAt13IG4JW5RTt6AuRmdbQfoWnjhFDElebZw4z/F720Kyzq4g32zbQV2+wku4QS"
        "kWPRIW/bm0GHVLamJvzAyPTxabxteN6O+UQZlGalS2Ia8tI6JWMrXytsGecukQZVOM28rIWXS0Vl"
        "DsODDVRmQDyEBV5qUxFj9TlcP1BKFNkR+igwGU7TVv0HvSSjnBTfE/LAyRb10fyR1JMkHgmqT52k"
        "MavcL+nhdSSJYZIzRV5aWnu5jtoX8HIY5A3L/bkkBF/UOA+eicOhbdYE0VxyDnqu9H75efWVOoWN"
        "ulnMR94OKj5igRHk0FymkNeYhdEs02djwJ4tqWTNMiJplZGXaKM6R5ystD/IJ1P708leXL5g7p0p"
        "OO2yXHEi3VF5cZgNfJjIFM0NIzuyCmAdVrtsie3GmfudNdyXxhNrEyLv0bzuJmIjMlOTA4QiNbYM"
        "muzpyxfzevSvSq11bBQeO845LbhtWiNmz56w0J62+M6OSrFQJUnmSF067LI+4pyWkovy7qCZZUlP"
        "a9wmKYU4FDrhIsmbcbwlmTtreA8UXYOwxizL4DAJcevlzD3NkgkWH5c7nhwLJlJSiqXkRo2P8ZKb"
        "b+r/iI4ytjlJ1UcFkrJ8hCvVsySGFR6eJQw9FYFZHDYQCLLABC0WVR/+K2CO5ChcBPMckKfGh7Vg"
        "COe+PvWJ6l0KQxxwhOYvcT8ffPMKliTJ8LHLaT6Gb2alFYi6CmGp81C9nfhSaI3WJKqkuOuAd+xP"
        "Hx7YjDlsQ6oWkdBiLzePPEDTiCRglebJ00Ib7FUfcEsMC4CCYSYOa+4JOo7EIY3oEzTOkYcA49RG"
        "h00w2GaLoqTSrEQgiwmjJXJJQmiDwpU0G1HzeLGWgbcQ0+MLqbFpG+0nO3pJjl/YEjXg4RXpIzRB"
        "Xd3xwp5hS1QOSFyRCJea8uXo5CouTlhtckKZ4YNVViDJJihwMC5kFCdC3qHgF5yZrYjSGUwKEBIO"
        "G0YIiWVGpUX6gG3IkA6OxfeI6YmJ6Qf53hlSVPK1B+8etnMMOY0XvbRWVBOlJIzsRB0qo7CSc2ym"
        "2JuAp0nJtCV5UrKFNKS0+RVkBLRbjiIj4meBIeyEBSZwfMxeSRskeQWWEqNgIOCuTKd4eueg1I6t"
        "UZOf3OtOyzkc5RwOG+RwoFBPuIC/GIs+6/BSw1n0WXFuRdIDxQb6h/lN2N7VZFrUywgsWXgFMjMz"
        "tzXl7APWZSyuaQu7+quE6QibxUAlvdfNA0QuXhkN3IFrCkXrQL6CCaTkKUPdtECCH/gomqLclYiC"
        "/1WfmhwglMBFIxLhzU+FYk3ixroTlE416aeyc6xC53tJDfq1iShZ7ONbqcW3oIqzJ1lhDRuqBZJo"
        "MpFiL11ACMj9pD6+wm0gjJVKmEM0RHYA5DhRlEOdkHG8vVOOLZZji9lcLNfYIreqh+aNJ0nSUVnY"
        "Yzx3KfdaurtbuhqbejCPxLtClKuFXmrPndpbL+g+6Tt4srGL3GoyNWPMihv6Qsr8c/QYcm1yhye1"
        "4c2xx9DFSAZih6nFd9gg0BzeS06x7LG9hw3sJftxPzUZVhMrOJiLk0kugqA8W/HC2d3pJ4VzKs5v"
        "DWo2v0sXwsrEe2g/gWVF7mitfHi5ub37mGE/FY9PXbVaauPsj5oiorDoAMWqCJboAfe5sF4LYWq1"
        "2MSW4YWGb7ER6YpCw8zLRPreRIohOYkeafYChgLcg5p/iQBcaMgWG3kOCR5q+XiVD+sb/lwm0LNy"
        "UXn6fNpvzkEM7JRcH4UGbpHRaGAbIJtVQ1RoIBYnhZK9kRHQEQNCeKlVocFaVLSJXT9E2w8WGEk5"
        "e6msqtAwLE7STLlwuIbScOfeXQFTLeRnQ4tsukyUpyLKZ+NitLj+S1yfTtpDLypOmuSO5PSoDpGb"
        "i+GJqD3AWlSUCepmHyrx6PqLrwqocpDKrYcLDdLipFTN5yGwUrsPzbOTGgwQcE8aSFum1MMo9QU3"
        "N/96+64iYMmt+COzwcskmguJWh21mMev7zpFWqx7AGS/PSBbZFQ6NiLmeLPIgL7QW2joFSddikno"
        "7gmRazmdYMnxHAGKPwrYuK4OG9fJMRkW7C+O2cB1mCmem/s+L0kS0QFtfAurJ/lisPWRpJLwVK9+"
        "5V8V2bvz+67HcpNT0YbsnMS8MF4DQF5z9FJiYss7NTGhj701+keOnxxbyLw9K/b368BTzJXIRpK9"
        "kiaoTpESkWlCI03uJUkjp4Jayco2Wr1I+cjwHD71YeHI7jhRGCemec0dPEhhbhZ+Zmxhu3ttKkKq"
        "iugGn1M+aDnfsJxvaIN8QznCSG1X6SCIfos87yzsQQfw5hTrWwM2KqbGLm7os1MotuMj9JUa26RC"
        "VKyzGN8izlu02YN1WEGQW+wwHxLy0OCbKS19VhSOuaNY7uHxk9jQZ/pNcWIfjnuMoMuANaWty6Oq"
        "DsCAD3IVQ0Z8Z2hmetEVVVRLJa2YumMZ2xG2108dQfGA8Wp/OkL0lX0r/9XkPmbW81QFecWYmWwi"
        "R3Q44NjEDk508/ZrMUxSGjZtr10dhSjcm3m2xNL52cK2OvxBTLEenmerXvN+Nzbo4lUJRmSFLtKE"
        "WTtNQfYNsvAM5q6am0z3qhXTq6PZ43Jtc8AIugRangxvds7Amhdj9Tkb9mL9FCdCPGNm5Z+octx+"
        "YyQ3OD6jZ+WPeRVFvBMyxhSOdHvbipVnxz/spfRb7XRFFuhM/0Fm5hYTg39Je8ZtVuRl1TwR2bvn"
        "gOyZj9cMpnlV1LKm5eQysML4vEg/C0ydaVd5pfxaJecMmXw5KiRDESknK8tEgfUp+thbthmkhJP0"
        "fNfQmnLjp6aWNoVq71JTLjsR1bEeiAz42+tAZ/sgrKZYxPHJfj+TWX4udIMkQeYVc0/iqMXDZlvc"
        "uX3nhoITf595cdKFxylPBSXehGPjRCcqMQeDpsiJ9mByqfPJxcJkkxZL5j61KcZcpPQmj0z4yAdO"
        "szvvYxeZjNQzatYPbII3ZbbaLlNRsRqP655fqKhYjXt5axleYOwfByq2+v4Tk8fi1/CeGh8A4aQn"
        "+lkshqq4OVFAA9OctyXVFnbw7/I2cqJiZOkZGwyUWoX/VfRgm/lUOWSn2YulHtaT20b8VNik0voE"
        "pNM2qQ9oGe+hFkvEfqT24zbaTMYQBoD0YeMUZuk2uqJN9hp7O1SL8/8meb90bP8Ed6HTYWhNG37K"
        "4jkdqcKOM+BUxvGnzW6hRbfuZf43KAzHc2qqU64TOsmJXX3N9Amf5nLF2V3epRxRhzPFAtuHUGVF"
        "GjwnA+kMs+6KC0oSm31fWFXX1l4rZVjRULijoVRxhsSMOtvOrn3v+avm0bHj5khZPi+Ct+MOT5pZ"
        "WVrEWl3axIrWXJlYS4VYa0qZWMndUCbWUiHW2pIm1vA70ATKxFoqxHqllIlVm3RpsViZWM9CrBeb"
        "1ncuJulxgPl913telG1vSJzmOHAHrOV3vXz58nkdjZIA0dHHB38UsM7hKtY5ZEcD9OW4uncYlT8+"
        "JJHzgtoqU/mN5OgWnZVDa3wiCgbjxOxlMyRHM3wsJ1p6ULE5cUyMZeZV+enQ/f56yjlPDZnV2Bgl"
        "UbCRJLrhEvNqzFl68bVqa3ZqfJW51zGXlA8yJNCdfrPngnCc6iFje3mCgp/qzjzFPxHt80E9MmhO"
        "GxjXvRtEApjzwtub4uPm1G2r2zOOa7IGHprEou9OGHsjzBfHFLMjRnHTaE2a48WSr9io/8AKjxmh"
        "+7gY4/DMtc7j8LprVfcMsvhogSiBt78dcGFOKV+JiFCKHni8Nx5P/cC6R69fG+mnlAnMhtrZqtBC"
        "bqwgGt9CPsafx2d23lGbROXWXXh5KApfUjpJ7hMNiumMX8MYusjL5TDQFnaOLFfOy+l+McFcbtGM"
        "nWeomsmo5oQuXmBgkR5mE5tL5pNDMAwt0E6onemnm1NPkthkbXMKs5Jd79hmUJ76ZaNIrpzzoy2M"
        "mPMoUPZSkBdAI0PB2r+cWZ1uW29CynZ96illC6d7hveyN2OvdIgDExz5MC8WmMJFz8a0UNwc7+UD"
        "Po/TRMxUd8w4HpovDym1QdlXuZEwvDgSsApsWeAlSEnglSLROfPwYUthzBwLTGjv3Bb/kY8BHF/M"
        "Y+I35Hp2j7RpsoyFAkpE0Hopw5J2VSBBKC8BDBY+vIenuMY8+qqfZBsYJrzaNK/VQZmOj0OYpSR+"
        "jyfj4+tTPEJNs7pU6K8SWBg+vGb4g7wn/gzlOhKUlBt/uX1TMXU8c+Qb78AChFpdWV1bUVlDKZWy"
        "8LcX7DIGFp4dfKIqziy/kt9IZow2tofVdk4PmUDSDWjGyDC3F4Qkde+9YZPBt2igrUloPhlO1j25"
        "08172wEXqWEKfs57VgABWLqYyC03x+aIytKosJA074TmTVpzdcAqZvM56Tf525jArZmmau3tIMd0"
        "pxJTRngZNDjMCj2U09hOXxY7k0bYpceGYVH2QVrGbvDWJOP4GGxQTSSxL0PIDR+4zyPNA7FqiBtG"
        "9JLSs3JrlCyDvkByncbaCquM14PwlVnFwOZocTGMEZ0O5LzAtfchpg6MLJQ8VdxQdAM9f+Voauxp"
        "dXRg1rKoDkA6ppfR1Cys0Z8fZrG+CngtHmOeS4isf89LdmPDw8aOBw8bW/9NqX/Y2tF4viO0bEIV"
        "tYpCw0eVL2/crlcKqu+13n3o6GhRCNc4mIMvifqJCGoJREQVfGiNTwykEjJYvlX9ajEDMSdt8p2R"
        "3ND8S1iT9oe7N26hIs8LlcXbqdRZKgcsIS+lujNHcz6k9rAFwm16qDEN4KQeqzSwE6e1c2dOarof"
        "1ikwzFcLcpYWT+EM5U5jkwIcXPkOrFDH4260UtgckgVVoZfa+fwYfZPZTWlzrPgqcH1PdlNiezm0"
        "gJ7MxnVpTYlXjLjlqTRoGFAT4PEBtr+u+bfTw+1f9Cm32htb20C6pMdrg6ZsNg1GyTKXU1QsX3um"
        "2B2flGPK2/KknPf359XWVB9W6mJePqubr7qqrrSrOHhAmayTo31lucGqrurjM/SKtGkWBIKCcxZF"
        "+R+NDx0ORfnmUU8bEr7SWt/meNSsKN9+CwxWD79SFD05ok2NVtUdk59b8fjUGUhnoL0zuJgvPAWJ"
        "i80curhfHJFZZ8GORNbd3tN5+QFS2uUmR3uZgLIhpK9HWOCYpsD/pIRz5drVMrUcwm4avqq491VD"
        "mVwy4PLrVFBRRI8xgBEoz7V1V+ho8TBGev7ypw33Pv/jPYAh3ANEZt1TprZDeNPYmubeZsH3rlsp"
        "OXr72fHov5fF2nGq9wnNxf9J6YbFx7BndmwU+8pwEAEbIk08FXJSIiX1USqzpMMBGBhBh2o8Tr6P"
        "MoFlwQezGH7f9Znht2GLieuzMRb0nUhU5EzI3FTe8uA/5v3+JT8kBViMyJY2NXy4K6kic7H59kyS"
        "E1J76zUi47T035z+1EufERGeSR7kxUw1FhzW+tDnR8Y15sOPveMddp0smvEG23j1qBOiNrlgRCLY"
        "D/FgH5xT+V7LDdnLDdlt0JCdJC5xRpC7ZnTHly2esZOV05eae2ZsvYCb5ckpPOD7wljtlaeTUKoP"
        "T/PwGsmkzMgw2do/ecbMvXJeajkvNZvec81LxZSI0Esp0ifFO3nSi8iGEBb0HTRsGjs7FZzNses0"
        "/B59bR+MaDksuu9KLSakNxZtOurHfAgBSfCjBnQVKoBo1lct6p5BALqIG1LgU2TEu38NeeyVRUVB"
        "QNBrSFEnTYfUdVgupVEgxYWjajKAk102FukRTJkSt9lpPzQzi6CuxhKppSk5u02gSGT3eDD/a/M5"
        "XMT4rdDF7bSZDJ3V0latBBc5f0JOlcAb1jCzF/Nk3AkqyCPltdzl0MYx3lLv1IcDqolmT0hDOpVf"
        "oKamqgjmKmb4BU4Nv/PxLJ2lhUL+PUun8KGYw+9mWNDNgj7Ns64FAkBalzub7xeAsOwNrOrLtcqd"
        "L87VVXkWgsq/U+5kMjI1oELQjK2dlBg9afmpsb2zreXc4yclQjykaJYpJ9O2OA1H/uelnmwTpExJ"
        "GSAyItvouuMp4bn4Li6aiuw8V/ozoVpHB46yDo3BN5a4OxaaF0dt9gaglGT/68DTglObrVVKqngv"
        "DA3Zrsed7KdRfp04pt+xIB1yFGSuPI/xW8udKriGWf5wuI8o7QgmgBRL8RPz91Lx0/oU9u7iLZFO"
        "6eTLo5O7qhJLfdTEMHvVB8sVZam8elXuhHN6N945VmzieKXt16JhRiJprYtG02OdTMzF/ONUGCOm"
        "Cs6GWXKcZ/hhMyrNs5MaDJhjUfnmxre052/lLWIofnIhFXKmnEH4Q5rHi3U1ibi+7rWjmzaj6OlE"
        "DNquBkquftJfTxlbW7QBmoOK2OAFmOhJjwSMRV9GMyo+F35N3/3F2J8VvX/ocdcb/U2iXF1ki6hs"
        "zZVTS9rKm9ftq4DQoSMaPFdV7QyQul5/s/6GXYuIlJ/a2/gCUFx3tXS3dP3YcukzkP9kppcBeFYA"
        "KgqY8MAWCZBlEJ4JhMbiCnOByhAwwodVM5dBeBIItSGnFooYb/u1iekyAM9Eg2AS6fFjutOXgXfc"
        "AV7tB+1Q3XlRaPjZW10xtpbY0GJZ2p5dXQmM6CvHFO+WQXck6NTElLaxZ4SjYIfb3TVVVcl77Evm"
        "J9me2PEwEoCLZGYq1Qr26/wlzsfKHxY5eWyDmgPqP4QDsZ87tfkVY/W59ixIrafQZj4iZZ98I/Zy"
        "EmQ0VqKGzMKzwZtNy84A2frHRm3jW+r+nD4+ffr95Nf7Vs29b8pfb99VZC9VjhmnF+R2S45qfWs4"
        "VHp5FVZEa6HG4NQJj9q1q7ExgYGgn/qpSQ6nQSMyQB455vHru5i6KYY/BCKYSpy5YXKUouNtY1Fz"
        "rmKHJhPHmEbpHki5/NhSk7fUs2v78OiABSLq64kTMDyjABw1FicHFm2cr99GK7fwiE0bQ07CJro6"
        "FgbT7taZfspm1dwT+u4EC2xT9ywTv6KBXZ574h63K+RrksPhb1Z6QGVlFWYIkPdUYb4dJC7cfDzj"
        "EexZKzVotdHGPv8INvE3YYiZ6nDWpgSe+L74E++/oXy7tY9gjfYSVTiZ3rUinW0stOTthQgPgCHm"
        "nkb5awncXSdb9dpX7IpWl0RFQnuYYaMv1fisoJ7t12muLF8ncUxXQtjqnF5ipx1yJQjUH9HRVSDB"
        "3opCDXlPd94pd6/fRM2UBip5vGzAd6Q2evGqQiCCoz2WZw4MCuGdf0GTxmaasFrFrIVwc60hTnE8"
        "FnvBor2puSWcE5LYhW/lLdmJXPiZZYE36t4qLFeb27c69OGUD16Vf3DpfmrVZ6M9YMBtF7YRt/QS"
        "wZ6m10T3Qd4EBecOcCxZt1GumLG9loFjetxeNSyiwEhCBZ9RM5hammJPfcwToc82WrElDoiK8LDI"
        "NTekBSOopQOE2OF3WwcotfhWnDQ7bY2v0eQA2CqVV4VjB3M+XOiAtNjBKQt6YlVPbIr5Ot4Nfd0r"
        "RqDx4VLlKvLH9qyjLVeRZ/Gh7dcW9Qs2ywfRYQ0g50DpLuPhKNsf4HMfvGj48rJamjlCWZTiTPBi"
        "SDTAtlfgfBRtSS2oMbV8sCH27lgYYcklY3WAze4VVnk5OKUD1wUK4WAcO3d/3lB/+7ZYMKwzPZFy"
        "GdReLHHUhlbYyBBYxGYZqJ38ESRdZEDLiouFBktfQa2S32zWENtKuHP1xVo0uTlRUvJBqOxFHxyh"
        "ctqPLYRIqcXRymk/uULq17HQr2POcprPKQDWpygCanJmj/I/ubvufynKhzUKJb2CJvwvZWDmAMwx"
        "BKac40PArEZgVpWBebqjXE71OctRpuwehf778EqZ6M5ygs2oBgGxugzEnIFI8DtN9o9kLkrbyGcG"
        "RvVhhiEaXN4NFthmAy7qqSNikTyuhfY0xVjHo6nBwOGq/wnWbx4a6nBbESeC8dY4aHG61sAKS+/W"
        "NCaZ56WxtaXN7aNZQyUR7qiaSGj9IgYP5mUOA4eIYIqoXkqNOXnJFLXPw6FYQ8sFcgmk50WJJqO8"
        "ThojFFKDPwzuTb3WNpe00Es9EdCG3drcE7gHu8+OTFEprPY2APYpm14zwvupyTD5c0ttTlRVVXqI"
        "/ZGNnQt7+jICyvJa7RV2NfZGeXA7arV/VmPL6PXjwUp7+WIyOr1SyILWra97gc+h+3PcDSsmvyYL"
        "RnAssXkktMl3lE1Ee6YHNY/feJKk01KC5wTzvHi/uOI4J7RWzPRZTaSml+17YMh7S+sTnZXj66Ap"
        "IkEO+DMOFZ0oG+0h+yBRSgKBvwSPQY2iyBRlOzyUwjFA7bH/Haa34JhLQfriJCwO6OPT+i9H9OKw"
        "yRmwhAmpgsbSOoEf42Q7XmyVzgu1hUrGteqSlRu1ipJyOjFxg++2QGqx4V1h269l5QlwYTW2xm71"
        "PF0MD5C0WGx27/TAkQK8iCQaPjxbRNxMdQCjoZE5TDKbmye2DUyaEEqaQmrQD3QsKwuFSwYo1ZhN"
        "uYH22RpoE59KrLChBfiMsVNeN9Lw5efVV+qUf1VSMwEtFNdng0DZVZWKGhtE4t7fVBM4XAoTYvp4"
        "cUNog4Wi1A1IzIbmaVlqzA8v16aG8aLgj33WWEE4GKhaB4etQ2IevDMmCBSf6yDu464DzeMFMFPq"
        "fcFdB2pimNZDORyiIIBPEgdVwkoQJilG6Wp6fB+1jI0NNebRJnZo3kDJibPqy7zOysnm5/XkiFlL"
        "gvEuNeZUvmu92YpDRfVEKJeE1vwlC6MawvHJnvr0sbdG/4ix/wxbnGWt20ZqlTmqngXepMbEEAqi"
        "PrlzvJySLp8hroW5xa5tmA0iJ6kjD0hsARvGJE+TuEh/sN0gbBwtwoEqalHMpSM7sNwjPAuM8JBa"
        "fJuaW7LRBiifLaM0iLvp/cDhMBGK32Dyvxgb8NHoEwyJcZLj1GWb/dCCRCEA105pV6LsEFTZ4ah1"
        "lHjxmVw/k2O+V5Ex6WpFMfZD+prX4tA4JTPoIx6nVChsecI6YoWrPQCjJIMt4zJDazJb5klhWHNI"
        "cRPaluANvOIQRzaEnEoTDre+39bY1dIshDhvHnaWUrYLm/lBe+Ud9OTmWTILp7RT2mVqdA9klL2c"
        "FcS2aZWioVfIo427tdC8UqWwaAAMPgykxLYwCda1AVRG2wAUW7zSTvuR2LkIDPHoDy2d6IyzDpmb"
        "I48Z9avJECfUobItW7ZlbWHLymzDOo0gE4UoXH5FIR1ssmiam8RV0RcU2RYGKD/bJu8p1jz1anRU"
        "i57Gd1q6u8EM7j4wC4o2iJIx4cJpWQWswEuP6yJJx72+lC3AJ80R/ySx4Y4Su7IS2eErbeydNdjC"
        "NnyVMtczplyB4iUZJDYSA8K5LgT0ts1k1MHJTofMdELyEcMJ9xe13giGobgDgp4gQU0yWaYpG+1R"
        "CGE+dUqf34RtkC4vBqPREeCBAtqAVduYye0GXKSQnGTtEhMvCkdZarqfJccVhcVi2Nh+Mnz4/Aib"
        "sN5vq4ANabNbGCnnzlFjO8L2+kXgYHhem1tBPsCdZsb+FFf3J3/fnS8c90XPSdZ6saaGtztJj7WL"
        "LGFAYyrCgi+ARYNZySkN95GuJpqImo/3kR19IH/MhAHdw9mh1AYHjKCtJbY7Dn+LGuAY+736aoJe"
        "qyb9fNzwrPVatJWsN7unQY4x96DmX6KXy3+IFolOMF8vPhWYMgdIcFWXh2vkZy27sNRM42+rf0c8"
        "Rph7QY0PpCvGTK8a6kFPklrvEgK2kFTJXdEzfYAsLRBU92bZ7DPgjfr6NNm8qMQl4kY4zK3KVWPR"
        "BztKTS8Lq8TjZy43IVd0hhqMW3FxtjyIebicJEgZ1J6FsJlFMAIwUeMj+uwU1n5srlD3H6W1Hk1r"
        "5XpX648t1JBb3Zm3qIcewPw6jxtjIMMDLPCaIFhy1FMD1IPdDxJOdceNopWXDFIVKgCaz4BIjU0D"
        "gXEZXTCWxuUEnH42u2CjZj58NDPKhtBLsjqyZzIDxcHKMYiyM6evL5wtiHAhi6f1UX8COCZyMilN"
        "kc7ejF2WLoKN4wNYys9plK4AHujYA9S//fY3Z29VXY2C8SxqnB9aQ9/N4ls41+Zt9sGGa4MFYuTN"
        "TE291nvX2dI0exUwxpKAHOXKtasK8HSltu6Koq9HWOA5GbhG7xhKc/4Y9SRgIydrUkXFomrxgJHV"
        "BapyYgXVq+UZFoia/tUh5nqlxtcLq3PxpaHwiQ7IOpdIT3FHaY0CUc97QcAIWfT15w3Yh849AWKH"
        "hJQUwnDLkgro+ZuOFvPj9S5H5w+On+BZ0SzlIEzwZHCR6xtkwRFyPwu1lVL++XosmWk5cHAJYHOE"
        "4NwvH3jf7oSxJ95jYQMbKUV2jcEtNvBaf4muRiHD3QO8K8CJZQHFSI9XAEoAWT5zHBRcpbunsavn"
        "b83t3TgesL2xo1nBsSAkUH1x5l4vIFVy1Qo0b+Zaw7hXEFsYmWsHtZ/6ALKVJMbCOLfX17wsHuBO"
        "Gx/mRhGt8FRpKzuAhT3Gc5dy9+eeh44OUN8PAQBYFMBgAyOG02X0g3jcQC80T6+mpJIDr0Aoqntz"
        "xtsJFh9FXSw5oEUC2HoCTBgwExLPUU6ZGXbk0hMrpzy7/SCoixqw+YkoqWwlR3N1Fg/UFkbo1GEY"
        "MDEN5KXG43rIaw/uZx55X3bZFEXv08OIsO8GZhsRT48OZE+/gesHm13wRCZhmA7GweqWa5KE5mra"
        "PdiWlXuKAEhqYoJGvQmwAWusb/izgjo/bypJpqkcQyVOaXqeSoyYriIx8Xii3CPPbC9cKBqiFVHD"
        "qN4/3L3F/72BP+5+jf9+eeN2Pfy4d/sm3nDrNv773Y0v7sKPL+7cRWHKg/nmNrhRB4QVGFLu/eUe"
        "3IQY1zd516rQhtXsFE0K9zRGwhIJNTlOT6fggHGyFIH4nXck0ok3UZcP0Rs24RLpVbEhYz9Kj5cc"
        "wVxDvWB/yghHM+wEzTmMUm7oWaFZj2hMxHWU7LRFEBlSgVXfgYzGpF+ND9vIpORxm5n/U3G5s70b"
        "Zerfmhwd91sfXP7PbkeHXdb4XWtHs+NxN6yz/pPv/9Td0tX9/c+OR10dje0t359p3RfEUahzlZna"
        "CDJG6Wzs7n7s6Gr+28PG7odce7OudDe29Sig9Kqxlyi5wti+jjl3tbEdbP32Sxwu6hsv1Nhr5dIl"
        "ZATUOS44TPmuqGtnlAhg52WzSuC8WQJ/4FSu/gsom86O1v7qHFNEk0OeOa/AhUP33d3S1HMXX9/V"
        "3dr8R8Bw5c36uo9rbl4yL93twouVdZV1NfXmxQZ4iF+tqa2rqrvEN/eg4b8owF5VXU3l7A/h85Vr"
        "ZrS988GdRg4mRydcr6090MPtWiWRmaOnx9Ge/lqEysW3D4HRIjauUv+3+w5Hj/Trg0c9/Ffx5wBq"
        "3QqhsoXu4ZebHU2YMoDvbu1oudva0wSrrLECSwQN/vEHR/PP/AM88qgdMPXZ/wdQSwMEFAAAAAgA"
        "XKDUXG6AGxIyAQAAywQAABwAAAB3b3JkL19yZWxzL2RvY3VtZW50LnhtbC5yZWxzrZRBT4MwGIbv"
        "/grChZMUpm6LGeyiJrsqRq+lfIVG2pL2Q+XfW91kLEPigeP3Nn2fJ23TzfZT1t47GCu0SoI4jAIP"
        "FNOFUGUSPGcPl+vAs0hVQWutIAk6sME2vdg8Qk3R7bGVaKznSpRN/AqxuSXEsgoktaFuQLkVro2k"
        "6EZTkoayN1oCWUTRkphhh5+edHq7IvHNrrjyvaxr4D/dmnPB4E6zVoLCEQSx2NVgXSM1JWDi7+fQ"
        "9fhkHH/9B14KZrTVHEOm5YH8TVyNEl8EVvecA8Mz+GBpyuNm1mMARHe/Q5dDMqWwnFPhA/KnM4tB"
        "OCWymlOEa4UZzWs4avTRlMR6Tgl0ewcCP+M+jKcc4jkdWGtRy1dH6z3C8JgSgSAnbRZz2qhW5mDc"
        "Szja9NGvBDn5g9IvUEsDBBQAAAAIAFyg1FwH1K+Zcy8AABJVBQAPAAAAd29yZC9zdHlsZXMueG1s"
        "7V1dk+JGsn2/v6KjX/zkbZCEAMfObgCSdhxhe72ese8zTTPT7NDQF2iP7V9/JSFAH1VSVVZKqpKy"
        "O8KeFlAp5Vedk1Rl/f2ff7xs735fH46b/e7dN8O/Db65W+9W+6fN7vO7b379GHw7+ebueFrunpbb"
        "/W797ps/18dv/vmP//n71++Opz+36+Nd+Pnd8buX1bv759Pp9buHh+Pqef2yPP5t/7rehS9+2h9e"
        "lqfwz8Pnh5fl4cvb67er/cvr8rR53Gw3pz8frMHAvU+GOYiMsv/0abNae/vV28t6d4o//3BYb8MR"
        "97vj8+b1eBntq8hoX/eHp9fDfrU+HsNnftmex3tZbnbXYYZOYaCXzeqwP+4/nf4WPkxyR/FQ4ceH"
        "g/hfL9v7u5fVd99/3u0Py8ft+t19OND9P0LNPe1X3vrT8m17OkZ/Hn4+JH8mf8X/C/a70/Hu63fL"
        "42qz+RhKDQd42YRjvZ/tjpv78JX18niaHTfL9It+ci16/Tl6I/OTq+MpdXm+edrcP0RCj3+FL/6+"
        "3L67t6zLlcUxf2273H2+XFvvvv31Q/pmUpcew3Hf3S8P336YRR98SJ7tIf/Er/m/YsGvy9UmlrP8"
        "dFqHfhGaJRp0uwm98N4au5c/fnmLVLt8O+0TIa+JkPSwDwWlh+4SOs+Hsw+Hr64//bBffVk/fTiF"
        "L7y7j2WFF3/9/ufDZn8I/fTd/XSaXPywftm83zw9rXfv7oeXN+6eN0/r/31e7349rp9u1/8TxL6W"
        "jLjav+1O59uPb+L45P+xWr9Gnhu+ultGNvkp+sA2evcxJSf++NvmdjfnCzmp8cX/u4gcJvZiSXle"
        "L6MYvxtWCpriCLKY40oNYasP4agPMVIfwlUfYqw+xER9iCl8iNN+dXa+9MftacUnCl5U+YmC01R+"
        "ouAjlZ8ouETlJwoeUPmJgsErP1Gwb+UnCuYs/cRqGf9d+MxI2Ac+bk7bdWUCGiqmuiTt3/28PCw/"
        "H5avz3fR3FqQUjLCh7fHk9itDtVu9cPpsN99rhRjWWpi/JfX5+Vxc6wWpKj6jxHwufvXYfNUKWrE"
        "mWf4g/+8Xa7Wz/vt0/pw93H9x0n28z/t7z6cUUa1XdXU8MPm8/Pp7sNznDQrhbkcpVeN/8PmeKoe"
        "nPMoVYML2dDl+CV/8B/XT5u3l4tqBNCIayuKsKpFOEARkQFEHmGkMr7A/bvA8SMbi9z/WGV8gfuf"
        "qIxvV48vnWm8kLeKhddYOnYX++3+8OltK5wextIRfBUh9gjSQXwdXyhJjKUjOJM+72arVcjcRPxU"
        "IY9KSFFIqBJSlDOrhCzlFCshSy3XSgiSTrq/rH/fHC/4Vsq8xxTWrLwxm6MBUWzxn7f9qRqYWoos"
        "/vvdab07ru/EpNmKsDEz30nYWG3ikxCkNgNKCFKbCiUEwedEcSHqk6OELLVZUkKQ2nQpIQhn3hTA"
        "XwjzpoAUhHlTQAravCkgC23erJ2jSAhSIysSgnCSt4AgnORdO4+REKSevKuF4CVvAVk4yVtAEE7y"
        "FhCEk7wFyC1C8haQgpC8BaSgJW8BWWjJW0AWTvIWEISTvAUE4SRvAUE4yVtAEE7yrrUaJS4EL3kL"
        "yMJJ3gKCcJK3gCCc5O00krwFpCAkbwEpaMlbQBZa8haQhZO8BQThJG8BQTjJW0AQTvIWEISTvAUE"
        "qSfvaiF4yVtAFk7yFhCEk7wFBOEk71EjyVtACkLyFpCClrwFZKElbwFZOMlbQBBO8hYQhJO8BQTh"
        "JG8BQTjJW0CQevKuFoKXvAVk4SRvAUE4yVtAEE7ydhtJ3gJSEJK3gBS05C0gCy15C8jCSd4CgnCS"
        "t4AgnOQtIAgneQsIwkneAoLUk3e1ELzkLSALJ3kLCMJJ3gKCpHNDtM52u74TXp46RFrVIL4eVnV9"
        "7/kBf1l/Wh/Wu5XASgpFgZcnlJCouLZ4vt9/uRNb2G1zHERY1OZxu9nHy2z+LIw9LluW/O/F3fv1"
        "dbldbsV7QfzD18x2oWjYePNb+MbTn6/heK/p1T5P5+XmyaLh+I3fP1239UQfjm7iLtlAlVyO7zWR"
        "Gv/7cAxDLXnPYBAs3KkdJPcSD1lxE1ex0WOuDwWxz+fLsajHZaj3f+9Yd7Td7L5crp9HWjwvk4/d"
        "tHZ5xzTZLZC1KONxfHc4mQfnNyf7vU7Lx2Py/8v7ojQT3mP45+v++O7ecSdJ7ki95xDho+tbprY7"
        "SJR0Ga+wjyx2r2QXmXP9g7uLjKPsVaiG5Sq5vdXb8bR/iZ0jb/WU0vImOL90d1Nozg7JtoXrSrJ4"
        "0wLHKlUW4alf1puC/f7E8KZP58sy3nQeibxJyptSSsub4PySqjcFKUPW701JCh4ys9N5O0CVS+3W"
        "f5xEElckptTZxDPw1cm+rNevP4XyHy5//BCa/viQ9ZPH9af9IdSAM4m94+o28dv2b6fIXX74fXsV"
        "lHaYis3Ay/+WbAaOXuRuBs588rYZOLp82wz8eP7v4vxEqwgDXu7SdkfBNHbN+KMxPgz9PQaGt8sR"
        "BI5m6URrqc3Fk8uV1ObiSfLkh/JQKfUki+tJFqYnWQKexMha9TlXsje6yrmGRjiXE0yGc4/nXHlX"
        "chmu5CK4ks11JRvTlWxDXcnqhispOonDdRIH00kcASe5ES1tfcbW1Wc25/+24UEjrgeNMD1o1A0P"
        "cvTxoIyXWI4dnL9BEMBD4wDBb1yu37iYfuN2w29G+vhNSa5p3ovGXC8aY3rRuBte5BrhRc4g+s17"
        "0SnUxc2HPm6iLkRzDBeacF1ogulCk2640FgfF1LgXAMG5xog+NKU60tTTF+adsOXJvr4EmI6wnK0"
        "TEmV85UMsyaad0FO9yCO+wzF3Id/36eoY07JPccddUq/S7qL31JVw6128NPjNimmP26/30X+/TWp"
        "d5/v9OmP5f3ljYv1dvvj8vzu/Sv/rdv1p9P51eFgwnj9cX867V/4n48L9PwBHrI383B9CL6+d28v"
        "j+tD8kUg96u7uHFGUd3nhhqKmpZNlj/tL12LGDd0eancPaVylwbfoF2r9/knfn/5ogDja7T4q4jy"
        "aYGvLH2qGbpU6iUNbJUa2EIysNU1AzdWLZc0p11qThvJnHbvzAmF2OcVOXl7nK9iYOt4pDJgPRwA"
        "5p7X+dMhgwvit0aNmpPlRX9FOPjuPElF37LGaj8rTUSVl/ELc5w9EJnlIlm7CMu+LbfJzKsNJs+4"
        "1XAcTgQFXUR3bnEngatKbiW0iLscri5ymxyub2J0jR5ZuPnl5mhMZ1ZNLKmI4PuwnmnFNIuzE9W1"
        "12revNcXMNLVZbDSjAVByyGfOP9jsy1+8Z68qEeCUPnWq+Asw1EBazgMrOHg5oKMFXn+opoRsn7H"
        "dxM9k4LGVmbHf0Spb93z8kbNNderSgVFa9kOIKg3cfkjKl5Ey+cH1VO/7EPP909/xi2M888bvXBu"
        "blz1qGmXvQyHsrxyNht6E6+8JDC0MgvX1CM78wRcpaiG9lXtFTriKQRq5uI6tdsjVa9UYz1B+ZI0"
        "bFNfkXGyqrHO+k/2CUv0huUM/BJBTd5QXGp2e6rqxWasRyhfVVZj4F/nutsMMWTUHIbINYfsc5do"
        "E8tH+HWHCh9BVhB/CmXOnID5UsVb0tOmfV7Z8LzcfY6OlrpP1tbjTqPRMxZza9I2vcZnty03mA5K"
        "IUMjz17MJPGzVyeR+p59OJg09PDzt+12zfb7u+S1ZtVwpYLhP76/vjXHBevSAycMzi82Hg1sVVjN"
        "qIITFYkqmg4OtirsulXxU/w9J1sTyWs66GHUjB440XF+sd7osKauPfUEVOE2owpOdCSqqDU6hFUx"
        "rlsVi3C8ze6tWHSMdXF9tVld8MB2EVjVMp9enpoTK5eXG48WMbXUUqZJq4UTN1e1NB05YmqJ4Ri6"
        "Xn5crg57Zv3qJXqlyKOuH0AhqgxtMDYARwqI7jre2zsaJ6yL94bh8PLVBvcd48vXIbx3WPbAqXjH"
        "hLELOfMO2xlV3KkTzq5Jfjw/dcXXC1E/j7fD5kyq44Ly7UpCRK8ADWsBXgl3z7pC3n/iV1FqfTcf"
        "laLuad9qU53swDsfx5JX2vlqVfoR+ZosHqksRi2pjdOJAku+kxjEP+zloqhud3sypvZUvS1lAr7S"
        "jFBUZg9iXleX9TwO0noehxucSeRk11Lq+ZXb4/m/jWwulLTiqNSKIyQrjrpgxfq3Zknazi21nYtk"
        "O7cLtmt6k52kJcellhwjWXLccUvib3STNOOk1IwTJDNOumDGdjabSdpzWmrPKZI9p12wp4YbvtgE"
        "aZGcUZ+36uXseihJYiwsGjHtp7pz8FbWEdxxc3WMLAyFR+CQsQdkCNkDclu2dz7lPm+T5LJchDHY"
        "lQWgpGllQR/r2kU0/2DXF5QfTWoNPYNEQsMoaSPKLjdkz4bFKDukxZVVH+y69hQ4qHsK2Bt7rQmj"
        "Pju147668T7H81/Voa0HwyzYrNRNVCfTjENWeIdU8DeqzsxC5u2am0DyfZFV88gQuWo3GUySZR5V"
        "cz6IUeV9jKunQj9n5YQrtQVAM3e69Xzm+NPtDap6siF6Ooa5fxsCNIZmFoPRwOFo5rI+M5e51d2K"
        "r69iF21lhamCFFX1sTf7ICo16gPO3nSY6hCurEYbWY0stYC3XP57cWkynldBugE5SwfZ7egSJKSe"
        "9iXF5iPTNC4R6GZxU0p0JTpHoKiT6JX4iAGmStKNLzhPP6r8XgWjpYFcZ4z5/vC0Ppy/i447Y1Sg"
        "zUEKbd62mSZ9M0CfFcW57E9fOm6APrzZhZZYv1f7+G+wjz8U1G9ym5JiIMUnAyWnjDCWoqROQYKG"
        "k1uJn3HC6XBZ0yX49eblYmb76sN1nHR0xkzll/3X+XL39GHz11U/w2t8xu8Ih+e/AyPCJxxnrfgW"
        "V3zju8SgJgbGzVQ/H64f+rQ5HE+hce+Zrngh3dleWgC/ZJWGkhs7u8AqubKq1RPSU8Bus63NPXIp"
        "/yoql8tz13/LXX/I6OPhoqWHtCE5Zt0uyards2ocrOFd3VfYQNRDkIZ6DNP+8Lf14bxyscL8TGPh"
        "6zW07/N1wl1t18tDHt6Ef37abGOiF/1erR7EF7OzZHTtXHu5HiAkbrVYPe/3h796rx4oNPt2lpRz"
        "SiHa5VA19oEnmmM1QI8xM9GawNdmkNQtUgYkxKbd3C7gDWizu4AsQm1kWUJuxkATz/YC389Bk/yc"
        "2WfshqogRfTG2gLHQG/snXCao7epY7u2w/uuqEPoTeBLMUgCrxyW0JuOc7yAN6DN8QKyCL2RZQm9"
        "GQNO/CCEJ7fZMQ1Oslf7it5QFaSI3lg79Rnojb1hX3P0Nnanlr1gJyC7S+htOp/PR1Peg4ITeOWw"
        "hN50nOMFvAFtjheQReiNLEvozRxw4vq+N2KCEztztbfoDVNBiuiteMY2E72xD9zWHL2NAmc6nrET"
        "0K0k1wH0Nhm4zsziPSg4gVcOS+hNxzlewBvQ5ngBWYTeyLKE3owBJ17gTfwJE5w4mat9RW+oClJE"
        "byMx9DYyEb3Zw4kznbMT0A08dwC9OfPZYuHyHhScwCuHJfSm4xwv4A14q6OqZRF6I8sSejMHnFj+"
        "LMgu4CrOmb1Gb5gKUkRvrhh6c01Eb77tLgac2tstL3UAvQXjqetwMq0LT+CVwxJ603GOF/AGtDle"
        "QBahN7IsoTdjwEng+Y6X31CZnzP7jN5QFSSN3jgHP0b64B7/KALTKk+4xu+rozuqktrZr28zkNIG"
        "P9RgpHHglyMpQfyT1/TjcvXl82H/FmZKBi3JpEvhxJWzaXqrvGwKNwNUPe3fHm+u7lKYQ8K8x+CM"
        "ZgwtXEkKL5LNmrYZCMJW9EyJz1lUbphCmFa174HeTVNAPk+tWLqIbXNWzbYS6DO6pYAXCnhCuTSH"
        "6OFS9aJdsh2S7VRQL6/XTBr1whvNMFHvYj5wXaevqFeyX4TezWZAXk8tbLqIenNWzbZg6DPqpYAX"
        "CnhCvTSH6OFS9aJesh2S7VRQL69HTxr1whv0EOpV7bOhd5MekNdT658uot6cVbOtK/qMeinghQKe"
        "UC/NIXq4VL2ol2yHZDsV1MvrbZRGvfDGRoR6VfuT6N3cCOT11DKpi6g3Z9Vsy48+o14KeKGAJ9RL"
        "c4geLlUv6iXbIdlOBfXyekKlUS+8IRShXtW+Lno3hYKt66FWUx1EvTmrZlul9Bn1UsALBTyhXppD"
        "9HCpmtf1ku1wbKeCenm9tNKoF95Ii1Cvaj8cvZtpgbyeWnR1EfXmrJptMdNn1EsBLxTwhHppDtHD"
        "pepFvWQ7JNtJo95/HTZPHLQbvwQFuZcVzgRyqUGJyJi5nn+oo/6GOioBcTlAeQj2u9MxGuS42mw+"
        "Rip9d/+y/O/+8H4WmicaZR1ijNlxs0y/6CfXotefozcyP7k6nlKX55unTaJIRRRrZkQPdQ5pXhvP"
        "trtSNUOrjIwC6rvXlyBgMUZtXBZIU7W5/+5PPBqFnCrHpZ6SbdtMor66GES/13HTnXDT15rpcE4e"
        "YQJ109bPLPKzbvoZaq2uot9q9Bb1fqtUvKN+a5BRQUU84XElY5T6w1IJw+To5hbzdAlvtVpGna03"
        "qaRHzYYpHLLzg67FMSruUfA1GQzUUlsr20kUYTzbC3z/OnL2aID0VU3LfeQbrVI9jX2uvtIf+Zwm"
        "PldHEZDXfj5dBIS3n6ciYMHm1H5WYFRQEVB4XMkopXb5VPQwObq5RUBdwlut6lFnJ3IqAtLZCxQO"
        "2flB1yIaFQEp+JoMBjphRCvbSRRk/MCzPXb3zOxVTYuA5ButUj2Nfa6+IiD5nCY+V0cRkHcaT7oI"
        "CD+Nh4qABZtTN36BUUFFQOFxJaOUTg+ioofJ0c0tAuoS3mpVjzoPZqEioGIRUMd4oHCgIqCG99+P"
        "yUiz4NO2CEi2k7SdTEHG9X1vdB05e3Bk+qqmRUDyjVapnsY+V18RkHxOE5+rowjIO5wwXQSEH05I"
        "RcCCzelwIoFRQUVA4XElo5QOU6Sih8nRzS0C6hLealWPOs+poyKgYhFQx3igcKAioIb334/JSLPg"
        "07YISLaTtJ1EQcYLvIk/uY6cPUc7fVXTIiD5RqtUT2Ofq68ISD6nic/VUQTkndWcLgLCz2qmImBx"
        "Czid1Vg9KqwnoOi4spv26WxpKnoYHN38noCahLdiE7Qaj+2lIqBqT0AN44HCgYqAGt5/PyYjzYJP"
        "2yIg2U7SdjIFGcufBdlObLeB01c1LQKSb7RK9TT2uRp7ApLP6eFzdRQBXYEi4OXwYyoCIhQB6ehq"
        "gVFBRUDhcSWjVOiobSoCdrzoYW50c4uAuoS3WtVDKDypCNhOEVDHeKBwoCKghvffj8lIs+DTtghI"
        "tpO0nURBJvB8xxtcR04XZNzMVU2LgOQbrVI9jX2uviIg+ZwmPodRBPxx/bR5e/nwvHwK77B4NPD5"
        "5bvkdYVzgS97r6n8dyv5DqLfvLWzR4OfU8A8ANfWpWWASu3SUiCVd2khsPWDkmKo4idX4UjznUTp"
        "FwMF8U9e9Y/L1ZfPh/1bCKPu610oQfHYaDzyihvJdTC+GsQ/OXx1vi9ZINVM0a+hRXjk3vq6t3Lt"
        "DbEMBh2qqgoiHMCLQfTLDOD0tWYoeUNJq4lnFqaEdXgynJQkyxOqycllkQKxFESWMp7PBgvuYZVY"
        "EwdECmTqgMgBTB4QMSC2Ii+I+EpX+ApFZjuRWRcEyB0LnD0wus/MhRzdAEcnBtP42e+6cRjNTrzX"
        "ksUUD17nsRj48evEYgrZcBGM52NOo10LbQqBSAGddAaQAzn6DCAGdoS7tCBiMV1hMRSZ7URmfYXM"
        "zLmG2RMv+8xiyNENcHRiMfoemNxQAtPsyF4tWUzx5Fgei4GfH0ssppAN5/ZiMeF0CrTRphCIFMgU"
        "ApEDmEIgYkAsRl4QsZiusBiKzHYisy4QkDuYKXtkV59ZDDm6AY5OLEbfEx+bYjF6nTmoJYspHn3H"
        "YzHwA/CIxRSy4TSYzOacmo6DNoVApIAOnATIgZxACRADYjHygojFdIXFUGS2E5l1gYDcyRLZM0f6"
        "zGLI0Q1wdGIx+h5Z1dSKMr0OTdKSxRTP7uGxGPgJPsRiiutrJ4uB57Cz4QhtCoFIAS1KBsiBLEoG"
        "iIHti5EWRCymKyyGIrOdyKxtX0y2NXa2aXqfWQw5ugGOTixG3zM3mmIxep36oCWLKR4+wGMx8CMI"
        "iMUUO85N54MxJxu6aFMIRAqo2x9ADqT9H0AM7BgDaUHEYrrCYigy24nMukBArrdntutrn1kMOboB"
        "jk4sRt+m4U0lML3aVmvFYip39cM38zv9JS3c04outw887Cj19ASWjQLLAh6RhgbXpKDkJrkJOpdp"
        "qJ1t3k0yjpF6V034scOmz0XV2fTFoMJDZk1F9VVhnTMZcrS2ZSumXbqiWKnjmvTThDeJfkuyQvqV"
        "CICuo8+gcxDd7ne3juCS0ok3nYEXcor7elUcawYnaNc2uRRtgG2pN8Amtklsk9hm11KSzGIr85oQ"
        "E9/EMj7xTeNMhh6vxDhrUS1xTuKc3QYZxDn1070y56z+YlO9XTlxTuKcxDm7lpIkJmsDW0YT58Qy"
        "PnFO40yGHq/EOWtRLXFO4pzdBhnEOfXTvTLnrGwub6k3lyfOSZyTOGfXUpLEZG1gg2/inFjGJ85p"
        "nMnQ45U4Zy2qJc5JnLPbIIM4p366V+aclUcBWOpHARDnJM5JnLNrKUlisjawHTtxTizjE+c0zmTo"
        "8UqcsxbVEuckztltkEGcUz/dK3POyoMbLPWDG4hzEuckztm1lCSzicm85vnEObGMT5zTOJOhxytx"
        "zlpUS5yTOGe3QQZxTv10r8w5K4/ZsNSP2SDOSZyTOGfXUpIM7TDvqAPinGjGJ85pnMmw45U4Zy2q"
        "Jc5JnLPbIIM4p366l+ecP2yO/Ga10YsKDWpHzZBLlkPlOpAnDpVuQZ52Jc2YKc+jKh4KdghWtaY6"
        "yGIT4x+C/e50jPzuuNpsPkbP/+7+Zfnf/eH9LIzCSOI6hEiz42aZftFPrkWvP0dvZH5ydTylLs83"
        "TxtlNFuTfYE5Pc32qtHjMHCmY491HxZOctctasw5kK33Okc7bW4xiH5zMPR8h+lrtZ0118aNAjFH"
        "VaP8M/ZQ75JPIAQXhOQ67SYPlWq1CwvuymEJiBgORIQs3G0o0mbs9BmOGKh3NEji2V7g+8yqpm6g"
        "BPVW1WAJt5dyFpbAGykTLMGFJblmjJlYtOAhXjkswRLDYYmQhbsNS9qMnT7DEgP1jgZL/CCc7dmb"
        "SrNX24clqLeqBku47TazsATea5NgCS4syfXrysSiDQ/xymEJlhgOS4Qs3G1Y0mbs9BmWGKh3PFji"
        "+r43Ys71tm6wBPNW1WAJtyNbFpbA27ERLMGFJbmWLplYdOAhXjkswRLDYYmQhbsNS9qMnT7DEgP1"
        "jvclTuBN/Pzy5ss96gVLUG9VDZZwm/ZkYQm8Yw/BEuS1Jdld/5lYHMFDvHJYgiWGwxIhC3cblrQZ"
        "O32GJQbqHQ+WWP4syC7NuN2jZrAE81bVYAm3r0MWlsCbOhAswYUluY2hmVh04SFeOSzBEsNhiZCF"
        "uw1L2oydPsMSA/WOBksCz3e8/OaWyz3qBUtQbxUGS8qXusJXuLqNopAWppM+QJ/KjXzp/fL67g3M"
        "7cGnndFV6Oz410VXVhKtx78Wx+w1JTgl3WfBclBMb3yLpTTuwwYNUtHOsZoe/Wvq7WxVj7+zNYdk"
        "OVP1ngbQimqvZXrqqLsb3r6q7X34ktWErqkn1e1JhRthO/Wx7LYqTCbGa4EMTKgXgqXeC4EoWQco"
        "mcBmZvlZr60d0iCw0+9eEQZRM1nzGw+b6iRnknFP9EwjeiZgO1M13zhBk56qOuryhlO09vuSaE7S"
        "6lcQ0TQQTav4wky9NwzRtA7QNIHmDvJzX1sdI0Cgp9+9cwyiabLmNx461UnTJOOeaJpGNE3AdqZq"
        "vnGaJj1VddTlDadp7fdp0pym1a8gomkgmlbeK8tS75VFNK0DNE2g2Y383NdWBx0Q6Ol3LzGDaJqs"
        "+Y2HTnXSNMm4J5qmEU0TsJ2pmm+cpklPVR11edNpWut963SnabUriGgaiKaV9w601HsHEk3rAE0T"
        "aP4lP/e11VEMBHr63VvRIJoma37joVOdNE0y7ommaUTTBGxnquYbp2nSU1VHXd5wmtZ+H0/NaVr9"
        "CiKaBqJp5b1ULfVeqkTTOkDTBJohAhb8t9RhEbbTo9e9Zg2iabLmNx461bo3TS7uiaZpRNMEbGeq"
        "5pvfmyY7VXXU5U2naa33NdadptWuIKJpIJpW3lvaUu8tTTStAzRNoDms/NzXVsdZEOjpd+9tg2ia"
        "rPmNh0510jTJuCeaphFNE7CdqZpvnKZJT1UddXnDaVr7fd41p2n1K4homjBN+9dh88Tt8Bi9qNDY"
        "cdwMKzOJ4ziD6JdN3C4Xz24/D8DlPmkZoC+qpKVAqsDSQnI5rF4xv9UrxlC2J5tnVfr+CpPLx/NT"
        "g47KERgKzoqGmN5iztlCGEBQ2MMmg+hX0MPGLR68g3ijQChQ1fT5DAnUmz4TNigE/Hg+Gyy4LSSx"
        "0AFECgQfQOQAEAJEDAgjwAVJogR5QT3BCWqtJzuMFGAeQ1iB6WWz8TzwxL2sTbSAeqtqeIHbfTSL"
        "F+DdRwkvFHuZBeP5mLNJ3mKGPahjGkAKqNsnQA6kmR5ADAgvwAVJ4gV5QT3BC2o90DqMF2AeQ3iB"
        "szdoNp65wl7WJl5AvVU1vMBtg5fFC/A2eIQXCmE/txeLCWe3ps0MewhegEiB4AWIHABegIgB4QW4"
        "IEm8IC+oL3hBqRlPh/ECzGMIL7C/7fI8b7YQ9rI28QLqrarhBW4/pixegPdjIrxQbMIXTGZzDk1w"
        "mGEPavUHkAJqUwuQA+kCCRADwgtwQZJ4QV5QT/CCWleIDuMFmMcQXmB62TyYDzmrJVle1iZeQL1V"
        "NbzAbQySxQvwxiCEF4pfQ04WA89hh/2IGfag9QsAKaD1CwA5kPULADGw9QtgQbLrF6QF9QUvKG1P"
        "7jBegHkM4QX2ooCRN/LZ33qxvKzV9QuYt6qGF7g71LN4Ab5DnfBCcb/bdD4Yc8LeZYY9aFcdQApo"
        "RzhADmTDJUAMCC/ABUniBXlBPcELavvkOowXYB5DeIHtZfPFbMaehFle1iZeQL1VGF4oX+cIX944"
        "aQYeUAObOgFNxUNB0EvlkBCoUjkoAJdUjgkCIYKjSiKOaufrA7xofNulUgqAzRi+G/0KPuNwKj21"
        "VWAgvCcWREwWRmbqcYOdVmyo3qvHeCOwgPFV4/cXa+SukF0KKT3+EU3ptrSZOrMhO6PeUmTi1oJM"
        "gKOCHaNufbffcAdI54S2u1vq292J33WA3znBZDjn7rMFMjyBQUHteaqHhfTjqR4V1oBHdFzZjjtV"
        "4/aE67Wwdb4NtucFVsBekEd8j/ge8T1tjEB8D8Uu3twfcVYU6cb42m+roc75VFEKeFywg9SvddOZ"
        "X8UXeuqNS4j5dYD5LQajgcOJUAvK/AQGBTVSqR4W0jelelRYmxTRcWW7olSN2xPm10ITlBaYXzDx"
        "Pd8TfkpifgaAW2J+XTQCMT8cu1je3JuLp/UWmV/7DZLUmZ8qSgGPCy8N1K5105lfeQsqS70FFTG/"
        "DjC/6Xw+H3H2sttQ5icwKKjFRfWwkI4W1aPCGliIjivbr6Jq3L4wv+bbWbXB/EYh92NXOFlPSczP"
        "BHBLzK+DRiDmh2KXqIeAxy51MdN6i8yv/VZ36sxPFaWAx4UvAq5d66Yzv/JmgpZ6M0Fifh1gfpOB"
        "66R2m2Yi1IEyP4FBIcxPYFgA8xMYFcT8hMeVZH6V4/aE+bXQmLAN5mf5QcCucLKekpifAeCWmF8X"
        "jUDMD4f5jbzAZwN7Zlpvkfm137RUnfmpohTwuGAHqV/rpjO/8rawlnpbWGJ+HWB+zny2WLjsCB1B"
        "mZ/AoKB9ftXDQvb5VY8K2+cnOq7sPr+qcfvC/JpvMdvOPj83mAo/JTE/A8AtMb8uGoGYH4pdvJnv"
        "B7Z4Wm9zn1/r7acR9vkpohTwuPB9frVr3XTmV97g21Jv8E3MrwPMLxhPXYcToS6U+QkMCmo4Xj0s"
        "pL949aiwduKi48p2D68atyfMr4Vm4W185+cHDqcEznpKYn4GgFtifl00AjE/HLt4/tRjl7qYab1F"
        "5tf+QQLqzE8VpYDHhTtI7Vo3lfmV7++Db+ubNkP0jKJNWeMm7p2zLog6iQ0Mok9iQ0MolNjIsAQl"
        "M7ZskhIZuyd0qoXDETY5MLQph0ei1lIkICaGvOUYEvM81IgoEwwscvA7HQHonFpP11d1I5ruyPWr"
        "axGt+n5tLlriR6ph1RDzRs5/+vpAVX0E13o6FlkQTV1VTeku6DJ94lF1Iq2OtCHPap3Bo4ytFSxC"
        "9HBgRU/otB5b/bQeKvFRgqASX8dLfK2ciaML0jc/6KnIhzCl506eyMYAlfn09X5TprzeOD8V+kwt"
        "9KHnQH29gEp9qMamYp+p04+qG2l2mhn5VutsvnvlPlQfVyv4lR/SZqsf0kYFP0oRVPDreMGvlaPQ"
        "dMH75gc9FfwQJvXcgUPZGKCCn77eb8qU1xvnp4KfqQU/9ByorxdQwQ/V2FTwM3X6Ue7ApNchluRb"
        "rbP57hX8UH1creBXsXdX/WxOKvhRiqCCX9cLfm2cgKkL3jc/6KnghzCp586Zy8YAFfz09X5Tprze"
        "OD8V/Ewt+KHnQH29gAp+qMamgp+p049y3Vivs4vJt1pn890r+KH6uFrBr/xIZlv9SGYq+FGKoIJf"
        "xwt+rRx8rAveNz/oqeCH0qUjc7xoNgao4Kev95sy5fXG+angZ2rBDz0H6usFVPBDNTYV/EydflTd"
        "SLMj68m3Wmfz3Sv4ofq4WsFvJFbwu5yMTgU/KvhpmCKo4Md4vevn3euC980Peir4YbQxy54qnY0B"
        "Kvjp6/2mTHm9cX4q+Jla8EPPgfp6ARX8UI1NBT9Tpx/lHn4jb+Sz68Ys7kAFvw76VtcLfqg+rlbw"
        "c8UKfi4V/Kjgp2+KoIIf4/UGC36B5zucbzBYx51TwU+voKeCH8KkHoynrsPmPy4V/DT2flOmvN44"
        "PxX8TC34oedAfb2ACn6oxqaCn6nTj7IbzRczzkJRFneggl8HfavrBT9UH5cp+HnLw5cfNsdTocoX"
        "vXAXvwIs7I0HzRT2ktlYeSZvsDbYwQLPIP7JOfD5kGnlSg4a5LpeLEuJQ8yc2DTkEjSDbIUBOiWq"
        "6lLAeHpA3RK9p699eF4+rUEQJUN56wkDtiZxDaqlOeby5khTT1QeqKpg84MDYA0oN1SPjm6qEkCF"
        "+q1KCOJOvl8f8pH35bv1S2gTBCcIjnFGLoFwAuFdBOGWYwcue5EBwfA2YLjtjoIpe5sXAfEWAqQB"
        "e/QHijelzF6AcVxlKsDx4pHVBTgOPq6a4Hif4LjwCXYExwmOdxGOu5blWDYnAAiOt3CegmO7tiNu"
        "EILjxtujP3C8KWX2Ao7jKlMBjhcPlCzAcfBhkgTH+wTHhc+XIThOcLyLcNzx3aHFbrJvs3I6wfGa"
        "DTJ2p5YtcowLwfGu2KM/cLwpZfYCjuMqUwGOF497KsBx8FFPBMf7BMeFu78THCc43kU4bgf2cMT+"
        "xtNh5XSC4zUbZBQ40/FM3CAEx423R3/geFPK7AUcx1WmAhwvHsZQgOPggxgIjvcJjgv3ZiU4TnC8"
        "i3DcGowm7pgTAATHW1g7Ppw407m4QQiOG2+P/sDxppTZCziOq0wFOF5slVyA4+A2yQTH+wTHhTun"
        "ERwnON5FOD4dO+MBLwAIjjcPx33bXQzYNS+mQQiOG2+P/sDxppTZCziOq0wZOB7H76e3eOAwARTQ"
        "+OX1u8sboFj8gkxawOI5QJKkrDQiaQmFK3V/z+2VTJ4qtVmyLL3zBq1QVTlqBQ9aMtWDxyxthorS"
        "+JvTDFVp7IeCX3SSq/lu9MukCOlr58atw6lp9E0lZtvtPp710bNdWP16IQSO2W5euWgCYITKhw81"
        "0DttOpXWNeuEh2bUWx/gUp8MLhczem25MR7AuIxzG0y3bQv1J2koDSJ54hWb+EdwGnSB5z+UECiJ"
        "lcfRr+CNAupKu3WEK7jOLQjghSR9rUGSAuHidrTMEy/1xpbEwExgYLmOlJlBFTiYwLAAFiYwKvEw"
        "nXmYF1gBe38rMTFiYh1lYtbCWYzZnTqIi6lysZxyc1PC5XK9bKwBAxMf08kaaIxsPlksfJF7bZ+T"
        "zcbzwPOFb5VYmTwrKzY25bEyeH9TYmUmsDKBQSGsTBaFoo1KrExjVhZMfM/ndcEtZnZiZcTKOsDK"
        "xmNrYbHXwDD7JxIrk0ivOeXmAutyuV5W1oCBiZXpZA00VuaP5pM5e6Mha0Jsk5V5wWw8Yy/CZt0q"
        "sTJ5Vlbsb8tjZfA2t8TKkFlZrndVZgJyoKws1582M6jNSr1owwJYmcCoxMp0ZmWjkJex623ZhoKd"
        "Y2UCsUusrKOsbOSPRzb7fEBmG01iZRLpNafc3JRwuVwvK2vAwMTKdLIGGivzXN+ei3TYbZ+VLTzP"
        "m4nfajkrU2cwxZbAPAYD7wxMDAaZwQjgd3kGIwCtIAxGFrGhjUoMRmcGY/lBwK5NZZc8dI7ByDJ6"
        "YjDdYTDOwp67vK7pOJCqvwwmp9zclHC5XC+DacDAxGB0sgYag1ksFgOPfb4Za0Jsk8HMg/nQYxND"
        "1q3S90ryrKzYGZrHyuANoomVIbOyXNe3zATkQllZrrNzZtARK/WiDQvZg1U9KrEyjVmZ7wVuwJ6E"
        "sq04O8fKBGKXWFlHWZk1dmdjdkWW2YCWWJlEes0pNzclXC7XvAerfgMTK9PJGnh7sFzP89mbklkT"
        "Yqt7sEbeyGeTXdatEiuTZ2XFBuE8VgbvE06sDJmVCXASeVYmABchrEwWhaKNSqxMY1YW+IHjsyfM"
        "7DdonWNlslUKYmXdYWVzd+QO2NCL2YeYWJlEes0pNzclXC7Xy8oaMDCxMp2sgcbKgrnnzNmdMVgT"
        "YpusLJgvZpxz0lm3SqxMhJVFJzLxqVj8KpR+XXZ2E/3q9AFNjTf9rnXiKT2+yWoFvU19e2azpxPm"
        "lt7FomasnLuhDOIp7Dq/3o0icuYqvzrWNSEkLIjMIopANAYdqj9n2ywG0a9gprLxD7aRWcAU/oje"
        "qF12o1BMUN3AOH2WI7x7MYGEfoCE5jvSEkwgmEAwgWCCtEU92ws4HQF0Awre3B8FQ/FbrRMqlHTV"
        "TEMFeEtNggq9gAottEkkqEBQgaACQQX5A16DECywv5Jg5ao2oUJgeXNvLn6rdUKFklZvaagA7/NG"
        "UKEfUKH53l19gwphxPgTiX2ftUOF3A1loEJhazJBBYIKukAF1/e9kXCuahMq+LNg6LEZGPNW64QK"
        "JT2V0lAB3lCJoEI/oELzTXL6BhXG/nThSDS5qx0q5G4oAxUKfRgJKhBU0AQqeIE34eyUY+WqVqHC"
        "yAs4+ymYt1onVChp9JGGCvAuHwQVegEVWujc0DeoEFhje8A+pYS5Qr52qJC7ofJNHAQVCCroAhWs"
        "iKwL56pW1yrMfD+wxW+1TqhQsvs8DRXgW88JKvQCKrSwnbhvUMF2Jt6MXTdltjipHSrkbigDFQpd"
        "eAgqEFTQBCoEnu9wWo2yclWraxU8f8pp4Mq8VXSo8K/D5okPEeJXocjAJmRQ1pSmvu4pD3lR3YQk"
        "KnuHQICkbHITX5EY/wjeNWATutwULxcoej5x/Q05hB81p07eoyaQaS4/8dTem0KfR0Vr/DAZRL+C"
        "/gfopYAGBhBvFAoFqjdDRu9S3wxJ2ICwQZ3YQG27UHvoYD5ZLHx2k5rO4oP6n1kjhGC7o2Aq4phd"
        "wAgNPCwaSpiN5yEZF/bCNnEC6q0qIoWSvZBppADfC0lIgZBCnUhBbbdQe0jBH80n87HwfXcCKdT/"
        "zBohhaljuzYbFjG3rhqNFBp4WLxjo4PZeMZeYM3ywjaRAuqtKiKFkq2QaaQA3wpJSIGQQp1IQW2z"
        "UHtIof5j7vVDCvU/s0ZIYexOLVvkYbuAFBp4WLzjWT3Pm4l7YZtIAfVWFZFCyU7INFKA74QkpEBI"
        "oVakoLRXqD2kUP9x0vohhfqfWSOkMAqc6Zi9G4XZ48JopNDAw+IdGVj76eh6HuSuiBRKNkKmkQJ8"
        "IyQhBUIKdSIFta1C7SGF+o841Q8p1P/MGiEFezhxpuyvxZibUYxGCg08LN46hdpP7NXzcGFFpFCy"
        "DzKNFOD7IAkpEFKoEymo7RRqDynUf+yefkih/mfWCCn4truQ6XBhNFJo4GERD7ys+xRJPQ+8vCGF"
        "y7+O//h/UEsDBBQAAAAIAFyg1FxgeYLTOTUAAHOvBgAaAAAAd29yZC9zdHlsZXNXaXRoRWZmZWN0"
        "cy54bWztfV2Xo0ay7fv5FbXqxU+elgAhyct9zhICxl7L4/GZ9vg+q6vUXZqukupKKrftX39An4AS"
        "yI9IyITtfpgpQBmQuTNzxw6I+P5//nh5vvt9ud2tNuv33wz/Nvjmbrl+2Dyu1p/ff/PvX+NvJ9/c"
        "7faL9ePiebNevv/mz+Xum//57//6/ut3u/2fz8vdXfL79e67r68P7++f9vvX79692z08LV8Wu7+9"
        "rB62m93m0/5vD5uXd5tPn1YPy3dfN9vHd85gODj8v9ft5mG52yXG5ov174vd/am5lw1fay+Lh/P/"
        "dQaDSfL3an1p4/aONq/LdXLy02b7stgnf24/J7/Yfnl7/TZp83WxX31cPa/2f6Zt+Zdmfn9//7Zd"
        "f3dq49vLfaS/+S65ge9+f3k+X7ypuvZ4o6f/Of9iy3OTx5+Em4e3l+V6f7i9d9vlc3LDm/XuafV6"
        "7TfZ1pKTT+dGKh8487BfX4ee2qCH28XX5H+uDfLc/uPxRy/PxzuvbnE44BiRtInLL3huIW/zfCdZ"
        "8H2V65ps535W69u/bzdvr9fWVmqt/bj+cmkrWQZE2jqNUfbRdmo38+Fp8ZpMoJeH7378vN5sFx+f"
        "kztKevwuReT9f//X3V2yPD1uHsLlp8Xb836XHjkc2/6yPR07HjofPP91/DverPe7u6/fLXYPq9Wv"
        "yf0lrb+sEkM/zNa71X1yZrnY7We71SJ7MjodS88/pRcyf/mw22cOB6vH1f27nPXdX8lVvy+e3987"
        "zs2p+a705PNi/fl8crn+9t8fsveZOfQxMfn+frH99sPs2sL37zLdcPoj11GJgVdW370W+m73unhY"
        "HW5k8Wm/TNa2ZPhTq8+rFDTO2D//8a+3dMwWb/tN/i5es3eRN5keKQzq4bn3ySL24bgXJRcsP/20"
        "efiyfPywT068vz9YTw7++8dftqvNNlnc399Pp6eDH5Yvqx9Wj4/L9fv74fnC9dPqcfn/npbrf++W"
        "j9fj/xsf5v+pxYfN23p/fKBLBz3vHqM/Hpav6aKcXLJepMP8c/qr5/Qnu4yxQxtvq+stHQ8UTB8O"
        "/v+z3eG5o8pMPS0X6a59N6y1NiW05jAbF2/HJWrHI2pnRNSOT9TOmKidCVE7U8V29puHI1KzbbhT"
        "np/dQI7vZzcI4/vZDaD4fnaDH76f3cCF72c36OD72Q0Y+H52M/b1P3tYHP6++eFIDDW/rvbPy9r1"
        "bUixnJ72mbtfFtvF5+3i9eku5QU3puqa+fD2cc9300OCm/6w325S9ltjy3EIbEUvr0+L3WpXb41i"
        "OH5NWd7d37erx1p7o5L9rcbCL8+Lh+XT5vlxub37dfnHXqqRnzd3H44cqH7ACXrlp9Xnp/1dwocf"
        "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"
        "AA=="
    )
    data = base64.b64decode(_MANUAL_B64)
    from flask import make_response
    resp = make_response(data)
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    resp.headers["Content-Disposition"] = (
        'attachment; filename="DMS_User_Manual.docx"; '
        "filename*=UTF-8''DMS%E7%94%A8%E6%88%B7%E6%89%8B%E5%86%8C.docx"
    )
    return resp

NOT_SHOW_FOLDER_NAME = "Not Show in Tree"


@app.route("/api/nodes/<node_id>", methods=["DELETE"])
def delete_node(node_id):
    """Delete a tree node. Documents inside are soft-deleted to 'Not Show in Tree'."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    idx = read_index()
    tree = idx.get("tree")
    if not tree:
        return jsonify({"error": "No tree found"}), 404

    target = _find_node(tree, node_id)
    if not target:
        return jsonify({"error": "Node not found"}), 404
    if tree.get("id") == node_id:
        return jsonify({"error": "Cannot delete root node"}), 400

    # Prevent deleting the "Not Show in Tree" recycle bin node via the normal
    # delete path — the frontend handles that separately (permanent delete).
    if target.get("name") == NOT_SHOW_FOLDER_NAME:
        return jsonify({"error": "Use the document-level delete to remove items from this folder"}), 400

    # Collect all doc IDs in the entire subtree.
    def _collect_doc_ids(node):
        ids = [d["id"] for d in (node.get("documents") or []) if d.get("id")]
        for child in (node.get("children") or []):
            ids.extend(_collect_doc_ids(child))
        return ids

    doc_ids = set(_collect_doc_ids(target))

    # Remove the node from the tree first (we need old tree for path lookups).
    def _remove(node, target_id):
        children = [c for c in (node.get("children") or []) if c["id"] != target_id]
        children = [_remove(c, target_id) for c in children]
        return {**node, "children": children}

    new_tree = _remove(tree, node_id)

    # Soft-delete: move documents to "Not Show in Tree" recycle bin.
    not_show_id = None
    if doc_ids:
        # Find or create the recycle bin node.
        for child in (new_tree.get("children") or []):
            if child.get("name") == NOT_SHOW_FOLDER_NAME:
                not_show_id = child["id"]
                break
        if not_show_id is None:
            not_show_id = f"NODE-{secrets.token_hex(4).upper()}"
            recycle_node = {"id": not_show_id, "name": NOT_SHOW_FOLDER_NAME,
                            "children": [], "documents": []}
            new_tree = {**new_tree, "children": [*(new_tree.get("children") or []), recycle_node]}

        # Add docs to the recycle bin node.
        def _add_to_recycle(node):
            if node["id"] == not_show_id:
                existing = {d["id"] for d in (node.get("documents") or [])}
                new_docs = [{"id": d} for d in doc_ids if d not in existing]
                return {**node, "documents": [*(node.get("documents") or []), *new_docs]}
            return {**node, "children": [_add_to_recycle(c) for c in (node.get("children") or [])]}

        new_tree = _add_to_recycle(new_tree)

        # Update docIndex: redirect physical file ownership to the recycle bin.
        new_doc_index = [
            {**d, "originalNodeId": not_show_id} if d.get("id") in doc_ids else d
            for d in (idx.get("docIndex") or [])
        ]

        # Move physical files from deleted node's folder to recycle bin folder.
        recycle_folder = _get_node_docs_dir(not_show_id, new_tree)
        if recycle_folder:
            recycle_folder.mkdir(parents=True, exist_ok=True)
        node_folder = _get_node_docs_dir(node_id, tree)  # use OLD tree for source path
        if node_folder and node_folder != docs_dir and node_folder.exists() and recycle_folder:
            for item in list(node_folder.rglob("*")):
                if not item.is_file():
                    continue
                dest = recycle_folder / item.name
                if dest.exists():
                    continue
                try:
                    shutil.move(str(item), str(dest))
                except OSError as e:
                    print(f"[DMS] Warning: could not move {item.name} to recycle bin: {e}")
        # Remove the now-empty deleted folder.
        if node_folder and node_folder != docs_dir and node_folder.exists():
            _try_rmdir_empty(node_folder)
    else:
        new_doc_index = idx.get("docIndex") or []
        # No documents — just delete the empty physical folder.
        node_folder = _get_node_docs_dir(node_id, tree)
        if node_folder and node_folder != docs_dir and node_folder.exists():
            shutil.rmtree(node_folder, ignore_errors=True)

    idx["tree"] = new_tree
    idx["docIndex"] = new_doc_index
    write_index(idx)

    return jsonify({"ok": True, "savedDocs": len(doc_ids), "recycleBinId": not_show_id})


def _find_node(tree, node_id):
    if tree.get("id") == node_id:
        return tree
    for child in (tree.get("children") or []):
        result = _find_node(child, node_id)
        if result:
            return result
    return None


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


@app.route("/api/docs/<doc_id>/open", methods=["POST"])
def open_doc_native(doc_id):
    """Open the document with the OS default application (Preview, etc.)."""
    import subprocess, sys as _sys
    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    if not matches:
        abort(404)

    file_path = str(matches[0])
    if _sys.platform == "darwin":
        subprocess.Popen(["open", file_path])
    elif _sys.platform == "win32":
        subprocess.Popen(["start", "", file_path], shell=True)
    else:
        subprocess.Popen(["xdg-open", file_path])

    return jsonify({"ok": True})


@app.route("/api/paste-screenshot", methods=["POST"])
def paste_screenshot():
    """Accept a base64-encoded clipboard image, convert to PDF, save to node folder."""
    import base64
    from io import BytesIO

    data = request.get_json(force=True) or {}
    image_b64 = data.get("image_data", "")
    node_id = data.get("node_id", "").strip() or None
    folder_name = data.get("folder_name", "截图").strip() or "截图"

    docs_dir = get_docs_dir()
    if not docs_dir:
        return jsonify({"error": "Storage path not configured"}), 503

    try:
        img_bytes = base64.b64decode(image_b64)
    except Exception:
        return jsonify({"error": "Invalid image data"}), 400

    try:
        from PIL import Image
        img = Image.open(BytesIO(img_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        pdf_buf = BytesIO()
        img.save(pdf_buf, "PDF", resolution=150)
        pdf_bytes = pdf_buf.getvalue()
    except Exception as e:
        return jsonify({"error": f"Image to PDF conversion failed: {e}"}), 500

    doc_id = secrets.token_hex(8)
    safe_folder = re.sub(r'[/\\:*?"<>|\x00-\x1f#]', '_', folder_name).strip('. ') or "截图"

    index_data = read_index()
    tree = index_data.get("tree")
    if node_id:
        out_dir = _get_node_docs_dir(node_id, tree) or docs_dir
    else:
        out_dir = docs_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find next available sequence number for this folder prefix
    existing = list(docs_dir.rglob(f"*__{safe_folder}#*.pdf"))
    n = len(existing) + 1
    safe_name = f"{safe_folder}#{n:03d}.pdf"
    while (out_dir / f"{doc_id}__{safe_name}").exists() or any(
        p.name.endswith(f"__{safe_name}") for p in docs_dir.rglob(f"*__{safe_name}")
    ):
        n += 1
        safe_name = f"{safe_folder}#{n:03d}.pdf"

    out_path = out_dir / f"{doc_id}__{safe_name}"
    out_path.write_bytes(pdf_bytes)

    print(f"[DMS] paste-screenshot → {out_path.name} ({len(pdf_bytes)} bytes)")
    return jsonify({
        "ok": True,
        "doc_id": doc_id,
        "name": safe_name,
        "filename": out_path.name,
        "size": len(pdf_bytes),
    })


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

def _write_dms_zip(root: Path, label: str = "") -> Path:
    """Save index.json into a .dms file next to the storage root.

    The actual photo/document files stay on disk where they are — only the
    index (folder tree + document list) is exported.  This keeps the .dms
    file small and fast to create regardless of how many files are stored.

    label: optional tag inserted before the timestamp (e.g. 'autobak').
    Returns the path of the written file.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name)[:40]
    tag = f"-{label}" if label else ""
    filename = f"{safe or 'dms-project'}{tag}-{stamp}.dms"
    out_path = root / filename

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
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

    return out_path


def _auto_backup():
    """Create an autobak .dms file if a storage root is configured."""
    try:
        root = get_storage_root()
        if root and root.exists():
            out = _write_dms_zip(root, label="autobak")
            print(f"[DMS] Auto-backup saved: {out}")
    except Exception as exc:
        print(f"[DMS] Auto-backup failed: {exc}")


@app.route("/api/project/export", methods=["GET"])
def export_project():
    """Bundle index.json + docs/ into a .dms zip saved next to index.json."""
    root = get_storage_root()
    if not root or not root.exists():
        return jsonify({"error": "Storage path not configured"}), 503

    out_path = _write_dms_zip(root)
    return jsonify({"ok": True, "path": str(out_path), "filename": out_path.name})


@app.route("/api/project/export-full", methods=["GET"])
def export_project_full():
    """Download a complete .dms backup that includes all document files."""
    root = get_storage_root()
    if not root or not root.exists():
        return jsonify({"error": "Storage path not configured"}), 503

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name)[:40]
    filename = f"{safe or 'dms-project'}-full-{stamp}.dms"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": "dms-project",
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": str(root),
            "includes_files": True,
        }, indent=2))
        index_path = root / "index.json"
        if index_path.exists():
            zf.write(index_path, arcname="index.json")
        docs_dir = root / "docs"
        if docs_dir.exists():
            for fpath in sorted(docs_dir.rglob("*")):
                if fpath.is_file():
                    zf.write(fpath, arcname=str(fpath.relative_to(root)))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/octet-stream")


@app.route("/api/project/export-selected", methods=["POST"])
def export_project_selected():
    """Download a .dms backup containing only the selected nodes and their documents.

    Body JSON: { "node_ids": ["NODE-xxx", ...] }
    Selected nodes AND all their descendants are included.
    """
    root = get_storage_root()
    if not root or not root.exists():
        return jsonify({"error": "Storage path not configured"}), 503

    body = request.get_json(force=True) or {}
    selected_ids = set(body.get("node_ids", []))
    if not selected_ids:
        return jsonify({"error": "No nodes selected"}), 400

    idx = read_index()
    doc_index = idx.get("docIndex", [])

    # Collect all node IDs in selected subtrees (selected node + all descendants)
    def collect_subtree_ids(node, parent_included=False):
        nid = node.get("id", "")
        included = parent_included or nid in selected_ids
        ids = {nid} if included else set()
        for child in node.get("children", []):
            ids.update(collect_subtree_ids(child, included))
        return ids

    included_node_ids = collect_subtree_ids(idx.get("tree", {}))

    # Filter docs to only those belonging to included nodes
    included_docs = [d for d in doc_index if d.get("originalNodeId") in included_node_ids]
    included_doc_ids = {d["id"] for d in included_docs}

    # Build a filtered index: keep full tree but only selected docs
    filtered_idx = dict(idx)
    filtered_idx["docIndex"] = included_docs

    # Build a map from doc_id → file path by scanning docs/
    docs_dir = root / "docs"
    doc_file_map: dict[str, Path] = {}
    if docs_dir.exists():
        for fpath in docs_dir.rglob("*"):
            if fpath.is_file():
                doc_id = fpath.name.split("__")[0]
                if doc_id in included_doc_ids:
                    doc_file_map[doc_id] = fpath

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name)[:40]
    filename = f"{safe or 'dms-project'}-selected-{stamp}.dms"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": "dms-project",
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": str(root),
            "includes_files": True,
            "selected_node_count": len(included_node_ids),
            "doc_count": len(included_docs),
        }, indent=2))
        zf.writestr("index.json", json.dumps(filtered_idx, indent=2, ensure_ascii=False))
        for doc_id, fpath in doc_file_map.items():
            zf.write(fpath, arcname=str(fpath.relative_to(root)))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/octet-stream")


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
    tmp_zip.close()  # Must close before f.save() on Windows (file locking)
    try:
        f.save(tmp_zip.name)

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

        # Create the target folder if it doesn't exist.
        # A .dms file contains only index.json — no actual document files.
        # We never delete existing content; importing only restores the
        # tree/index and leaves all physical files and folders untouched.
        if not target.exists():
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


@app.route("/api/project/inspect-dms", methods=["POST"])
def inspect_dms():
    """Read the manifest from a .dms file without importing it.

    Returns the source_path and export date from the embedded manifest.json
    so the UI can offer the user a path-selection dialog before importing.
    """
    if "file" not in request.files:
        return jsonify({"error": "Missing 'file'"}), 400

    f = request.files["file"]
    tmp_zip = tempfile.NamedTemporaryFile(suffix=".dms", delete=False)
    tmp_zip.close()  # Must close before f.save() on Windows (file locking)
    try:
        f.save(tmp_zip.name)
        try:
            with zipfile.ZipFile(tmp_zip.name, "r") as zf:
                if "manifest.json" not in zf.namelist():
                    return jsonify({"error": "Not a valid .dms file (missing manifest.json)"}), 400
                manifest = json.loads(zf.read("manifest.json"))
                if manifest.get("format") != "dms-project":
                    return jsonify({"error": "Not a DMS project file"}), 400
        except zipfile.BadZipFile:
            return jsonify({"error": "Not a valid .dms (zip) file"}), 400
        return jsonify({
            "ok": True,
            "source_path": manifest.get("source_path", ""),
            "exported_at": manifest.get("exported_at", ""),
        })
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

    details_nodes, errors, root = parse_hierarchy_file_with_details(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    nodes = {nid: info["parent_id"] for nid, info in details_nodes.items()}
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
    tmp.close()  # Must close before f.save() on Windows (file locking)
    try:
        f.save(tmp.name)

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


# ---- Face recognition -------------------------------------------------------

def _get_faces_path():
    root = get_storage_root()
    if not root:
        return None
    return root / "faces.json"


def read_faces() -> dict:
    p = _get_faces_path()
    if not p or not p.exists():
        return {"people": [], "labels": [], "recognitions": []}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"people": [], "labels": [], "recognitions": []}


def write_faces(data: dict) -> None:
    p = _get_faces_path()
    if not p:
        raise RuntimeError("Storage path is not configured")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


# Deepface runs in a dedicated Python 3.12 venv because TensorFlow (which
# deepface requires) does not yet support Python 3.14.  All DeepFace calls
# go through deepface_worker.py executed in that venv via subprocess.
_DEEPFACE_VENV = Path.home() / ".dms_deepface"
_DEEPFACE_WORKER = Path(__file__).with_name("deepface_worker.py")


def _deepface_python() -> str | None:
    """Return path to the deepface-venv Python executable, or None if not ready."""
    candidates = [
        _DEEPFACE_VENV / "bin" / "python3",
        _DEEPFACE_VENV / "Scripts" / "python.exe",  # Windows
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _check_deepface() -> bool:
    return _deepface_python() is not None


def _run_deepface_worker(cmd: dict, timeout: int = 300) -> dict:
    """Call deepface_worker.py in the isolated deepface venv and return parsed JSON."""
    python = _deepface_python()
    if not python:
        raise RuntimeError("deepface not installed")
    worker = str(_DEEPFACE_WORKER)
    result = subprocess.run(
        [python, worker],
        input=json.dumps(cmd),
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "deepface worker failed")
    return json.loads(result.stdout)


@app.route("/api/install-deepface", methods=["POST"])
def install_deepface():
    """Set up the deepface venv (Python 3.12) and install deepface + dependencies."""
    # Find Python 3.12 — check well-known paths directly (server PATH may not
    # include /opt/homebrew/bin even when brew is installed on Apple Silicon).
    python312_candidates = [
        "/opt/homebrew/bin/python3.12",   # Homebrew Apple Silicon
        "/usr/local/bin/python3.12",       # Homebrew Intel Mac
        "/usr/bin/python3.12",
        # python.org universal installer (macOS)
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
    ]
    if sys.platform.startswith("win"):
        import winreg  # type: ignore
        try:
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                key = winreg.OpenKey(root, r"SOFTWARE\Python\PythonCore\3.12\InstallPath")
                base = winreg.QueryValueEx(key, "ExecutablePath")[0]
                if base and Path(base).exists():
                    python312_candidates.insert(0, base)
        except Exception:
            pass
        python312_candidates += [r"C:\Python312\python.exe",
                                  r"C:\Program Files\Python312\python.exe"]

    python312 = next((p for p in python312_candidates if Path(p).exists()), None)

    if not python312:
        # Try to install via Homebrew (check known paths, not just PATH)
        brew_candidates = [
            "/opt/homebrew/bin/brew",
            "/usr/local/bin/brew",
            shutil.which("brew") or "",
        ]
        brew = next((b for b in brew_candidates if b and Path(b).exists()), None)
        if brew:
            try:
                subprocess.run([brew, "install", "python@3.12"],
                               capture_output=True, timeout=300, check=True)
            except Exception as e:
                return jsonify({"error": f"brew install python@3.12 失败: {e}"}), 500
            python312 = next((p for p in python312_candidates if Path(p).exists()), None)

        if not python312:
            # No Python 3.12 and no Homebrew — tell the frontend to show download link
            if sys.platform.startswith("win"):
                dl_url = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
            else:
                dl_url = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-macos11.pkg"
            return jsonify({
                "needsPython312": True,
                "downloadUrl": dl_url,
                "message": "人脸识别需要 Python 3.12。请点击下方按钮下载安装，完成后再点击「安装 deepface」。",
            }), 200

    venv_dir = str(_DEEPFACE_VENV)

    # Create venv with Python 3.12
    try:
        subprocess.run([python312, "-m", "venv", venv_dir],
                       capture_output=True, timeout=60, check=True)
    except Exception as e:
        return jsonify({"error": f"创建虚拟环境失败: {e}"}), 500

    pip = _DEEPFACE_VENV / "bin" / "pip"
    if not pip.exists():
        pip = _DEEPFACE_VENV / "Scripts" / "pip.exe"

    # Install deepface (TF will install automatically on Python 3.12)
    try:
        result = subprocess.run(
            [str(pip), "install", "--quiet", "deepface", "tf-keras", "pillow-heif"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "pip install 失败").strip()[-1000:]
            return jsonify({"error": err}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "安装超时（超过10分钟）。请检查网络后重试。"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


def _resolve_photo_path(doc_id: str):
    """Return Path to the actual file for doc_id, or None if not found."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return None
    matches = list(docs_dir.rglob(f"{doc_id}__*"))
    if not matches:
        matches = list(docs_dir.rglob(f"{doc_id}*"))
    return matches[0] if matches else None


def _face_crop_b64(file_path, bbox: dict) -> str:
    """Return 120x120 JPEG crop of face area as base64 string."""
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        from PIL import Image
        img = Image.open(file_path).convert("RGB")
        x, y, w, h = bbox.get("x", 0), bbox.get("y", 0), bbox.get("w", 50), bbox.get("h", 50)
        pad = max(10, int(min(w, h) * 0.3))
        crop = img.crop((max(0, x - pad), max(0, y - pad),
                         min(img.width, x + w + pad), min(img.height, y + h + pad)))
        crop = crop.resize((120, 120), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=85)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()
    except Exception as e:
        return ""


def _cosine_sim(a, b) -> float:
    try:
        import numpy as np
        a, b = np.array(a, dtype=float), np.array(b, dtype=float)
        denom = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)
    except Exception:
        pass
    try:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na * nb > 0 else 0.0
    except Exception:
        return 0.0


@app.route("/api/faces/detect", methods=["POST"])
def faces_detect():
    if not _check_deepface():
        return jsonify({"error": "deepface 未安装", "deepfaceMissing": True}), 200
    data = request.get_json(force=True) or {}
    doc_ids = data.get("docIds", [])
    if not doc_ids:
        return jsonify({"error": "No docIds provided"}), 400

    # Load existing labels for annotation
    faces_data = read_faces()
    label_map = {(lbl["docId"], lbl["faceIdx"]): lbl.get("personId")
                 for lbl in faces_data.get("labels", [])}
    person_name_map = {p["id"]: p["name"] for p in faces_data.get("people", [])}

    # Build worker items (images only, with resolved paths)
    items = []
    skipped = {}
    for doc_id in doc_ids:
        file_path = _resolve_photo_path(doc_id)
        if not file_path:
            skipped[doc_id] = "文件未找到"
            continue
        mime = mimetypes.guess_type(str(file_path))[0] or ""
        if not mime.startswith("image/"):
            skipped[doc_id] = "不是图片文件"
            continue
        items.append({"doc_id": doc_id, "path": str(file_path)})

    results = []
    for doc_id, reason in skipped.items():
        results.append({"docId": doc_id, "faces": [], "error": reason})

    if items:
        try:
            worker_out = _run_deepface_worker({"action": "represent", "items": items})
        except Exception as e:
            return jsonify({"error": f"人脸检测失败: {e}"}), 500

        for r in worker_out.get("results", []):
            doc_id = r["doc_id"]
            if r.get("error"):
                results.append({"docId": doc_id, "faces": [], "error": r["error"]})
                continue
            faces = []
            for idx, f in enumerate(r.get("faces", [])):
                person_id = label_map.get((doc_id, idx))
                faces.append({
                    "faceIdx": idx,
                    "bbox": f["bbox"],
                    "confidence": f["confidence"],
                    "embedding": f["embedding"],
                    "crop_b64": f["crop_b64"],
                    "personId": person_id,
                    "personName": person_name_map.get(person_id) if person_id else None,
                })
            results.append({"docId": doc_id, "faces": faces})

    return jsonify({"results": results})


@app.route("/api/faces/label", methods=["POST"])
def faces_label():
    data = request.get_json(force=True) or {}
    doc_id = data.get("docId", "").strip()
    face_idx = data.get("faceIdx", 0)
    name = (data.get("name") or "").strip()
    embedding = data.get("embedding", [])
    bbox = data.get("bbox", {})

    if not doc_id or not name:
        return jsonify({"error": "Missing docId or name"}), 400

    faces_data = read_faces()

    # Find or create person (case-insensitive match)
    person = next(
        (p for p in faces_data["people"] if p["name"].lower() == name.lower()),
        None,
    )
    if not person:
        person = {"id": "PERSON-" + secrets.token_hex(4).upper(), "name": name}
        faces_data["people"].append(person)

    # Remove any existing label for this (docId, faceIdx)
    faces_data["labels"] = [
        lbl for lbl in faces_data["labels"]
        if not (lbl["docId"] == doc_id and lbl["faceIdx"] == face_idx)
    ]
    faces_data["labels"].append({
        "docId": doc_id,
        "faceIdx": face_idx,
        "personId": person["id"],
        "embedding": embedding,
        "bbox": bbox,
    })

    write_faces(faces_data)
    return jsonify({"ok": True, "personId": person["id"], "personName": person["name"]})


@app.route("/api/faces/label", methods=["DELETE"])
def faces_unlabel():
    data = request.get_json(force=True) or {}
    doc_id = data.get("docId", "").strip()
    face_idx = data.get("faceIdx", 0)

    if not doc_id:
        return jsonify({"error": "Missing docId"}), 400

    faces_data = read_faces()
    faces_data["labels"] = [
        lbl for lbl in faces_data["labels"]
        if not (lbl["docId"] == doc_id and lbl["faceIdx"] == face_idx)
    ]
    write_faces(faces_data)
    return jsonify({"ok": True})


@app.route("/api/faces/people", methods=["GET"])
def faces_people():
    faces_data = read_faces()
    people = faces_data.get("people", [])
    labels = faces_data.get("labels", [])
    recognitions = faces_data.get("recognitions", [])

    result = []
    for person in people:
        pid = person["id"]
        labeled_docs = list({lbl["docId"] for lbl in labels if lbl["personId"] == pid})
        recog_docs = list({r["docId"] for r in recognitions if r["personId"] == pid})
        all_docs = list(set(labeled_docs + recog_docs))
        photo_count = len(all_docs)

        # Avatar: first labeled face crop
        avatar_crop = None
        first_label = next((lbl for lbl in labels if lbl["personId"] == pid), None)
        avatar_doc_id = None
        avatar_face_idx = None
        if first_label:
            avatar_doc_id = first_label["docId"]
            avatar_face_idx = first_label["faceIdx"]
            file_path = _resolve_photo_path(avatar_doc_id)
            if file_path:
                avatar_crop = _face_crop_b64(file_path, first_label.get("bbox", {}))

        result.append({
            "id": pid,
            "name": person["name"],
            "photoCount": photo_count,
            "avatarDocId": avatar_doc_id,
            "avatarFaceIdx": avatar_face_idx,
            "avatarCrop": avatar_crop,
        })

    return jsonify({"people": result, "deepfaceAvailable": _check_deepface()})


@app.route("/api/faces/recognize", methods=["POST"])
def faces_recognize():
    if not _check_deepface():
        return jsonify({"error": "deepface 未安装", "deepfaceMissing": True}), 200

    data = request.get_json(force=True) or {}
    target_person_id = data.get("personId")  # optional — scan for specific person
    threshold_raw = data.get("threshold")
    try:
        threshold_val = float(threshold_raw) if threshold_raw is not None else 0.70
        threshold_val = max(0.50, min(0.99, threshold_val))
    except (TypeError, ValueError):
        threshold_val = 0.70
    node_ids = data.get("nodeIds", [])        # optional — limit scan to these folder subtrees

    faces_data = read_faces()
    labels = faces_data.get("labels", [])
    people = faces_data.get("people", [])

    if not labels:
        return jsonify({"ok": True, "found": 0, "scanned": 0, "message": "没有已标记的人脸用于比对"})

    # Build reference embeddings per person
    person_embeddings = {}  # personId -> [embedding, ...]
    for lbl in labels:
        pid = lbl["personId"]
        if target_person_id and pid != target_person_id:
            continue
        emb = lbl.get("embedding", [])
        if emb:
            person_embeddings.setdefault(pid, []).append(emb)

    if not person_embeddings:
        return jsonify({"ok": True, "found": 0, "scanned": 0, "message": "指定人物没有可用的嵌入向量"})

    # Get all image docs
    idx = read_index()
    all_docs = idx.get("docIndex", [])
    image_docs = [d for d in all_docs if (d.get("mime") or "").startswith("image/")]

    # If specific folders requested, limit to docs in those subtrees
    if node_ids:
        tree = idx.get("tree")
        allowed_ids: set[str] = set()
        for nid in node_ids:
            node = _find_node_by_id(tree, nid) if tree else None
            if node:
                stack = [node]
                while stack:
                    n = stack.pop()
                    for d in (n.get("documents") or []):
                        allowed_ids.add(d["id"] if isinstance(d, dict) else d)
                    stack.extend(n.get("children") or [])
        image_docs = [d for d in image_docs if d["id"] in allowed_ids]

    labeled_pairs = {(lbl["docId"], lbl["faceIdx"]) for lbl in labels}

    # Remove old recognitions for target person (or all if no target)
    if target_person_id:
        faces_data["recognitions"] = [
            r for r in faces_data.get("recognitions", [])
            if r["personId"] != target_person_id
        ]
    else:
        faces_data["recognitions"] = []

    THRESHOLD = threshold_val
    found = 0
    scanned = 0

    # Build worker items for all candidate images
    items = []
    for doc in image_docs:
        doc_id = doc["id"]
        file_path = _resolve_photo_path(doc_id)
        if file_path:
            items.append({"doc_id": doc_id, "path": str(file_path)})

    if not items:
        write_faces(faces_data)
        return jsonify({"ok": True, "found": 0, "scanned": 0})

    try:
        worker_out = _run_deepface_worker({"action": "represent", "items": items},
                                          timeout=600)
    except Exception as e:
        return jsonify({"error": f"人脸识别失败: {e}"}), 500

    for r in worker_out.get("results", []):
        doc_id = r["doc_id"]
        if r.get("error"):
            continue
        scanned += 1
        for idx_face, f in enumerate(r.get("faces", [])):
            if (doc_id, idx_face) in labeled_pairs:
                continue
            emb = f.get("embedding", [])
            if not emb:
                continue
            bbox = f["bbox"]
            for pid, ref_embeddings in person_embeddings.items():
                best_sim = max(_cosine_sim(emb, ref) for ref in ref_embeddings)
                if best_sim >= THRESHOLD:
                    faces_data["recognitions"].append({
                        "docId": doc_id,
                        "faceIdx": idx_face,
                        "personId": pid,
                        "confidence": round(best_sim, 4),
                        "bbox": bbox,
                    })
                    found += 1
                    break

    write_faces(faces_data)
    # Count unique photos (a group photo may have multiple matched faces)
    matched_docs = len({r["docId"] for r in faces_data.get("recognitions", [])
                        if r["personId"] == target_person_id} if target_person_id
                       else {r["docId"] for r in faces_data.get("recognitions", [])})
    return jsonify({"ok": True, "found": found, "photos": matched_docs, "scanned": scanned})


@app.route("/api/faces/photos/<person_id>", methods=["GET"])
def faces_photos(person_id):
    faces_data = read_faces()
    labels = faces_data.get("labels", [])
    recognitions = faces_data.get("recognitions", [])
    labeled_docs = list({lbl["docId"] for lbl in labels if lbl["personId"] == person_id})
    recog_docs = list({r["docId"] for r in recognitions if r["personId"] == person_id})
    all_doc_ids = list(set(labeled_docs + recog_docs))
    return jsonify({"docIds": all_doc_ids})


@app.route("/api/faces/create-folder", methods=["POST"])
def faces_create_folder():
    """Create a tree folder named after a person and link all their matched photos to it."""
    data = request.get_json(force=True) or {}
    person_name = (data.get("personName") or "").strip()
    doc_ids = data.get("docIds") or []
    if not person_name or not doc_ids:
        return jsonify({"error": "personName and docIds are required"}), 400

    idx = read_index()
    tree = idx.get("tree")
    if not tree:
        return jsonify({"error": "No project tree found"}), 400

    # Create/find "人脸匹配" parent folder at root level
    face_parent = _get_or_create_child_node(tree, "人脸匹配")
    # Create/find the person's subfolder
    person_node = _get_or_create_child_node(face_parent, person_name)

    # Link docs that aren't already in this node
    existing_ids = {d["id"] for d in (person_node.get("documents") or []) if isinstance(d, dict)}
    added = 0
    for doc_id in doc_ids:
        if doc_id not in existing_ids:
            person_node.setdefault("documents", []).append({"id": doc_id})
            existing_ids.add(doc_id)
            added += 1

    write_index(idx)
    return jsonify({"ok": True, "added": added, "nodeId": person_node["id"]})


@app.route("/api/faces/person/<person_id>", methods=["DELETE"])
def faces_delete_person(person_id):
    faces_data = read_faces()
    faces_data["people"] = [p for p in faces_data["people"] if p["id"] != person_id]
    faces_data["labels"] = [lbl for lbl in faces_data["labels"] if lbl["personId"] != person_id]
    faces_data["recognitions"] = [r for r in faces_data.get("recognitions", []) if r["personId"] != person_id]
    write_faces(faces_data)
    return jsonify({"ok": True})


# ---- Guide download -------------------------------------------------------
_ONEDRIVE_GUIDE_TEXT = """\
================================================================================
  QCDMS — HOW TO STORE YOUR DOCUMENTS ON MICROSOFT ONEDRIVE
  Step-by-Step Setup Guide for New Users
================================================================================

OVERVIEW
--------
By default, QCDMS saves all your folders and documents on your computer's
local hard drive. This guide shows you how to store everything in Microsoft
OneDrive instead, so your documents are:

  • Automatically backed up to the cloud
  • Accessible from any computer, phone, or tablet
  • Protected even if your computer is lost, stolen, or damaged
  • Shareable with other people if needed

How it works: OneDrive keeps a special sync folder on your computer. Anything
you save in that folder is automatically and silently uploaded to Microsoft's
cloud servers. We simply tell QCDMS to use a subfolder inside that OneDrive
folder as its storage location. QCDMS works exactly as before — it writes
files to that folder, and OneDrive does the rest.

--------------------------------------------------------------------------------
PART 1 — SET UP MICROSOFT ONEDRIVE ON YOUR COMPUTER
--------------------------------------------------------------------------------

You only need to do Part 1 once per computer. If OneDrive is already installed
and you are already signed in, skip to Part 2.

STEP 1: CHECK IF ONEDRIVE IS ALREADY INSTALLED
-----------------------------------------------
  Mac:
    - Look for a cloud icon (looks like a small cloud) in the menu bar at the
      top-right of your screen. If you see it, OneDrive is installed.
    - Alternatively, open Finder and look in the left sidebar for an entry
      called "OneDrive" or "OneDrive - Personal".

  Windows:
    - Look for a cloud icon in the system tray (bottom-right corner of the
      screen, near the clock). If you see it, OneDrive is installed.
    - Alternatively, open File Explorer and look in the left sidebar for
      an entry called "OneDrive" or "OneDrive - Personal".

  If you see OneDrive, skip to STEP 3. If not, continue to STEP 2.


STEP 2: INSTALL MICROSOFT ONEDRIVE
-----------------------------------
  Mac:
    Option A — From the Mac App Store:
      1. Open the App Store (blue icon with the letter "A").
      2. In the search bar, type "Microsoft OneDrive".
      3. Click "Get" and then "Install". You may need to enter your
         Apple ID password.
      4. Once installed, open OneDrive from your Applications folder or
         Launchpad.

    Option B — Direct download:
      1. Open your web browser.
      2. Go to: https://www.microsoft.com/en-us/microsoft-365/onedrive/download
      3. Click "Download" and open the downloaded file.
      4. Follow the on-screen installer instructions.

  Windows 10 / 11:
    OneDrive is usually pre-installed on Windows 10 and 11. If it is missing:
      1. Open your web browser.
      2. Go to: https://www.microsoft.com/en-us/microsoft-365/onedrive/download
      3. Click "Download" and run the downloaded installer.
      4. Follow the on-screen instructions.


STEP 3: SIGN IN TO ONEDRIVE
-----------------------------
  1. Open OneDrive (click the cloud icon in the menu bar or system tray,
     or open it from your Applications folder / Start Menu).

  2. A "Sign in" window will appear. Enter your Microsoft account email
     address. This is usually one of:
       • yourname@outlook.com
       • yourname@hotmail.com
       • yourname@live.com
       • A work or school email ending in @yourcompany.com (Microsoft 365)

  3. Click "Sign in" and enter your password.

  4. Follow any additional prompts (two-factor authentication, etc.).

  5. You will be asked to choose your OneDrive folder location.
     RECOMMENDATION: Accept the default location. It will be:
       Mac:     /Users/[your username]/OneDrive - Personal
       Windows: C:\\Users\\[your username]\\OneDrive - Personal

     Write down this folder path — you will need it in Part 2.

  6. OneDrive will begin syncing. A blue cloud icon will appear in your
     menu bar (Mac) or system tray (Windows) to confirm it is running.


STEP 4: CONFIRM ONEDRIVE IS WORKING
--------------------------------------
  1. Click the OneDrive cloud icon in your menu bar or system tray.
  2. You should see "Up to date" or a sync progress message.
  3. If you see any error messages, resolve them before continuing
     (usually just signing in again fixes them).

--------------------------------------------------------------------------------
PART 2 — CREATE A DEDICATED FOLDER FOR QCDMS INSIDE ONEDRIVE
--------------------------------------------------------------------------------

STEP 5: FIND YOUR ONEDRIVE FOLDER
-----------------------------------
  Mac:
    1. Open Finder (the smiley face icon in your Dock).
    2. In the left sidebar, click "OneDrive" or "OneDrive - Personal".
    3. The full path is typically:
         /Users/[your username]/OneDrive - Personal
       For example: /Users/david/OneDrive - Personal

    TIP — To see the exact full path:
      1. With the OneDrive folder open in Finder, right-click (or
         Control-click) anywhere in the window background.
      2. If "Get Info" is shown, click it and look at "Where:".
      3. Alternatively, hold the Option key and right-click the folder name
         at the top of the window — the full path appears.

  Windows:
    1. Open File Explorer (the folder icon in your taskbar).
    2. In the left sidebar, click "OneDrive" or "OneDrive - Personal".
    3. The address bar at the top shows the full path. It is typically:
         C:\\Users\\[your username]\\OneDrive - Personal
       For example: C:\\Users\\David\\OneDrive - Personal


STEP 6: CREATE A SUBFOLDER FOR QCDMS
---------------------------------------
  It is best to give QCDMS its own dedicated subfolder inside OneDrive,
  rather than putting files directly in the root of OneDrive. This keeps
  things organized.

  Suggested folder name: DMS_Storage
  (You can name it anything you like, such as "My DMS" or "Company Files".)

  Mac — using Finder:
    1. Open Finder and navigate to your OneDrive folder.
    2. Right-click in an empty area of the window.
    3. Select "New Folder".
    4. Type the folder name (e.g., DMS_Storage) and press Enter.
    5. The full path to use later will be:
         /Users/[your username]/OneDrive - Personal/DMS_Storage
       For example: /Users/david/OneDrive - Personal/DMS_Storage

  Windows — using File Explorer:
    1. Open File Explorer and navigate to your OneDrive folder.
    2. Right-click in an empty area of the window.
    3. Select "New" > "Folder".
    4. Type the folder name (e.g., DMS_Storage) and press Enter.
    5. The full path to use later will be:
         C:\\Users\\[your username]\\OneDrive - Personal\\DMS_Storage
       For example: C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

  WRITE DOWN THIS FULL PATH. You will paste it into QCDMS in the next step.

--------------------------------------------------------------------------------
PART 3 — TELL QCDMS TO USE YOUR ONEDRIVE FOLDER
--------------------------------------------------------------------------------

STEP 7: LAUNCH QCDMS
----------------------
  Start QCDMS as you normally would:
    Mac:     Double-click DMS.app (or run start_dms.command)
    Windows: Double-click DMS.exe (or run start_dms.bat)

  QCDMS will open in your web browser automatically.


STEP 8A: IF THIS IS YOUR FIRST TIME USING QCDMS (No data yet)
--------------------------------------------------------------
  When QCDMS opens for the first time, it will show a setup screen that says
  "Set Storage Location" (设置存储位置). This is where you enter your path.

    1. Click inside the text box (it shows a greyed-out example path).

    2. Type (or paste) the full path to your OneDrive folder you created
       in Step 6. Example:
         Mac:     /Users/david/OneDrive - Personal/DMS_Storage
         Windows: C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

    3. Click "Save and Continue" (保存并继续).

    4. QCDMS will create the folder if it doesn't already exist, and will
       show the main document management interface. You are done!

    TIP: You can also type ~ as a shortcut for your home folder on Mac:
         ~/OneDrive - Personal/DMS_Storage


STEP 8B: IF YOU ALREADY HAVE QCDMS DATA (Migrating from local storage)
-----------------------------------------------------------------------
  If you have been using QCDMS and already have documents stored locally,
  follow these steps to move everything to OneDrive.

  IMPORTANT: Before doing anything, make a backup copy of your existing
  QCDMS data folder. This is simply a precaution.

  Step 8B-1: Find your current storage folder.
    - Look at the grey status bar at the very bottom of the QCDMS window.
    - You will see a hard drive icon followed by a folder path, for example:
        /Users/david/Documents/Company01
    - That is your current storage folder. Make a copy of it somewhere safe.

  Step 8B-2: Copy your existing data to OneDrive.
    Option A — Copy before changing (Recommended for large collections):
      1. Open Finder (Mac) or File Explorer (Windows).
      2. Navigate to your current QCDMS storage folder.
      3. Select ALL files and folders inside it (Cmd+A on Mac, Ctrl+A on
         Windows).
      4. Copy them (Cmd+C on Mac, Ctrl+C on Windows).
      5. Navigate to your new OneDrive DMS folder (e.g., OneDrive/DMS_Storage).
      6. Paste (Cmd+V on Mac, Ctrl+V on Windows).
      7. Wait for the copy to finish before continuing.

    Option B — Let QCDMS migrate automatically (for smaller collections):
      QCDMS can copy your current data to the new location automatically.
      Skip ahead to Step 8B-3 and let the app handle it.

  Step 8B-3: Change the storage path in QCDMS.
    1. In the QCDMS window, look at the bottom status bar. You will see
       a path displayed next to a small hard-drive icon.
    2. Click that path. A "Change Storage Location" screen appears.
    3. Clear the text box and type your new OneDrive path:
         Mac:     /Users/david/OneDrive - Personal/DMS_Storage
         Windows: C:\\Users\\David\\OneDrive - Personal\\DMS_Storage
    4. Click "Save" (保存).
    5. If QCDMS detects that the new folder already contains DMS data
       (because you copied it in Option A above), it will ask if you
       want to switch to that data. Click "Continue" (继续).
    6. If the new folder is empty (Option B), QCDMS will copy your current
       index and document list automatically.

  Step 8B-4: Verify the migration was successful.
    1. Check that your folders and documents all appear correctly in QCDMS.
    2. Open a few documents to confirm they can be viewed.
    3. Once satisfied, you may delete the old local storage folder, or keep
       it as an extra backup.


--------------------------------------------------------------------------------
PART 4 — VERIFY ONEDRIVE IS SYNCING YOUR QCDMS DATA
--------------------------------------------------------------------------------

STEP 9: CONFIRM SYNC IS WORKING
---------------------------------
  1. After saving documents in QCDMS, look at the OneDrive cloud icon in
     your menu bar (Mac) or system tray (Windows).

  2. While syncing, the icon will show animated arrows or a progress circle.

  3. When sync is complete, the icon will show a green checkmark or a
     plain cloud, and you will see "Up to date".

  4. To double-check online:
     a. Open your web browser.
     b. Go to https://onedrive.live.com and sign in.
     c. Look for your DMS_Storage folder. You should see your QCDMS files
        (especially index.json and a "docs" subfolder containing your
        uploaded documents).


--------------------------------------------------------------------------------
PART 5 — USING QCDMS ON A SECOND COMPUTER
--------------------------------------------------------------------------------

STEP 10: ACCESS YOUR DOCUMENTS FROM ANOTHER COMPUTER
------------------------------------------------------
  To use the same QCDMS document library on a second Mac or Windows PC:

  1. Install QCDMS on the second computer (copy the DMS.app or DMS.exe).

  2. Install and sign in to OneDrive on the second computer using the same
     Microsoft account (see Steps 1-4 above). OneDrive will sync all your
     files to that computer.

  3. Launch QCDMS on the second computer. It will show the setup screen.

  4. Enter the same OneDrive DMS folder path:
       Mac:     /Users/[username on this computer]/OneDrive - Personal/DMS_Storage
       Windows: C:\\Users\\[username on this computer]\\OneDrive - Personal\\DMS_Storage
     Note: The username part of the path may differ on each computer, but
     the OneDrive folder name (OneDrive - Personal) will be the same.

  5. Click "Save and Continue". QCDMS will detect your existing data and
     open your full document library.

  IMPORTANT — Do not run QCDMS on two computers at exactly the same time.
  Both would be writing to the same files via OneDrive, which can cause
  conflicts. Use it on one computer at a time.


--------------------------------------------------------------------------------
FREQUENTLY ASKED QUESTIONS
--------------------------------------------------------------------------------

Q: What happens if my internet connection goes down?
A: QCDMS will continue to work normally because it reads and writes to the
   local OneDrive sync folder on your computer. Your changes are saved locally
   first and uploaded to the cloud as soon as the internet is restored.

Q: What if OneDrive is not running when I use QCDMS?
A: QCDMS will still work, writing to the local folder. When OneDrive starts
   again, it will sync any changes you made while it was offline.

Q: How much OneDrive storage do I need?
A: A free Microsoft account includes 5 GB of OneDrive storage. A Microsoft 365
   Personal subscription includes 1 TB (1,000 GB). For most personal document
   libraries, 5 GB is more than sufficient. You can check how much storage
   you are using at https://onedrive.live.com (sign in, click your name or
   avatar in the top right, then "Storage").

Q: Can I share my QCDMS library with someone else?
A: You can share the OneDrive folder with another person via OneDrive's
   sharing feature. However, both people should not run QCDMS at the same
   time on the same folder, as this can cause file conflicts.

Q: Will OneDrive compress or change my documents?
A: No. OneDrive stores your files exactly as they are. PDFs, images, and
   all other files are stored and retrieved without any modification.

Q: How do I find out my exact OneDrive folder path on Mac?
A: Open Terminal (press Cmd+Space, type "Terminal", press Enter) and type:
     ls ~/OneDrive*
   Press Enter. The folder name(s) shown are inside your home directory.
   Your full path is /Users/[your username]/[that folder name].
   Example: /Users/david/OneDrive - Personal

Q: How do I find out my exact OneDrive folder path on Windows?
A: Open File Explorer, click on OneDrive in the left sidebar, then look at
   the address bar at the top. It shows the full path.
   Alternatively, right-click the OneDrive tray icon and choose "Settings",
   then look in the Account tab for the folder location.

Q: What is "OneDrive - Personal" vs. "OneDrive - [Company Name]"?
A: "OneDrive - Personal" uses your personal Microsoft account (Outlook,
   Hotmail, Live). "OneDrive - [Company Name]" is a work or school account
   managed by your organization. Either will work with QCDMS; just make sure
   to use the correct folder path.

--------------------------------------------------------------------------------
QUICK REFERENCE — PATH EXAMPLES
--------------------------------------------------------------------------------

  Mac (Personal account):
    /Users/david/OneDrive - Personal/DMS_Storage

  Mac (Work/School account, replace "Contoso" with your company name):
    /Users/david/OneDrive - Contoso/DMS_Storage

  Windows (Personal account):
    C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

  Windows (Work/School account):
    C:\\Users\\David\\OneDrive - Contoso\\DMS_Storage

--------------------------------------------------------------------------------
SUPPORT
--------------------------------------------------------------------------------

  For QCDMS issues, contact your QCDMS administrator.

  For OneDrive issues, visit Microsoft's support site:
    https://support.microsoft.com/onedrive

================================================================================
  END OF GUIDE
================================================================================
"""

@app.route("/guides/onedrive-setup", methods=["GET"])
def download_onedrive_guide():
    buf = io.BytesIO(_ONEDRIVE_GUIDE_TEXT.encode("utf-8"))
    return send_file(buf, mimetype="text/plain",
                     as_attachment=True,
                     download_name="OneDrive_Setup_Guide.txt")


_ONEDRIVE_GUIDE_CN_TEXT = """\
================================================================================
  QCDMS — 如何将文档保存到 Microsoft OneDrive
  新用户逐步设置指南
================================================================================

概述
----
默认情况下，QCDMS 将所有文件夹和文档保存在电脑本地硬盘上。本指南将引导您将
存储位置改为 Microsoft OneDrive，这样您的文档将：

  • 自动备份到云端
  • 可从任何电脑、手机或平板访问
  • 即使电脑丢失、被盗或损坏，数据也不会丢失
  • 可根据需要与他人共享

工作原理：OneDrive 在您的电脑上建立一个专属同步文件夹。保存在该文件夹中的
任何内容都会自动静默上传到微软云端服务器。我们只需告诉 QCDMS 使用该 OneDrive
文件夹内的子文件夹作为存储位置即可。QCDMS 的使用方式完全不变——它照常向该
文件夹写入文件，OneDrive 负责其余的同步工作。

--------------------------------------------------------------------------------
第一部分 — 在电脑上安装并设置 Microsoft OneDrive
--------------------------------------------------------------------------------

每台电脑只需设置一次。如果 OneDrive 已安装并且您已登录，请直接跳至第二部分。

第一步：检查是否已安装 OneDrive
--------------------------------
  Mac（苹果电脑）：
    - 查看屏幕右上角菜单栏中是否有云朵图标（形似小云）。有则表示已安装。
    - 或者，打开 Finder（访达），查看左侧栏中是否有"OneDrive"或
      "OneDrive - Personal"条目。

  Windows：
    - 查看屏幕右下角系统托盘（时钟旁边）是否有云朵图标。有则表示已安装。
    - 或者，打开文件资源管理器，查看左侧栏中是否有"OneDrive"条目。

  若已看到 OneDrive，请跳至第三步。否则继续第二步。


第二步：安装 Microsoft OneDrive
---------------------------------
  Mac（苹果电脑）：
    方式一 — 从 Mac App Store 安装：
      1. 打开 App Store（蓝色"A"字图标）。
      2. 在搜索栏中输入"Microsoft OneDrive"。
      3. 点击"获取"，然后点击"安装"。可能需要输入 Apple ID 密码。
      4. 安装完成后，从应用程序文件夹或启动台打开 OneDrive。

    方式二 — 直接下载：
      1. 打开浏览器。
      2. 访问：https://www.microsoft.com/zh-cn/microsoft-365/onedrive/download
      3. 点击"下载"，打开下载的文件。
      4. 按照屏幕提示完成安装。

  Windows 10 / 11：
    OneDrive 通常已预装在 Windows 10 和 11 中。如果缺失：
      1. 打开浏览器。
      2. 访问：https://www.microsoft.com/zh-cn/microsoft-365/onedrive/download
      3. 点击"下载"并运行安装程序。
      4. 按照屏幕提示完成安装。


第三步：登录 OneDrive
-----------------------
  1. 打开 OneDrive（点击菜单栏或系统托盘中的云朵图标，或从应用程序文件夹 /
     开始菜单打开）。

  2. 将出现"登录"窗口。输入您的微软账户电子邮件地址，通常为：
       • yourname@outlook.com
       • yourname@hotmail.com
       • yourname@live.com
       • 单位或学校邮件（以 @yourcompany.com 结尾，即 Microsoft 365 账户）

  3. 点击"登录"并输入密码。

  4. 按照提示完成任何附加验证（如两步验证等）。

  5. 系统会询问您选择 OneDrive 文件夹的保存位置。
     建议：接受默认位置，通常为：
       Mac：     /Users/[您的用户名]/OneDrive - Personal
       Windows： C:\\Users\\[您的用户名]\\OneDrive - Personal

     请记下此路径——第二部分将用到它。

  6. OneDrive 开始同步后，菜单栏（Mac）或系统托盘（Windows）将显示蓝色云朵图标。


第四步：确认 OneDrive 正常运行
---------------------------------
  1. 点击菜单栏或系统托盘中的 OneDrive 云朵图标。
  2. 应看到"已是最新"或同步进度提示。
  3. 如有错误提示，请先解决（通常重新登录即可），再继续。

--------------------------------------------------------------------------------
第二部分 — 在 OneDrive 中为 QCDMS 创建专用文件夹
--------------------------------------------------------------------------------

第五步：找到您的 OneDrive 文件夹
-----------------------------------
  Mac：
    1. 打开 Finder（Dock 中的笑脸图标）。
    2. 点击左侧栏中的"OneDrive"或"OneDrive - Personal"。
    3. 完整路径通常为：
         /Users/[您的用户名]/OneDrive - Personal
       示例：/Users/david/OneDrive - Personal

    提示 — 查看精确完整路径的方法：
      1. 在 Finder 中打开 OneDrive 文件夹后，在窗口空白处右键单击。
      2. 点击"显示简介"，查看"位置"字段。
      3. 或者，按住 Option 键并右键单击窗口顶部的文件夹名称，完整路径将显示出来。

  Windows：
    1. 打开文件资源管理器（任务栏中的文件夹图标）。
    2. 点击左侧栏中的"OneDrive"或"OneDrive - Personal"。
    3. 地址栏顶部会显示完整路径，通常为：
         C:\\Users\\[您的用户名]\\OneDrive - Personal
       示例：C:\\Users\\David\\OneDrive - Personal


第六步：为 QCDMS 创建子文件夹
--------------------------------
  建议在 OneDrive 内为 QCDMS 创建一个专用子文件夹，而不是直接使用 OneDrive
  根目录，这样可以保持整洁有序。

  建议文件夹名称：DMS_Storage
  （您也可以自定义名称，如"我的文档库"或"公司文件"等。）

  Mac — 使用 Finder（访达）：
    1. 打开 Finder，进入您的 OneDrive 文件夹。
    2. 在窗口空白处右键单击。
    3. 选择"新建文件夹"。
    4. 输入文件夹名称（例如：DMS_Storage），按回车键确认。
    5. 之后要填写的完整路径为：
         /Users/[您的用户名]/OneDrive - Personal/DMS_Storage
       示例：/Users/david/OneDrive - Personal/DMS_Storage

  Windows — 使用文件资源管理器：
    1. 打开文件资源管理器，进入您的 OneDrive 文件夹。
    2. 在窗口空白处右键单击。
    3. 选择"新建" > "文件夹"。
    4. 输入文件夹名称（例如：DMS_Storage），按回车键确认。
    5. 之后要填写的完整路径为：
         C:\\Users\\[您的用户名]\\OneDrive - Personal\\DMS_Storage
       示例：C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

  请记下此完整路径，下一步将粘贴到 QCDMS 中。

--------------------------------------------------------------------------------
第三部分 — 告知 QCDMS 使用 OneDrive 文件夹
--------------------------------------------------------------------------------

第七步：启动 QCDMS
--------------------
  按照平时的方式启动 QCDMS：
    Mac：     双击 DMS.app（或运行 start_dms.command）
    Windows： 双击 DMS.exe（或运行 start_dms.bat）

  QCDMS 将自动在浏览器中打开。


第八步（A）：首次使用 QCDMS（尚无数据）
-----------------------------------------
  首次打开 QCDMS 时，将显示"设置存储位置"界面。

    1. 点击文本框（显示灰色示例路径）。

    2. 输入或粘贴您在第六步中创建的 OneDrive 文件夹完整路径，例如：
         Mac：     /Users/david/OneDrive - Personal/DMS_Storage
         Windows： C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

    3. 点击"保存并继续"。

    4. 如果文件夹不存在，QCDMS 将自动创建，然后进入主界面。设置完成！

    提示：在 Mac 上可用 ~ 代替主文件夹路径：
         ~/OneDrive - Personal/DMS_Storage


第八步（B）：已有 QCDMS 数据（从本地迁移到 OneDrive）
--------------------------------------------------------
  如果您之前已在使用 QCDMS 并在本地存有文档，请按以下步骤将数据迁移到 OneDrive。

  重要提示：操作前，请先备份现有的 QCDMS 数据文件夹。

  步骤 B-1：找到当前存储文件夹。
    - 查看 QCDMS 窗口底部状态栏。
    - 您会看到一个硬盘图标后跟一个文件夹路径，例如：
        /Users/david/Documents/Company01
    - 这就是当前存储文件夹，请先将其复制到安全位置作为备份。

  步骤 B-2：将现有数据复制到 OneDrive。
    方式一 — 先复制再切换（推荐用于大量文档）：
      1. 打开 Finder（Mac）或文件资源管理器（Windows）。
      2. 进入当前 QCDMS 存储文件夹。
      3. 全选其中所有文件和文件夹（Mac：Cmd+A；Windows：Ctrl+A）。
      4. 复制（Mac：Cmd+C；Windows：Ctrl+C）。
      5. 进入新的 OneDrive DMS 文件夹（如 OneDrive/DMS_Storage）。
      6. 粘贴（Mac：Cmd+V；Windows：Ctrl+V）。
      7. 等待复制完成后再继续。

    方式二 — 让 QCDMS 自动迁移（适用于少量文档）：
      QCDMS 可自动将当前数据复制到新位置。
      跳过此步骤，直接进行步骤 B-3，让程序自动处理。

  步骤 B-3：在 QCDMS 中更改存储路径。
    1. 查看 QCDMS 窗口底部状态栏，点击硬盘图标旁边显示的路径。
    2. 出现"更改存储位置"界面。
    3. 清除文本框内容，输入新的 OneDrive 路径：
         Mac：     /Users/david/OneDrive - Personal/DMS_Storage
         Windows： C:\\Users\\David\\OneDrive - Personal\\DMS_Storage
    4. 点击"保存"。
    5. 如果 QCDMS 检测到新文件夹中已有 DMS 数据（即方式一已完成复制），
       将询问是否切换到该数据，点击"继续"即可。
    6. 如果新文件夹为空（方式二），QCDMS 将自动复制当前索引和文档列表。

  步骤 B-4：验证迁移是否成功。
    1. 检查 QCDMS 中的文件夹和文档是否正常显示。
    2. 打开几份文档，确认可以正常查看。
    3. 确认无误后，可以删除原本地存储文件夹，或保留作为额外备份。


--------------------------------------------------------------------------------
第四部分 — 验证 OneDrive 正在同步 QCDMS 数据
--------------------------------------------------------------------------------

第九步：确认同步正常
----------------------
  1. 在 QCDMS 中保存文档后，查看菜单栏（Mac）或系统托盘（Windows）中的
     OneDrive 云朵图标。

  2. 同步中时，图标会显示动态箭头或进度圆圈。

  3. 同步完成后，图标会显示绿色对勾或静态云朵，并提示"已是最新"。

  4. 在线确认方法：
     a. 打开浏览器。
     b. 访问 https://onedrive.live.com 并登录。
     c. 查找 DMS_Storage 文件夹，应能看到 QCDMS 文件（尤其是 index.json
        和包含上传文档的 docs 子文件夹）。


--------------------------------------------------------------------------------
第五部分 — 在第二台电脑上使用 QCDMS
--------------------------------------------------------------------------------

第十步：在另一台电脑上访问您的文档
-------------------------------------
  要在第二台 Mac 或 Windows 电脑上使用同一个 QCDMS 文档库：

  1. 在第二台电脑上安装 QCDMS（复制 DMS.app 或 DMS.exe）。

  2. 使用相同的微软账户在第二台电脑上安装并登录 OneDrive
     （参见第一步至第四步）。OneDrive 将自动同步所有文件。

  3. 在第二台电脑上启动 QCDMS，将显示设置界面。

  4. 输入相同的 OneDrive DMS 文件夹路径：
       Mac：     /Users/[此电脑的用户名]/OneDrive - Personal/DMS_Storage
       Windows： C:\\Users\\[此电脑的用户名]\\OneDrive - Personal\\DMS_Storage
     注意：路径中的用户名部分可能因电脑而异，但 OneDrive 文件夹名称
     （OneDrive - Personal）是相同的。

  5. 点击"保存并继续"，QCDMS 将识别已有数据并打开完整文档库。

  重要提示 — 请勿在两台电脑上同时运行 QCDMS。
  两台电脑同时通过 OneDrive 写入同一文件可能导致冲突。请每次只在一台电脑上使用。


--------------------------------------------------------------------------------
常见问题解答
--------------------------------------------------------------------------------

问：如果网络断开，会怎样？
答：QCDMS 仍可正常使用，因为它读写的是电脑本地的 OneDrive 同步文件夹。
   更改会先保存在本地，待网络恢复后自动上传到云端。

问：如果使用 QCDMS 时 OneDrive 未运行，会怎样？
答：QCDMS 仍将正常写入本地文件夹。OneDrive 再次启动后，会自动同步期间的更改。

问：需要多少 OneDrive 存储空间？
答：免费微软账户提供 5 GB OneDrive 空间。Microsoft 365 个人版提供 1 TB（1000 GB）。
   对于大多数个人文档库，5 GB 已足够。您可以登录 https://onedrive.live.com，
   点击右上角姓名或头像，选择"存储"查看已用空间。

问：可以与他人共享 QCDMS 文档库吗？
答：可以通过 OneDrive 的共享功能与他人共享文件夹。但请注意，两人不应同时在
   同一文件夹上运行 QCDMS，否则可能导致文件冲突。

问：OneDrive 会压缩或修改我的文档吗？
答：不会。OneDrive 原样存储您的文件。PDF、图片及所有其他文件均不会被修改。

问：如何在 Mac 上找到精确的 OneDrive 文件夹路径？
答：打开终端（按 Cmd+空格键，输入"终端"，按回车），输入：
     ls ~/OneDrive*
   按回车键后，显示的文件夹名即在您的主目录下。
   完整路径为：/Users/[您的用户名]/[显示的文件夹名]
   示例：/Users/david/OneDrive - Personal

问：如何在 Windows 上找到精确的 OneDrive 文件夹路径？
答：打开文件资源管理器，点击左侧栏中的 OneDrive，地址栏将显示完整路径。
   或者，右键单击系统托盘中的 OneDrive 图标，选择"设置"，
   在"账户"选项卡中查看文件夹位置。

问："OneDrive - Personal"与"OneDrive - [公司名]"有何区别？
答："OneDrive - Personal"使用个人微软账户（Outlook、Hotmail、Live）。
   "OneDrive - [公司名]"是由单位或学校管理的工作账户（Microsoft 365）。
   两种账户均可与 QCDMS 配合使用，请确保使用正确的文件夹路径即可。

--------------------------------------------------------------------------------
路径快速参考
--------------------------------------------------------------------------------

  Mac（个人账户）：
    /Users/david/OneDrive - Personal/DMS_Storage

  Mac（工作/学校账户，将"Contoso"替换为您的公司名）：
    /Users/david/OneDrive - Contoso/DMS_Storage

  Windows（个人账户）：
    C:\\Users\\David\\OneDrive - Personal\\DMS_Storage

  Windows（工作/学校账户）：
    C:\\Users\\David\\OneDrive - Contoso\\DMS_Storage

--------------------------------------------------------------------------------
技术支持
--------------------------------------------------------------------------------

  如有 QCDMS 相关问题，请联系您的 QCDMS 管理员。

  如有 OneDrive 相关问题，请访问微软支持页面：
    https://support.microsoft.com/zh-cn/onedrive

================================================================================
  指南结束
================================================================================
"""

@app.route("/guides/onedrive-setup-cn", methods=["GET"])
def download_onedrive_guide_cn():
    buf = io.BytesIO(_ONEDRIVE_GUIDE_CN_TEXT.encode("utf-8"))
    return send_file(buf, mimetype="text/plain; charset=utf-8",
                     as_attachment=True,
                     download_name="OneDrive设置指南.txt")


# ---- Quit -----------------------------------------------------------------
@app.route("/api/quit", methods=["POST"])
def quit_app():
    """Shut down the DMS server and exit the process."""
    import threading
    _auto_backup()
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

    # Auto-backup on Ctrl+C or SIGTERM
    import signal

    def _handle_shutdown(signum, frame):
        print()
        _auto_backup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Use the built-in dev server. Fine for local single-user use.
    # For a real deployment, run via gunicorn or similar.
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

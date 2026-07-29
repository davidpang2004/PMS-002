#!/usr/bin/env python3
"""
audit_index.py — one-time integrity check/repair for a DMS storage folder.

Compares index.json's docIndex (what DMS.exe shows you: name, size, date,
GPS, ...) against what actually exists in docs/ on disk, and reports or
repairs the difference. Run this with DMS.exe CLOSED — it reads and writes
index.json directly, and a running server could overwrite your changes or
race with them.

Three kinds of drift are detected:
  1. MISSING  — docIndex says a file exists, but it's not on disk anywhere
                under docs/. The file is gone; only the metadata survives.
  2. ORPHAN   — a file exists on disk under docs/ but has no docIndex entry
                at all (the reverse problem — untracked file).
  3. UNLINKED — the file exists and its docIndex entry is fine, but it isn't
                listed in its folder's own "documents" array in the tree, so
                it may not show up when browsing that folder (only via
                search/unreferenced-docs). This is a free, lossless repair.

Usage:
    python3 audit_index.py                     # dry run, whole archive
    python3 audit_index.py --folder 2013        # dry run, only paths containing "2013"
    python3 audit_index.py --folder 2013 --apply  # actually write the fix

Without --apply, nothing is changed — you get a report only. Re-run with
--apply once you're happy with what the dry run says it will do.

MISSING entries are, with --apply, moved into a "Missing Files" folder at
the root of the tree (metadata kept intact, just relocated out of the real
folders) rather than deleted, so you keep a full record to work from if you
later find the original photos elsewhere. UNLINKED entries are silently
repaired either way (their file is present — nothing to lose).

A timestamped backup of index.json is written before any change.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".pms_dms_config.json"
MISSING_FOLDER_NAME = "Missing Files"


def _safe_folder_name(name: str) -> str:
    """Mirrors dms_server.py's _safe_folder_name — must match exactly so
    resolved paths line up with what the app actually created on disk."""
    safe = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '_', name).strip('. ')
    return (safe[:60] if len(safe) > 60 else safe) or "node"


def get_storage_root(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    if not CONFIG_PATH.exists():
        sys.exit(f"No --storage given and {CONFIG_PATH} not found. "
                  f"Pass --storage \"<path to your DMS storage folder>\".")
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"Could not read {CONFIG_PATH}: {e}")
    p = (cfg.get("storage_path") or "").strip()
    if not p:
        sys.exit(f"{CONFIG_PATH} has no storage_path set. Pass --storage explicitly.")
    return Path(p).expanduser().resolve()


def walk_tree(node, fn):
    if not node:
        return
    fn(node)
    for child in (node.get("children") or []):
        walk_tree(child, fn)


def find_node_by_id(tree, node_id):
    result = [None]
    def visit(n):
        if result[0] is None and n.get("id") == node_id:
            result[0] = n
    walk_tree(tree, visit)
    return result[0]


def node_path_parts(tree, node_id) -> list[str]:
    """Mirrors dms_server.py's _get_node_path_parts."""
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


def remove_doc_from_all_nodes(tree, doc_id: str) -> None:
    def visit(n):
        docs = n.get("documents")
        if docs:
            n["documents"] = [d for d in docs if d.get("id") != doc_id]
    walk_tree(tree, visit)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--storage", help="Path to the DMS storage folder (contains index.json and docs/). "
                                       "Defaults to whatever DMS.exe is currently configured to use.")
    ap.add_argument("--folder", help="Only check documents whose folder path contains this name "
                                      "(case-insensitive), e.g. --folder 2013. This is a folder NAME, "
                                      "not a path — do not pass a full Windows/Mac path here.")
    ap.add_argument("--apply", action="store_true",
                     help="Actually write the fix. Without this, only a report is produced.")
    args = ap.parse_args()

    if args.folder and ("\\" in args.folder or "/" in args.folder):
        last_part = re.split(r"[\\/]+", args.folder.strip())[-1] or args.folder
        print(f"Note: --folder {args.folder!r} looks like a path, not a folder name. "
              f"Using just {last_part!r} instead. Pass a bare folder name (e.g. 2013), "
              f"not a full path, to avoid this.")
        print()
        args.folder = last_part

    root = get_storage_root(args.storage)
    index_path = root / "index.json"
    docs_dir = root / "docs"

    if not index_path.exists():
        sys.exit(f"No index.json found at {index_path}")
    if not docs_dir.exists():
        sys.exit(f"No docs/ folder found at {docs_dir}")

    idx = json.loads(index_path.read_text())
    tree = idx.get("tree")
    doc_index = idx.get("docIndex") or []
    if not tree:
        sys.exit("index.json has no tree — nothing to check.")

    print(f"Storage root : {root}")
    print(f"index.json   : {index_path}")
    print(f"docIndex     : {len(doc_index)} entries")
    print(f"Mode         : {'APPLY (will write changes)' if args.apply else 'DRY RUN (no changes will be written)'}")
    if args.folder:
        print(f"Folder filter: paths containing {args.folder!r}")
    print()

    # Map every file actually on disk under docs/, keyed the same way the
    # server does: the DOC-ID prefix before "__" in the filename.
    file_map: dict[str, Path] = {}
    for f in docs_dir.rglob("*"):
        if not f.is_file():
            continue
        name = f.name
        sep = name.find("__")
        key = name[:sep] if sep != -1 else f.stem
        if key not in file_map:
            file_map[key] = f

    missing_rows = []
    unlinked_rows = []
    orphan_rows = []
    ok_count = 0
    out_of_scope_count = 0

    folder_filter = args.folder.lower() if args.folder else None

    for entry in doc_index:
        doc_id = entry.get("id")
        if not doc_id:
            continue
        node_id = entry.get("originalNodeId") or entry.get("nodeId")
        path_parts = node_path_parts(tree, node_id) if node_id else []
        path_str = "/".join(path_parts)

        if folder_filter is not None:
            if not any(folder_filter in part.lower() for part in path_parts):
                out_of_scope_count += 1
                continue

        row = {
            "doc_id": doc_id,
            "name": entry.get("name", ""),
            "size": entry.get("size", ""),
            "uploadedAt": entry.get("uploadedAt", ""),
            "folder_path": path_str,
            "gps": (entry.get("metadata", {}) or {}).get("Location", {}).get("actual", "")
                   if isinstance(entry.get("metadata"), dict) else "",
        }

        on_disk = file_map.get(doc_id)
        if not on_disk:
            missing_rows.append(row)
            continue

        # File exists — is it actually linked into its folder's documents list?
        target_node = find_node_by_id(tree, node_id) if node_id else None
        linked = bool(target_node and any(
            d.get("id") == doc_id for d in (target_node.get("documents") or [])
        ))
        if linked:
            ok_count += 1
        else:
            unlinked_rows.append(row)
            if args.apply and target_node is not None:
                target_node.setdefault("documents", []).append({"id": doc_id})

    # Orphan files: on disk, no docIndex entry at all. Not folder-filtered
    # (an orphan has no resolvable folder to filter by) unless no filter given.
    if folder_filter is None:
        indexed_ids = {e.get("id") for e in doc_index}
        for key, path in file_map.items():
            if key not in indexed_ids:
                orphan_rows.append({"doc_id": key, "path": str(path)})

    # --- Apply the MISSING repair: move to a "Missing Files" bin ---------
    moved = 0
    if args.apply and missing_rows:
        missing_node_id = None
        for child in (tree.get("children") or []):
            if child.get("name") == MISSING_FOLDER_NAME:
                missing_node_id = child["id"]
                break
        if missing_node_id is None:
            missing_node_id = f"NODE-{secrets.token_hex(4).upper()}"
            tree.setdefault("children", []).append({
                "id": missing_node_id, "name": MISSING_FOLDER_NAME,
                "children": [], "documents": [],
            })
        missing_node = find_node_by_id(tree, missing_node_id)

        by_id = {e.get("id"): e for e in doc_index}
        for row in missing_rows:
            doc_id = row["doc_id"]
            remove_doc_from_all_nodes(tree, doc_id)
            missing_node.setdefault("documents", []).append({"id": doc_id})
            entry = by_id.get(doc_id)
            if entry is not None:
                entry["originalNodeId"] = missing_node_id
            moved += 1

    # --- Backup + write ----------------------------------------------------
    if args.apply and (missing_rows or unlinked_rows):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = root / f"index.json.bak-{stamp}"
        backup_path.write_text(index_path.read_text())
        idx["tree"] = tree
        idx["docIndex"] = doc_index
        index_path.write_text(json.dumps(idx, indent=2))
        print(f"Backup written to : {backup_path}")
        print(f"index.json updated: {moved} moved to {MISSING_FOLDER_NAME!r}, "
              f"{len(unlinked_rows)} re-linked into their folder")
        print()

    # --- Report --------------------------------------------------------
    print(f"OK (file present, correctly linked) : {ok_count}")
    print(f"UNLINKED (file present, re-linked)  : {len(unlinked_rows)}")
    print(f"MISSING (file not found on disk)    : {len(missing_rows)}")
    if folder_filter is None:
        print(f"ORPHAN (file on disk, no metadata)  : {len(orphan_rows)}")
    else:
        print(f"(skipped, outside --folder filter)  : {out_of_scope_count}")
    print()

    if missing_rows or orphan_rows:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = root / f"audit-report-{stamp}.csv"
        with report_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["status", "doc_id", "name", "size", "uploadedAt", "folder_path", "gps"])
            for row in missing_rows:
                w.writerow(["MISSING", row["doc_id"], row["name"], row["size"],
                            row["uploadedAt"], row["folder_path"], row["gps"]])
            for row in orphan_rows:
                w.writerow(["ORPHAN", row["doc_id"], "", "", "", "", row["path"]])
        print(f"Detailed report written to: {report_path}")

    if missing_rows and not args.apply:
        print()
        print(f"Dry run only — nothing was changed. Re-run with --apply to move these "
              f"{len(missing_rows)} entries into a {MISSING_FOLDER_NAME!r} folder.")


if __name__ == "__main__":
    main()

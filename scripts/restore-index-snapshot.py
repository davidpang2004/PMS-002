#!/usr/bin/env python3
"""restore-index-snapshot.py — recover index.json from an automatic snapshot.

dms_server.write_index() copies the previous index.json into
<storage_root>/.snapshots/ (throttled to once per 10 minutes) before every
overwrite, so a bad edit -- a bug, or an accidental bulk delete in the tree
editor -- can be undone. This script lists those snapshots and restores one.

Usage:
    python3 scripts/restore-index-snapshot.py <storage_root>
    python3 scripts/restore-index-snapshot.py <storage_root> --list
    python3 scripts/restore-index-snapshot.py <storage_root> --restore index-20260920-143000-123456.json

With no --restore, it lists available snapshots and prompts for one
interactively. The current index.json is always copied to
index.json.before-restore-<timestamp> first, so restoring is itself
undoable.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("storage_root", help="The DMS project's storage folder (contains index.json and docs/)")
    parser.add_argument("--list", action="store_true", help="List available snapshots and exit")
    parser.add_argument("--restore", metavar="FILENAME", help="Restore this snapshot without prompting")
    args = parser.parse_args()

    root = Path(args.storage_root).expanduser().resolve()
    index_path = root / "index.json"
    snap_dir = root / ".snapshots"

    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1
    if not snap_dir.is_dir():
        print(f"No snapshots found (no {snap_dir} directory yet).", file=sys.stderr)
        return 1

    snapshots = sorted(snap_dir.glob("index-*.json"))
    if not snapshots:
        print(f"No snapshots found in {snap_dir}.", file=sys.stderr)
        return 1

    if args.list or not args.restore:
        print(f"Snapshots in {snap_dir}:")
        for i, s in enumerate(snapshots, 1):
            when = datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            try:
                doc_count = len(json.loads(s.read_text()).get("docIndex", []))
                summary = f"{doc_count} documents"
            except (json.JSONDecodeError, OSError):
                summary = "(could not read)"
            print(f"  {i:>3}. {s.name}   {when}   {summary}")
        if args.list:
            return 0

    if args.restore:
        chosen = snap_dir / args.restore
        if not chosen.exists():
            print(f"Error: no such snapshot: {chosen}", file=sys.stderr)
            return 1
    else:
        choice = input("\nRestore which snapshot? (number, or blank to cancel): ").strip()
        if not choice:
            print("Cancelled.")
            return 0
        try:
            chosen = snapshots[int(choice) - 1]
        except (ValueError, IndexError):
            print(f"Error: invalid choice: {choice}", file=sys.stderr)
            return 1

    if index_path.exists():
        backup_name = f"index.json.before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(index_path, root / backup_name)
        print(f"Current index.json backed up to {backup_name}")

    shutil.copy2(chosen, index_path)
    print(f"Restored {chosen.name} -> index.json")
    print("Restart DMS.app (or refresh the browser tab) to see the restored data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

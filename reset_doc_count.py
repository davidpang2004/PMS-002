"""
DMS Document-Count Reset Tool
=============================

A standalone, single-use tool for support use: resets a user's lifetime
"documents uploaded" counter back to zero, then permanently disables
itself on that machine so it cannot be run a second time.

Build into a small executable with PyInstaller and send just that one
file to the user:

    Mac:     pyinstaller --onefile --windowed --name ResetDocCount reset_doc_count.py
    Windows: pyinstaller --onefile --windowed --name ResetDocCount reset_doc_count.py

The result is dist/ResetDocCount(.app|.exe) — no Python installation
required on the user's machine.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

# Matches dms_server.py's CONFIG_PATH — where DMS records its storage folder.
CONFIG_PATH = Path.home() / ".pms_dms_config.json"

# Separate from DMS's own config so this tool never touches a file DMS
# itself reads/writes. Its mere existence means "already used on this machine".
USED_MARKER = Path.home() / ".pms_dms_reset_used.json"


def _message(title: str, body: str, is_error: bool = False) -> None:
    root = tk.Tk()
    root.withdraw()
    (messagebox.showerror if is_error else messagebox.showinfo)(title, body)
    root.destroy()


def main() -> None:
    if USED_MARKER.exists():
        _message(
            "Reset Already Used",
            "This reset tool has already been used on this computer.\n"
            "It can only reset the document count once.",
            is_error=True,
        )
        sys.exit(1)

    if not CONFIG_PATH.exists():
        _message(
            "DMS Not Found",
            f"Could not find a DMS installation on this computer.\n\n"
            f"Expected to find:\n{CONFIG_PATH}",
            is_error=True,
        )
        sys.exit(1)

    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        storage_path = (cfg.get("storage_path") or "").strip()
        if not storage_path:
            raise ValueError("DMS has no storage folder configured yet")

        index_path = Path(storage_path) / "index.json"
        if not index_path.exists():
            raise FileNotFoundError(f"index.json not found at {index_path}")

        idx = json.loads(index_path.read_text())

        # Back up before modifying — if anything below goes wrong, the
        # user's document tree/index is never left in a half-written state.
        backup_path = index_path.with_suffix(".json.bak-reset")
        backup_path.write_text(json.dumps(idx, indent=2))

        idx["totalDocsUploaded"] = 0
        index_path.write_text(json.dumps(idx, indent=2))

    except Exception as e:
        _message(
            "Reset Failed",
            f"Could not reset the document count:\n{e}\n\n"
            "No changes were made. Please contact support.",
            is_error=True,
        )
        sys.exit(1)

    # Write the used-marker only after the reset above fully succeeded, so a
    # failed attempt never burns the single use.
    USED_MARKER.write_text(json.dumps({"used": True}))

    _message(
        "Reset Complete",
        "Your document upload count has been reset to 0.\n"
        "You can now close this window and continue using DMS.",
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Windows PC Optimizer
=====================
A safe, interactive command-line tool to speed up a slow Windows computer by:
  1. Cleaning junk/temp files (Windows Temp, user Temp, Prefetch, Recycle Bin, browser caches)
  2. Reviewing and disabling unnecessary startup programs (with automatic backup/restore)
  3. Reviewing running processes by RAM usage and killing ones you choose to stop
  4. Reporting disk and memory usage

Design goals: nothing destructive happens without your confirmation. Every cleaning
action can be previewed first (dry run), and startup changes are backed up to a JSON
file so they can be restored later.

Usage:
    python pc_optimizer.py                # interactive menu
    python pc_optimizer.py --scan         # just report junk size / startup items / top processes, no changes
    python pc_optimizer.py --clean-junk   # clean junk files (asks for confirmation unless --yes)
    python pc_optimizer.py --yes          # skip confirmation prompts (use with care)

Requires only the Python standard library. Works fully only on Windows; on other
platforms the junk-file scan still runs against whatever temp folders exist.
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
BACKUP_FILE = Path(__file__).with_name("pc_optimizer_startup_backup.json")
LOG_FILE = Path(__file__).with_name("pc_optimizer_log.txt")

# Processes that should never be offered for termination — killing these can
# crash or lock up Windows.
CRITICAL_PROCESSES = {
    "system", "system idle process", "registry",
    "wininit.exe", "csrss.exe", "smss.exe", "services.exe", "lsass.exe",
    "winlogon.exe", "explorer.exe", "svchost.exe", "dwm.exe", "sihost.exe",
    "fontdrvhost.exe", "taskhostw.exe", "spoolsv.exe", "ctfmon.exe",
    "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe", "conhost.exe",
    "wmiprvse.exe", "wmiprvse", "audiodg.exe", "userinit.exe", "logonui.exe",
}


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def confirm(prompt, auto_yes=False):
    if auto_yes:
        return True
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer == "y"


# ---------------------------------------------------------------------------
# 1. Junk file cleaning
# ---------------------------------------------------------------------------

def junk_targets():
    """Return list of (label, path) candidate junk directories that exist."""
    candidates = []
    env = os.environ

    candidates.append(("User Temp (%TEMP%)", env.get("TEMP")))
    candidates.append(("User Temp (%TMP%)", env.get("TMP")))

    if IS_WINDOWS:
        windir = env.get("WINDIR", r"C:\Windows")
        candidates.append(("Windows Temp", os.path.join(windir, "Temp")))
        candidates.append(("Prefetch", os.path.join(windir, "Prefetch")))
        local_appdata = env.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(("Local AppData Temp", os.path.join(local_appdata, "Temp")))
            candidates.append(("Chrome Cache", os.path.join(
                local_appdata, "Google", "Chrome", "User Data", "Default", "Cache")))
            candidates.append(("Edge Cache", os.path.join(
                local_appdata, "Microsoft", "Edge", "User Data", "Default", "Cache")))
        appdata = env.get("APPDATA")
        if appdata:
            candidates.append(("Firefox Cache", os.path.join(
                appdata, "..", "Local", "Mozilla", "Firefox", "Profiles")))

    # keep only ones that actually exist
    return [(label, path) for label, path in candidates if path and os.path.isdir(path)]


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def clean_folder(path, dry_run=True):
    """Delete files/subfolders inside `path` (not the folder itself).
    Returns (bytes_freed, files_removed, files_skipped)."""
    freed = 0
    removed = 0
    skipped = 0
    for entry in os.scandir(path):
        try:
            if entry.is_file(follow_symlinks=False):
                size = entry.stat().st_size
                if not dry_run:
                    os.remove(entry.path)
                freed += size
                removed += 1
            elif entry.is_dir(follow_symlinks=False):
                sub_freed, sub_removed, sub_skipped = clean_folder(entry.path, dry_run)
                freed += sub_freed
                removed += sub_removed
                skipped += sub_skipped
                if not dry_run:
                    try:
                        os.rmdir(entry.path)
                    except OSError:
                        pass
        except (PermissionError, OSError):
            # file in use / locked by a running program — skip it, don't crash
            skipped += 1
    return freed, removed, skipped


def scan_junk():
    print("\nScanning for junk files...")
    results = []
    for label, path in junk_targets():
        size = folder_size(path)
        results.append((label, path, size))
        print(f"  {label:<22} {path:<70} {human_size(size)}")
    total = sum(r[2] for r in results)
    print(f"\nTotal reclaimable (approx): {human_size(total)}")
    return results


def clean_junk(auto_yes=False, dry_run=False):
    results = scan_junk()
    total = sum(r[2] for r in results)
    if total == 0:
        print("Nothing to clean.")
        return
    if not confirm(f"\nDelete these junk files now? ({human_size(total)})", auto_yes):
        print("Skipped.")
        return
    grand_freed = 0
    grand_skipped = 0
    for label, path, _size in results:
        freed, removed, skipped = clean_folder(path, dry_run=dry_run)
        grand_freed += freed
        grand_skipped += skipped
        log(f"Cleaned '{label}' ({path}): freed {human_size(freed)}, "
            f"{removed} items removed, {skipped} skipped (in use)")
    print(f"\nDone. Freed {human_size(grand_freed)}"
          f"{' (skipped ' + str(grand_skipped) + ' locked items)' if grand_skipped else ''}.")


def empty_recycle_bin(auto_yes=False):
    if not IS_WINDOWS:
        print("Recycle Bin emptying is only available on Windows.")
        return
    if not confirm("Empty the Recycle Bin now?", auto_yes):
        print("Skipped.")
        return
    SHERB_NOCONFIRMATION = 0x00000001
    SHERB_NOPROGRESSUI = 0x00000002
    SHERB_NOSOUND = 0x00000004
    flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
    # result is 0 on success, or a non-zero HRESULT (e.g. already empty)
    if result in (0, -2147418113 & 0xFFFFFFFF):
        log("Recycle Bin emptied.")
    else:
        log(f"Recycle Bin empty returned code {result} (may already be empty).")


# ---------------------------------------------------------------------------
# 2. Startup program management
# ---------------------------------------------------------------------------

def list_startup_items():
    """Return list of dicts: {source, hive, subkey, name, command}."""
    items = []
    if not IS_WINDOWS:
        return items

    import winreg

    run_locations = [
        ("HKCU\\Run", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ("HKLM\\Run", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ("HKLM\\Run (Wow6432Node)", winreg.HKEY_LOCAL_MACHINE,
         r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for source, hive, subkey in run_locations:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        name, value, _type = winreg.EnumValue(key, i)
                        items.append({
                            "source": source, "hive": hive, "subkey": subkey,
                            "name": name, "command": value, "kind": "registry",
                        })
                        i += 1
                    except OSError:
                        break
        except OSError:
            continue

    # Startup folders (shortcuts)
    startup_folders = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    if appdata:
        startup_folders.append(("User Startup Folder",
                                 os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                                              "Programs", "Startup")))
    if programdata:
        startup_folders.append(("All Users Startup Folder",
                                 os.path.join(programdata, "Microsoft", "Windows", "Start Menu",
                                              "Programs", "Startup")))
    for label, folder in startup_folders:
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                full = os.path.join(folder, name)
                if os.path.isfile(full):
                    items.append({
                        "source": label, "path": full,
                        "name": name, "command": full, "kind": "shortcut",
                    })
    return items


def backup_startup_item(item):
    backups = []
    if BACKUP_FILE.exists():
        try:
            backups = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            backups = []
    backups.append({**item, "hive": None, "disabled_at": datetime.now().isoformat()})
    BACKUP_FILE.write_text(json.dumps(backups, indent=2), encoding="utf-8")


def disable_startup_item(item):
    import winreg
    if item["kind"] == "registry":
        backup_startup_item(item)
        try:
            with winreg.OpenKey(item["hive"], item["subkey"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, item["name"])
            log(f"Disabled startup entry '{item['name']}' ({item['source']})")
            return True
        except OSError as e:
            log(f"Failed to disable '{item['name']}': {e}")
            return False
    else:  # shortcut -> move to a "disabled" subfolder instead of deleting
        disabled_dir = Path(item["path"]).parent / "Disabled_by_pc_optimizer"
        disabled_dir.mkdir(exist_ok=True)
        try:
            backup_startup_item(item)
            dest = disabled_dir / os.path.basename(item["path"])
            os.replace(item["path"], dest)
            log(f"Moved startup shortcut '{item['name']}' to {disabled_dir}")
            return True
        except OSError as e:
            log(f"Failed to move '{item['name']}': {e}")
            return False


def review_startup(auto_yes=False):
    items = list_startup_items()
    if not items:
        print("No startup items found (or not running on Windows).")
        return
    print("\nStartup programs:")
    for idx, item in enumerate(items, 1):
        print(f"  [{idx}] {item['name']:<30} ({item['source']})")
        print(f"        {item['command']}")

    choice = input(
        "\nEnter numbers to disable (comma-separated), or press Enter to skip: "
    ).strip()
    if not choice:
        return
    try:
        indices = {int(x.strip()) for x in choice.split(",") if x.strip()}
    except ValueError:
        print("Invalid input, no changes made.")
        return

    for idx in sorted(indices):
        if 1 <= idx <= len(items):
            item = items[idx - 1]
            if confirm(f"Disable '{item['name']}'?", auto_yes):
                disable_startup_item(item)


def restore_startup():
    if not BACKUP_FILE.exists():
        print("No backup file found — nothing to restore.")
        return
    import winreg
    backups = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
    restored = []
    for item in backups:
        if item["kind"] == "registry":
            hive = winreg.HKEY_CURRENT_USER if "HKCU" in item["source"] else winreg.HKEY_LOCAL_MACHINE
            try:
                with winreg.OpenKey(hive, item["subkey"], 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, item["name"], 0, winreg.REG_SZ, item["command"])
                log(f"Restored startup entry '{item['name']}'")
                restored.append(item)
            except OSError as e:
                log(f"Failed to restore '{item['name']}': {e}")
        else:
            disabled_dir = Path(item["path"]).parent / "Disabled_by_pc_optimizer"
            moved_path = disabled_dir / os.path.basename(item["path"])
            if moved_path.exists():
                try:
                    os.replace(moved_path, item["path"])
                    log(f"Restored startup shortcut '{item['name']}'")
                    restored.append(item)
                except OSError as e:
                    log(f"Failed to restore '{item['name']}': {e}")
    remaining = [b for b in backups if b not in restored]
    BACKUP_FILE.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
    print(f"Restored {len(restored)} item(s).")


# ---------------------------------------------------------------------------
# 3. Running process review
# ---------------------------------------------------------------------------

def list_processes():
    """Return list of dicts {name, pid, mem_kb} using the built-in `tasklist` command."""
    if not IS_WINDOWS:
        print("Process listing via tasklist is only available on Windows.")
        return []
    try:
        output = subprocess.check_output(
            ["tasklist", "/fo", "csv", "/nh"], text=True, encoding="mbcs", errors="ignore"
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Could not list processes: {e}")
        return []

    import csv
    import io
    processes = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 5:
            continue
        name, pid, _session, _sess_num, mem = row[:5]
        try:
            mem_kb = int(mem.replace(",", "").replace(" K", "").strip())
        except ValueError:
            mem_kb = 0
        try:
            pid_int = int(pid)
        except ValueError:
            continue
        processes.append({"name": name, "pid": pid_int, "mem_kb": mem_kb})
    return processes


def review_processes(top_n=20, auto_yes=False):
    processes = list_processes()
    if not processes:
        return
    processes.sort(key=lambda p: p["mem_kb"], reverse=True)
    top = processes[:top_n]

    print(f"\nTop {len(top)} processes by memory usage:")
    for idx, p in enumerate(top, 1):
        flag = "  [protected]" if p["name"].lower() in CRITICAL_PROCESSES else ""
        print(f"  [{idx}] {p['name']:<28} PID {p['pid']:<8} {human_size(p['mem_kb'] * 1024)}{flag}")

    choice = input(
        "\nEnter numbers to terminate (comma-separated), or press Enter to skip: "
    ).strip()
    if not choice:
        return
    try:
        indices = {int(x.strip()) for x in choice.split(",") if x.strip()}
    except ValueError:
        print("Invalid input, no changes made.")
        return

    for idx in sorted(indices):
        if 1 <= idx <= len(top):
            p = top[idx - 1]
            if p["name"].lower() in CRITICAL_PROCESSES:
                print(f"Refusing to terminate protected process '{p['name']}'.")
                continue
            if confirm(f"Terminate '{p['name']}' (PID {p['pid']})?", auto_yes):
                kill_process(p["pid"])


def kill_process(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True,
                        capture_output=True, text=True)
        log(f"Terminated process PID {pid}")
    except subprocess.CalledProcessError as e:
        log(f"Failed to terminate PID {pid}: {e.stderr.strip() if e.stderr else e}")


# ---------------------------------------------------------------------------
# 4. Disk / memory report
# ---------------------------------------------------------------------------

def system_report():
    import shutil as sh
    print("\nDisk usage:")
    drives = ["C:\\"] if IS_WINDOWS else ["/"]
    for drive in drives:
        try:
            total, used, free = sh.disk_usage(drive)
            pct_used = used / total * 100
            print(f"  {drive}  {human_size(used)} used / {human_size(total)} total "
                  f"({pct_used:.0f}% full, {human_size(free)} free)")
        except OSError:
            pass

    if IS_WINDOWS:
        try:
            output = subprocess.check_output(
                ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/format:list"],
                text=True, errors="ignore"
            )
            values = dict(
                line.split("=", 1) for line in output.strip().splitlines() if "=" in line
            )
            free_kb = int(values.get("FreePhysicalMemory", 0))
            total_kb = int(values.get("TotalVisibleMemorySize", 0))
            if total_kb:
                used_kb = total_kb - free_kb
                print(f"\nMemory: {human_size(used_kb * 1024)} used / "
                      f"{human_size(total_kb * 1024)} total "
                      f"({used_kb / total_kb * 100:.0f}% full)")
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass  # wmic may be unavailable on newer Windows builds; not critical


# ---------------------------------------------------------------------------
# Menu / CLI
# ---------------------------------------------------------------------------

def interactive_menu(auto_yes):
    while True:
        print("\n" + "=" * 60)
        print("  Windows PC Optimizer")
        print("=" * 60)
        print("  1) Scan junk files (no changes)")
        print("  2) Clean junk files")
        print("  3) Empty Recycle Bin")
        print("  4) Review & disable startup programs")
        print("  5) Restore previously disabled startup programs")
        print("  6) Review & terminate heavy background processes")
        print("  7) Disk / memory report")
        print("  8) Run everything (scan -> clean -> startup -> processes -> report)")
        print("  0) Exit")
        choice = input("Select an option: ").strip()

        if choice == "1":
            scan_junk()
        elif choice == "2":
            clean_junk(auto_yes=auto_yes)
        elif choice == "3":
            empty_recycle_bin(auto_yes=auto_yes)
        elif choice == "4":
            review_startup(auto_yes=auto_yes)
        elif choice == "5":
            restore_startup()
        elif choice == "6":
            review_processes(auto_yes=auto_yes)
        elif choice == "7":
            system_report()
        elif choice == "8":
            scan_junk()
            clean_junk(auto_yes=auto_yes)
            empty_recycle_bin(auto_yes=auto_yes)
            review_startup(auto_yes=auto_yes)
            review_processes(auto_yes=auto_yes)
            system_report()
        elif choice == "0":
            print("Goodbye.")
            break
        else:
            print("Invalid choice.")


def main():
    parser = argparse.ArgumentParser(description="Windows PC Optimizer")
    parser.add_argument("--scan", action="store_true", help="Report junk size / startup / processes, no changes")
    parser.add_argument("--clean-junk", action="store_true", help="Clean junk files")
    parser.add_argument("--empty-recycle-bin", action="store_true", help="Empty the Recycle Bin")
    parser.add_argument("--restore-startup", action="store_true", help="Restore previously disabled startup items")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    if not IS_WINDOWS:
        print("Note: this script is designed for Windows. Some features (Recycle Bin, "
              "startup registry, process listing) are unavailable on this platform; "
              "junk-file scanning of TEMP/TMP will still work.\n")

    if args.scan:
        scan_junk()
        review_startup_scan_only = list_startup_items()
        if review_startup_scan_only:
            print("\nStartup items found (use interactive mode to disable):")
            for item in review_startup_scan_only:
                print(f"  - {item['name']} ({item['source']})")
        system_report()
        return
    if args.clean_junk:
        clean_junk(auto_yes=args.yes)
        return
    if args.empty_recycle_bin:
        empty_recycle_bin(auto_yes=args.yes)
        return
    if args.restore_startup:
        restore_startup()
        return

    interactive_menu(auto_yes=args.yes)


if __name__ == "__main__":
    main()

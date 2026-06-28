"""
DMS Build Script
================

Packages the DMS into a self-contained executable that doesn't require Python
to be installed on the user's machine.

Usage:
    python3 build.py

What this produces:
    On Mac:     dist/DMS.app  (a normal Mac application bundle)
    On Windows: dist/DMS.exe  (a single-file Windows executable)
    On Linux:   dist/DMS      (a single-file Linux binary)

Important:
    PyInstaller can only build for the OS it's running on. To produce a Windows
    .exe, run this script on a Windows machine. To produce a Mac .app, run it
    on a Mac. There's no cross-compilation.

Prerequisites:
    pip install pyinstaller flask pypdf reportlab Pillow

How users install the result:
    Mac:     drag DMS.app to /Applications
    Windows: copy DMS.exe wherever you like
    Linux:   chmod +x DMS && put it in ~/Applications or similar
"""
from __future__ import annotations

import calendar
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "DMS"

# Files that must be packaged alongside the Python code.
# Each entry is either a filename (goes to the _MEIPASS root)
# or a (src, dest) tuple where dest is the subfolder inside _MEIPASS.
DATA_FILES = [
    ("dms.html", "."),             # dms.html → _MEIPASS/dms.html
    ("vendor", "vendor"),          # vendor/ dir → _MEIPASS/vendor/ (offline JS)
    ("deepface_worker.py", "."),   # deepface subprocess worker
]

# Icon files (optional). Add an .icns for Mac and .ico for Windows if you have them.
MAC_ICON = HERE / "icon.icns"
WIN_ICON = HERE / "icon.ico"


def ensure_venv():
    """If not running inside .build_venv, create it, install deps, and re-exec."""
    venv_dir = HERE / ".build_venv"
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return  # already inside .build_venv

    venv_python = venv_dir / "bin" / "python3"
    if sys.platform.startswith("win"):
        venv_python = venv_dir / "Scripts" / "python.exe"

    if not venv_python.exists():
        print("Creating build virtual environment at .build_venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        print("Installing build dependencies into venv ...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade",
             "pip", "pyinstaller", "flask", "waitress", "pypdf", "reportlab", "Pillow",
             "pillow-heif", "PyMuPDF", "qrcode", "numpy"],
            check=True,
        )

    print("Re-launching build inside virtual environment ...\n")
    result = subprocess.run([str(venv_python)] + sys.argv)
    sys.exit(result.returncode)


def check_pyinstaller() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def install_pyinstaller():
    print("PyInstaller is not installed. Installing now...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"],
        check=True,
    )


def check_dependencies():
    """Verify all third-party packages are installed before building.

    Without this check, PyInstaller emits cryptic warnings and produces
    an executable that crashes on launch with 'Flask is not installed'.
    Better to fail fast with a clear actionable message.
    """
    required = {
        "flask": "flask",
        "waitress": "waitress",
        "pypdf": "pypdf",
        "reportlab": "reportlab",
        "PIL": "Pillow",
    }
    missing = []
    for import_name, pkg_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print()
        print("=" * 60)
        print("  ERROR: missing required Python packages")
        print("=" * 60)
        print()
        print("  These packages must be installed in this Python before building:")
        print(f"      {', '.join(missing)}")
        print()
        print("  Install them with:")
        print()
        if sys.platform.startswith("win"):
            print(f"      python -m pip install {' '.join(missing)}")
        else:
            print(f"      python3 -m pip install {' '.join(missing)}")
        print()
        print("  Then run this build script again.")
        print("=" * 60)
        sys.exit(1)


def _on_rm_error(func, path, _exc):
    """rmtree error handler: make the entry (and its parent) writable, retry.

    Removal can fail for two reasons we can recover from:
      • the entry is read-only (common with PyInstaller output on Windows), or
      • its *parent directory* isn't writable, so the entry can't be unlinked
        (this is the macOS 'Permission denied' on the dir itself).
    We clear the read-only bit on both the entry and its parent, then retry.

    `func` is whatever rmtree was calling (os.unlink, os.rmdir, os.open, ...).
    On macOS's fd-based rmtree, func can be os.open, which takes extra args we
    don't have — so if retrying func(path) raises TypeError we fall back to a
    plain unlink/rmdir. Any failure here is swallowed; the caller re-checks
    existence and decides whether to keep trying.
    """
    for target in (path, os.path.dirname(str(path))):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR
                     | stat.S_IRWXG | stat.S_IRWXO)
        except OSError:
            pass
    try:
        func(path)
        return
    except (OSError, TypeError):
        pass
    # Fallback that doesn't depend on which func rmtree handed us.
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        else:
            os.unlink(path)
    except OSError:
        pass


def _force_rmtree(p: Path, attempts: int = 4) -> bool:
    """Remove a directory tree robustly, retrying on transient locks.

    Returns True if the tree is gone, False if it can't be removed (e.g. a
    running DMS app/exe holding files, or a directory the user can't write).
    """
    for i in range(attempts):
        if not p.exists():
            return True
        # Proactively make the whole tree writable so unlink/rmdir can proceed.
        try:
            for root, dirs, files in os.walk(p):
                for name in dirs + files:
                    try:
                        os.chmod(os.path.join(root, name),
                                 stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
                    except OSError:
                        pass
            os.chmod(p, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        except OSError:
            pass
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(p, onexc=_on_rm_error)
            else:
                shutil.rmtree(p, onerror=lambda f, pth, ei: _on_rm_error(f, pth, ei))
        except (OSError, TypeError):
            pass
        if not p.exists():
            return True
        # A lock may release a moment after a process exits or AV finishes a
        # scan of the freshly built artifact — wait and retry.
        time.sleep(0.6 * (i + 1))
    return not p.exists()


def clean_previous_builds():
    is_win = sys.platform.startswith("win")
    # Kill any lingering cloudflared process that may lock files inside dist.
    if is_win:
        subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
    for d in ("build", "dist"):
        p = HERE / d
        if p.exists():
            print(f"Cleaning {p}...")
            if not _force_rmtree(p):
                # If the folder is empty it's locked by the OS (Search Indexer,
                # antivirus, IDE file watcher, etc.) but contains nothing that
                # would conflict with PyInstaller writing fresh output into it.
                # Treat an empty locked folder as a non-fatal warning and let
                # PyInstaller proceed — it will create the output file inside it.
                contents = list(p.iterdir()) if p.exists() else []
                if not contents:
                    print(f"  Warning: could not remove empty {p} (OS lock). "
                          f"Continuing — PyInstaller will write into it.")
                    continue

                is_mac = sys.platform == "darwin"
                artifact = "DMS.exe" if is_win else ("DMS.app" if is_mac else "DMS")
                print(f"ERROR: Could not remove {p}.")
                print(f"  Most likely the previous {artifact} is still running, or")
                print(f"  the folder's permissions block deletion.")
                print("  Please:")
                print(f"    1. Quit any running {artifact}.")
                if is_win:
                    print("       (Windows: check Task Manager for a 'DMS' process, End task.)")
                else:
                    print(f"       (macOS/Linux: run  pkill -f {artifact}  or quit it from the Dock.)")
                print(f"    2. Close any Finder/Explorer window showing '{p.name}'.")
                if not is_win:
                    print(f"    3. If it's a permissions problem, fix ownership/permissions, e.g.:")
                    print(f"         sudo chflags -R nouchg '{p}' 2>/dev/null; chmod -R u+rwx '{p}'")
                    print(f"       or simply delete it yourself:  rm -rf '{p}'")
                else:
                    print("    3. Re-run this build.")
                print("  Then run this build again.")
                sys.exit(1)
    spec = HERE / f"{APP_NAME}.spec"
    if spec.exists():
        try:
            spec.unlink()
        except OSError:
            try:
                os.chmod(spec, stat.S_IWRITE)
                spec.unlink()
            except OSError:
                pass


def _ensure_cloudflared_windows() -> Path:
    """Download cloudflared.exe into the project directory for bundling (cached after first run)."""
    import urllib.request
    dest = HERE / "cloudflared.exe"
    if dest.exists():
        print(f"  cloudflared.exe already cached ({dest})")
        return dest
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    print(f"  Downloading cloudflared.exe (~35 MB) for bundling…")
    urllib.request.urlretrieve(url, dest)
    print(f"  Saved to {dest}")
    return dest


def build_pyinstaller_command() -> list[str]:
    """Compose the platform-specific PyInstaller command."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--clean",
        "--noconfirm",
    ]

    if sys.platform == "darwin":
        # On Mac: --windowed produces a .app bundle (no Terminal opens)
        cmd.append("--windowed")
        # A stable bundle identifier prevents macOS from confusing multiple
        # builds or versions of DMS when one is already running.
        cmd.extend(["--osx-bundle-identifier", "com.david.qcdms"])
        # Single-file is unusual on Mac (apps are normally bundles), so we
        # let PyInstaller build a proper .app
        if MAC_ICON.exists():
            cmd.extend(["--icon", str(MAC_ICON)])
    elif sys.platform.startswith("win"):
        # On Windows: --windowed prevents a console window from appearing
        cmd.append("--windowed")
        cmd.append("--onefile")  # Single .exe is much friendlier on Windows
        if WIN_ICON.exists():
            cmd.extend(["--icon", str(WIN_ICON)])
        # Bundle cloudflared.exe so users never need to download it at runtime.
        cf_path = HERE / "cloudflared.exe"
        if cf_path.exists():
            cmd.extend(["--add-binary", f"{cf_path};."])
    else:
        # Linux: single-file binary
        cmd.append("--onefile")

    # Bundle data files and directories alongside the code
    sep = ";" if sys.platform.startswith("win") else ":"
    for item in DATA_FILES:
        src, dest = item if isinstance(item, tuple) else (item, ".")
        src_path = HERE / src
        if not src_path.exists():
            print(f"WARNING: data file/dir not found: {src}")
            continue
        cmd.extend(["--add-data", f"{src_path}{sep}{dest}"])

    # Tkinter: only collect the submodules actually used; avoid pulling in the
    # entire Tcl/Tk test suite and demos which inflate the bundle and VM footprint.
    for mod in ("tkinter", "tkinter.ttk", "tkinter.scrolledtext",
                "tkinter.font", "tkinter.messagebox"):
        cmd.extend(["--collect-submodules", mod])

    # --collect-submodules compiles packages to bytecode (PYZ archive) so
    # macOS does NOT memory-map thousands of loose source files at runtime.
    # --collect-all was tried earlier but caused ~93 GB virtual-memory usage
    # because it copies every .py/.html/data file and Python maps them all.
    for pkg in ("flask", "werkzeug", "jinja2", "click", "itsdangerous",
                "markupsafe", "waitress", "pypdf", "reportlab", "PIL"):
        cmd.extend(["--collect-submodules", pkg])
    # PIL data files (fonts, image format plugins) still need to be present.
    cmd.extend(["--copy-metadata", "Pillow"])

    # pillow_heif is OPTIONAL — enables HEIC/HEIF photo preview in the browser.
    # Without it, HEIC files return a 500 error from the /api/docs/<id>/preview route.
    try:
        import pillow_heif  # noqa: F401  # type: ignore
        cmd.extend(["--collect-submodules", "pillow_heif"])
        cmd.extend(["--collect-all", "pillow_heif"])
        cmd.extend(["--hidden-import", "pillow_heif"])
        print("  pillow_heif found — HEIC/HEIF photo preview will work in the build.")
    except ImportError:
        print("  pillow_heif not installed — HEIC photo preview unavailable. "
              "Run: pip install pillow-heif")

    # PyMuPDF (fitz) is OPTIONAL — it lets OCR rasterize scanned PDFs. Image
    # OCR and text-layer PDF extraction work without it, so we only bundle it
    # when it's actually installed in the build environment.
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        cmd.extend(["--collect-submodules", "fitz"])
        cmd.extend(["--hidden-import", "fitz"])
        print("  PyMuPDF found — scanned-PDF OCR will be available in the build.")
    except ImportError:
        print("  PyMuPDF not installed — scanned-PDF OCR omitted "
              "(image OCR + text-PDF extraction still work).")

    # qrcode is used by the launcher's Mobile Upload dialog to display a QR code.
    try:
        import qrcode  # noqa: F401  # type: ignore
        cmd.extend(["--collect-submodules", "qrcode"])
        cmd.extend(["--hidden-import", "qrcode"])
        print("  qrcode found — Mobile Upload QR codes will be available.")
    except ImportError:
        print("  qrcode not installed — Mobile Upload will show URL only (no QR code).")

    # pyngrok is used by the Remote Upload dialog to create a public tunnel.
    # The ngrok binary itself is downloaded to ~/.ngrok2/ at runtime, so it
    # does not need to be bundled inside the .app.
    try:
        import pyngrok  # noqa: F401  # type: ignore
        cmd.extend(["--collect-submodules", "pyngrok"])
        cmd.extend(["--hidden-import", "pyngrok"])
        cmd.extend(["--hidden-import", "pyngrok.ngrok"])
        cmd.extend(["--hidden-import", "pyngrok.conf"])
        print("  pyngrok found — Remote Upload via ngrok will be available.")
    except ImportError:
        print("  pyngrok not installed — Remote Upload will not be available in the built app.")

    # Only exclude large third-party packages that are definitely unused.
    # Stdlib exclusions are risky — http/email/uu modules form an import chain
    # that werkzeug pulls in at module level, so excluding any one of them
    # causes a ModuleNotFoundError inside Flask's own __init__.
    for mod in ("pandas", "scipy", "matplotlib",
                "idlelib", "turtle", "turtledemo", "lib2to3"):
        cmd.extend(["--exclude-module", mod])

    # numpy is used by _cosine_sim for face recognition
    try:
        import numpy  # noqa: F401
        cmd.extend(["--collect-submodules", "numpy"])
        cmd.extend(["--hidden-import", "numpy"])
        print("  numpy found — face recognition cosine similarity will work.")
    except ImportError:
        print("  numpy not installed — face recognition will be disabled.")

    # Belt-and-suspenders hidden imports
    for mod in ("tkinter", "tkinter.ttk", "tkinter.scrolledtext",
                "tkinter.font", "tkinter.messagebox",
                "flask", "dms_server", "databook", "pdf_extraction",
                "_dms_trial"):
        cmd.extend(["--hidden-import", mod])

    # The entry point is the launcher
    cmd.append(str(HERE / "dms_launcher.py"))
    return cmd


def prompt_trial_period():
    """Ask how many months the trial lasts and return the ISO expiry date (or None)."""
    print()
    print("=" * 60)
    print("  Trial period setup")
    print("=" * 60)
    while True:
        raw = input("  Enter trial period in months (0 = no expiry): ").strip()
        if not raw:
            raw = "0"
        try:
            months = int(raw)
            if months < 0:
                print("  Please enter 0 or a positive number.")
                continue
            break
        except ValueError:
            print("  Please enter a whole number.")

    if months == 0:
        print("  No trial period — this build will never expire.")
        print()
        expiry_iso = None
    else:
        today = date.today()
        m = today.month - 1 + months
        year = today.year + m // 12
        month = m % 12 + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        expiry = date(year, month, day)
        print(f"  Build date : {today.strftime('%B %d, %Y')}")
        print(f"  Expiry date: {expiry.strftime('%B %d, %Y')}  ({months} month{'s' if months != 1 else ''})")
        print()
        expiry_iso = expiry.isoformat()

    while True:
        raw = input("  Enter maximum number of launches (0 = unlimited): ").strip()
        if not raw:
            raw = "0"
        try:
            max_launches = int(raw)
            if max_launches < 0:
                print("  Please enter 0 or a positive number.")
                continue
            break
        except ValueError:
            print("  Please enter a whole number.")

    if max_launches == 0:
        print("  No launch limit.")
    else:
        print(f"  Max launches: {max_launches}")
    print()

    return expiry_iso, max_launches


def write_trial_module(expiry_iso, max_launches=0):
    """Write _dms_trial.py which gets bundled into the app by PyInstaller."""
    content = (
        "# Auto-generated by build.py — do not edit manually.\n"
        f"EXPIRY = {repr(expiry_iso)}\n"
        f"MAX_LAUNCHES = {max_launches!r}\n"
    )
    (HERE / "_dms_trial.py").write_text(content)


def main():
    ensure_venv()

    print(f"DMS build for {sys.platform}")
    print(f"Working directory: {HERE}")
    print()

    if not check_pyinstaller():
        install_pyinstaller()

    check_dependencies()

    # On Windows, download cloudflared.exe now so it can be bundled into DMS.exe.
    if sys.platform.startswith("win"):
        _ensure_cloudflared_windows()

    # Sanity check — make sure all required files exist
    required = ["dms_launcher.py", "dms_server.py", "databook.py", "dms.html"]
    missing = [f for f in required if not (HERE / f).exists()]
    if missing:
        print(f"ERROR: missing required source files: {missing}")
        sys.exit(1)

    expiry, max_launches = prompt_trial_period()
    write_trial_module(expiry, max_launches)

    # Reset the persisted launch counter so the new build starts from zero.
    # The counter lives in ~/.pms_dms_trial.json on the build machine; without
    # this deletion every rebuild inherits the count accumulated during testing.
    trial_state_file = Path.home() / ".pms_dms_trial.json"
    if trial_state_file.exists():
        trial_state_file.unlink()
        print(f"  Reset trial state ({trial_state_file})")

    clean_previous_builds()

    cmd = build_pyinstaller_command()
    print("Running:")
    print("  " + " ".join(cmd))
    print()

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\nBuild failed with exit code {e.returncode}")
        sys.exit(1)

    # Tell the user where the result is
    print()
    print("=" * 60)
    print("  BUILD COMPLETE")
    print("=" * 60)

    dist = HERE / "dist"
    if sys.platform == "darwin":
        target = dist / f"{APP_NAME}.app"
        print(f"  Mac app bundle: {target}")
        print()
        print("  To test:")
        print(f"    open '{target}'")
        print()
        print("  To install: drag DMS.app to /Applications")
        print()
        print("  Note: macOS will warn 'unidentified developer' on first launch.")
        print("  Right-click the app → Open, then click Open in the dialog.")
    elif sys.platform.startswith("win"):
        target = dist / f"{APP_NAME}.exe"
        print(f"  Windows executable: {target}")
        print()
        print("  Just double-click DMS.exe to run.")
        print()
        print("  Note: Windows SmartScreen may flag the .exe on first launch")
        print("  because it's unsigned. Click 'More info' → 'Run anyway'.")
    else:
        target = dist / APP_NAME
        print(f"  Linux binary: {target}")
        print(f"  Run with: ./{APP_NAME}")

    print()
    print("  The executable is fully self-contained — Python is NOT")
    print("  required on the target machine.")
    print("=" * 60)


if __name__ == "__main__":
    main()

"""
DMS Application Launcher
========================

Entry point for the packaged Mac (.app) and Windows (.exe) builds.

1. Starts the DMS server (via waitress) in a background thread.
2. Shows a minimal Tkinter status window.
3. Opens the user's default browser to http://localhost:<port>.

Every standard quit path closes the server and exits the process:
  • Red X button on the window
  • "Quit DMS" button in the window
  • Cmd+Q  (macOS keyboard shortcut)
  • Dock → Quit  (macOS dock right-click)
  • SIGTERM / SIGINT  (kill from terminal or system shutdown)
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Resource resolution — works both in dev and inside a PyInstaller bundle
# ---------------------------------------------------------------------------
def resource_path(*parts: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = Path(__file__).resolve().parent
    return Path(base, *parts)


os.environ["DMS_RESOURCE_DIR"] = str(resource_path())


# ---------------------------------------------------------------------------
# Trial period check — runs before anything else starts
# ---------------------------------------------------------------------------
_TRIAL_CONFIG = Path.home() / ".pms_dms_trial.json"


def _load_trial_state() -> dict:
    try:
        import json
        return json.loads(_TRIAL_CONFIG.read_text())
    except Exception:
        return {}


def _save_trial_state(state: dict) -> None:
    import json
    try:
        _TRIAL_CONFIG.write_text(json.dumps(state))
    except Exception:
        pass


def _show_trial_ended_window(message: str) -> None:
    root = tk.Tk()
    root.title("QCDMS – Trial Limit Reached")
    root.resizable(False, False)

    BG  = "#fafaf9"
    FG  = "#1c1917"
    BLU = "#0064c8"

    root.configure(bg=BG)

    tk.Label(
        root, text=message,
        font=("Helvetica", 12), bg=BG, fg=FG, justify="center",
    ).pack(padx=40, pady=(28, 8))

    email = "aatbinc@yahoo.com"
    lbl = tk.Label(
        root, text=email,
        font=("Helvetica", 12, "bold"), bg=BG, fg=BLU, cursor="hand2",
    )
    lbl.pack()
    lbl.bind("<Button-1>", lambda _e: webbrowser.open(f"mailto:{email}"))

    tk.Frame(root, bg="#e7e5e4", height=1).pack(fill="x", padx=30, pady=16)

    tk.Button(
        root, text="OK", command=lambda: os._exit(0),
        relief="groove", padx=20, pady=5,
    ).pack(pady=(0, 24))

    root.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))

    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()
    os._exit(0)


def check_trial_expiry() -> None:
    """If the date-based trial has expired, show a message and exit."""
    try:
        from _dms_trial import EXPIRY
    except ImportError:
        return

    if not EXPIRY:
        return

    if date.today() <= date.fromisoformat(EXPIRY):
        return

    _show_trial_ended_window(
        "Trial Limit Reached.\nContact for latest version:"
    )


def check_trial_launches() -> None:
    """If the launch-count limit has been reached, show a message and exit."""
    try:
        from _dms_trial import MAX_LAUNCHES
    except ImportError:
        return

    if not MAX_LAUNCHES:
        return

    state = _load_trial_state()
    count = state.get("launches", 0) + 1
    state["launches"] = count
    _save_trial_state(state)

    if count > MAX_LAUNCHES:
        _show_trial_ended_window(
            "Trial Limit Reached.\nContact for latest version:"
        )


check_trial_expiry()    # exits here if date expired
check_trial_launches()  # exits here if launch count exceeded


# ---------------------------------------------------------------------------
# Suppress noisy loggers before importing Flask / waitress
# ---------------------------------------------------------------------------
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("waitress").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Find an open port
# ---------------------------------------------------------------------------
def find_free_port(start: int = 8000, end: int = 8050) -> int:
    for p in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return p
        except OSError:
            continue
    raise RuntimeError(f"No free port available in {start}–{end}")


def get_all_local_ips() -> list[tuple[str, str]]:
    """Return list of (ip, interface_name) for all active non-loopback interfaces.

    Uses getifaddrs via socket.getaddrinfo alternative: iterates getnameinfo.
    Falls back to a platform-specific approach on macOS.
    """
    import subprocess
    ips: list[tuple[str, str]] = []
    try:
        # `ifconfig -a` is reliable on macOS/Linux
        out = subprocess.check_output(["ifconfig", "-a"], text=True, stderr=subprocess.DEVNULL)
        iface = ""
        for line in out.splitlines():
            # New interface block
            if line and line[0] not in (" ", "\t"):
                iface = line.split(":")[0]
            elif "inet " in line and "127." not in line:
                parts = line.split()
                idx = parts.index("inet")
                ip = parts[idx + 1]
                if ip and ip != "127.0.0.1":
                    ips.append((ip, iface))
    except Exception:
        pass

    if not ips:
        # Final fallback
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ips = [(s.getsockname()[0], "")]
        except Exception:
            ips = [("127.0.0.1", "")]

    return ips


def get_local_ip() -> str:
    """Return the best candidate LAN IP for mobile access.

    Prefers physical Wi-Fi / Ethernet IPs over VPN tunnel IPs.
    On macOS the Wi-Fi adapter is typically en0/en1; VPN tunnels appear
    as utun* interfaces and are skipped when a physical interface IP exists.
    """
    import subprocess
    physical_ips: list[str] = []
    all_ips: list[str] = []
    try:
        out = subprocess.check_output(["ifconfig", "-a"], text=True, stderr=subprocess.DEVNULL)
        iface = ""
        for line in out.splitlines():
            if line and line[0] not in (" ", "\t"):
                iface = line.split(":")[0]
            elif "inet " in line and "127." not in line:
                parts = line.split()
                idx = parts.index("inet")
                ip = parts[idx + 1]
                if ip and ip != "127.0.0.1":
                    all_ips.append(ip)
                    # en0/en1/en2 etc. are physical (Wi-Fi / Ethernet) on macOS
                    if iface.startswith("en"):
                        physical_ips.append(ip)
    except Exception:
        pass

    candidates = physical_ips or all_ips
    if candidates:
        return candidates[0]

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


PORT       = 8765  # fixed port so ngrok tunnel always forwards to the right place
URL        = f"http://127.0.0.1:{PORT}"
LOCAL_IP   = get_local_ip()
MOBILE_URL = f"http://{LOCAL_IP}:{PORT}/mobile"

# ---------------------------------------------------------------------------
# Tunnel state — one cloudflared tunnel per session, reused across dialogs
# ---------------------------------------------------------------------------
_tunnel_proc   = None   # subprocess.Popen for cloudflared
_tunnel_url    = None   # public HTTPS URL


def _load_ngrok_token() -> str:
    from dms_server import load_config
    return load_config().get("ngrok_auth_token", "")


def _save_ngrok_token(token: str) -> None:
    from dms_server import load_config, save_config
    cfg = load_config()
    cfg["ngrok_auth_token"] = token
    save_config(cfg)


def _download_cloudflared_windows(dest_path: str, status_fn=None) -> str:
    """Download cloudflared.exe to dest_path on Windows (one-time) and return the path."""
    import urllib.request, os as _os
    _os.makedirs(_os.path.dirname(dest_path), exist_ok=True)
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    if status_fn:
        status_fn("Downloading cloudflared… (one-time, ~35 MB)")
    urllib.request.urlretrieve(url, dest_path)
    return dest_path


def _get_or_start_tunnel(status_fn=None) -> str:
    """Return a public HTTPS URL via cloudflared, starting it if needed."""
    global _tunnel_proc, _tunnel_url
    import subprocess, re as _re, threading, os as _os, sys as _sys, shutil

    # Reuse existing tunnel if the process is still alive.
    if _tunnel_proc is not None and _tunnel_proc.poll() is None and _tunnel_url:
        return _tunnel_url

    # Kill any stale cloudflared left from a previous session.
    if _sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
    else:
        subprocess.run(["/usr/bin/pkill", "-f", "cloudflared"], capture_output=True)
    time.sleep(0.5)

    _tunnel_url = None

    # Locate cloudflared — check platform-specific well-known paths before
    # falling back to whatever is on PATH.
    if _sys.platform.startswith("win"):
        _local_appdata = _os.environ.get("LOCALAPPDATA") or _os.path.join(_os.path.expanduser("~"), "AppData", "Local")
        _local_cf = _os.path.join(_local_appdata, "cloudflared", "cloudflared.exe")
        _candidates = [
            r"C:\Program Files\cloudflared\cloudflared.exe",
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            _local_cf,
            "cloudflared.exe",
            "cloudflared",
        ]
        _install_hint = "Download cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ and add it to your PATH."
    else:
        _local_cf = None
        _candidates = ["/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared", "cloudflared"]
        _install_hint = "Run: brew install cloudflare/cloudflare/cloudflared"

    # Check for cloudflared bundled inside this PyInstaller build first.
    _cf = None
    if _sys.platform.startswith("win"):
        _meipass = getattr(_sys, '_MEIPASS', None)
        if _meipass:
            _bundled = _os.path.join(_meipass, "cloudflared.exe")
            if _os.path.isfile(_bundled):
                _cf = _bundled

    # Search well-known paths and PATH if not found in the bundle.
    if _cf is None:
        for p in _candidates:
            if _os.path.isfile(p):
                _cf = p
                break
            if _os.sep not in p and shutil.which(p):
                _cf = shutil.which(p)
                break

    # On Windows, auto-download cloudflared as a last resort (running from source).
    if _cf is None:
        if _sys.platform.startswith("win"):
            _cf = _download_cloudflared_windows(_local_cf, status_fn)
        else:
            raise RuntimeError(f"cloudflared not found. {_install_hint}")

    if status_fn:
        status_fn("Starting tunnel…")

    kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if _sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            [_cf, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate",
             "--protocol", "http2"],
            **kwargs,
        )
    except FileNotFoundError:
        raise RuntimeError(f"cloudflared not found. {_install_hint}")

    _tunnel_proc = proc

    # Read output lines until we find the public URL (printed within ~15 s).
    import queue as _queue
    q = _queue.Queue()

    def _reader():
        for line in proc.stdout:
            q.put(line)

    threading.Thread(target=_reader, daemon=True).start()

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            line = q.get(timeout=0.3)
            m = _re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
            if m:
                _tunnel_url = m.group(0)
                break
        except _queue.Empty:
            if proc.poll() is not None:
                raise RuntimeError(f"cloudflared exited unexpectedly. {_install_hint}")

    if not _tunnel_url:
        proc.kill()
        raise RuntimeError("cloudflared did not produce a URL within 15 seconds.")

    return _tunnel_url


def _send_imessage(phone: str, url: str) -> None:
    """Send *url* to *phone* via the Mac Messages app using AppleScript."""
    import subprocess

    # Normalise to E.164 (+1XXXXXXXXXX) so Messages can match the number.
    normalised = phone.strip()
    if not normalised.startswith("+"):
        normalised = "+" + normalised
    # Digits-only suffix for partial matching (handles +1XXXXXXXXXX vs XXXXXXXXXX)
    digits_suffix = normalised.lstrip("+")

    # Escape the URL for embedding in an AppleScript string literal.
    safe_url = url.replace("\\", "\\\\").replace('"', '\\"')

    # Strategy 1: find an existing chat containing this number and send to it.
    # Strategy 2: fall back to the buddy approach with normalised +phone.
    script = f"""
tell application "Messages"
    set _url to "{safe_url}"
    set _digits to "{digits_suffix}"
    set _norm to "{normalised}"
    set _sent to false

    -- Strategy 1: search existing chats for a participant matching the number
    repeat with c in chats
        try
            repeat with p in (participants of c)
                set h to handle of p
                if h ends with _digits or h is _norm then
                    send _url to c
                    set _sent to true
                    exit repeat
                end if
            end repeat
        end try
        if _sent then exit repeat
    end repeat

    -- Strategy 2: buddy lookup (works when the number is in contacts)
    if not _sent then
        repeat with svc in services
            set sType to service type of svc as string
            if sType is in {{"iMessage", "SMS", "RCS"}} then
                try
                    send _url to buddy _norm of svc
                    set _sent to true
                    exit repeat
                end try
                try
                    send _url to buddy _digits of svc
                    set _sent to true
                    exit repeat
                end try
            end if
        end repeat
    end if

    if not _sent then
        error "No Messages conversation found for " & _norm & ". Open Messages and start a conversation with this number first."
    end if
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "Messages app could not send the link. "
            "Is your iPhone linked to this Mac via Handoff/SMS Forwarding?"
        )


# ---------------------------------------------------------------------------
# Single-instance guard — check BEFORE importing Flask or starting any server
# ---------------------------------------------------------------------------
def _kill_stale_port_owner() -> None:
    """Windows only: kill whatever process holds PORT so we can rebind."""
    if not sys.platform.startswith("win"):
        return
    import subprocess
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            cols = line.split()
            # cols: Proto  Local  Foreign  State  PID
            if len(cols) >= 5 and f":{PORT}" in cols[1] and cols[3] == "LISTENING":
                pid = cols[4]
                if pid.isdigit() and pid != "0":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=3,
                    )
    except Exception:
        pass


def _already_running() -> bool:
    """Return True if a healthy DMS instance is already serving on PORT.

    Three cases:
      • Port closed              → not running          → return False
      • Port open + HTTP 200     → healthy instance     → return True
      • Port open + HTTP fails   → stale/stuck process  → kill it, return False
    """
    # Quick socket probe first (cheap).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", PORT))
        except OSError:
            return False  # nothing on that port

    # Port is open — confirm the server actually responds to HTTP.
    import urllib.request as _req
    try:
        with _req.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        # Port is bound but server is unresponsive (stuck/zombie).
        # Kill the stale process so the new instance can bind the port.
        _kill_stale_port_owner()
        return False


if _already_running():
    webbrowser.open(URL)
    _r = tk.Tk()
    _r.withdraw()
    import tkinter.messagebox as _mb
    _mb.showinfo(
        "DMS Already Running",
        f"DMS is already running.\n\nOpening browser to {URL}",
        parent=_r,
    )
    _r.destroy()
    os._exit(0)


# ---------------------------------------------------------------------------
# Import the Flask app (after DMS_RESOURCE_DIR is set)
# ---------------------------------------------------------------------------
from dms_server import app  # noqa: E402
import dms_server as _dms_server  # noqa: E402

# If a project folder was passed as a command-line argument, use it as the
# storage root for this instance only (does not touch ~/.dms_server_config.json).
_cli_project_path = sys.argv[1] if len(sys.argv) > 1 else ""
if _cli_project_path:
    p = Path(_cli_project_path).expanduser().resolve()
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    _dms_server._storage_path_override = str(p)


# ---------------------------------------------------------------------------
# WSGI server thread — waitress with a fixed thread pool
# ---------------------------------------------------------------------------
_server_error: str = ""


def serve() -> None:
    global _server_error
    try:
        from waitress import serve as waitress_serve
        waitress_serve(
            app,
            host="0.0.0.0",   # accept connections from all interfaces (including iPhone on LAN)
            port=PORT,
            threads=8,
            channel_timeout=300,
            connection_limit=50,
        )
    except Exception:
        import traceback
        _server_error = traceback.format_exc()


server_thread = threading.Thread(target=serve, daemon=True)
server_thread.start()

# Give waitress a moment to bind before opening the browser
time.sleep(0.5)


# ---------------------------------------------------------------------------
# Minimal Tkinter status window
# ---------------------------------------------------------------------------
class LauncherWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        _title = f"QCDMS — {Path(_cli_project_path).name}" if _cli_project_path else "QCDMS"
        self.root.title(_title)
        self.root.resizable(False, False)

        BG     = "#fafaf9"
        FG     = "#1c1917"
        SUB    = "#78716c"
        ACCENT = "#0064c8"
        QUIT_BG = "#dc2626"
        QUIT_FG = "#dc2626"  # macOS ignores bg on native buttons; use red fg instead

        self.root.configure(bg=BG)

        # ── Title ────────────────────────────────────────────────────────────
        tk.Label(
            root, text="QC Document Management System (QCDMS)",
            font=("Helvetica", 13, "bold"), bg=BG, fg=FG,
        ).pack(anchor="w", padx=20, pady=(16, 2))

        # ── Status ───────────────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Starting…")
        tk.Label(
            root, textvariable=self.status_var,
            font=("Helvetica", 10), bg=BG, fg=SUB,
        ).pack(anchor="w", padx=20)

        # ── URL (clickable) ──────────────────────────────────────────────────
        url_lbl = tk.Label(
            root, text=URL,
            font=("Helvetica", 10, "underline"), bg=BG, fg=ACCENT,
            cursor="hand2",
        )
        url_lbl.pack(anchor="w", padx=20, pady=(4, 0))
        url_lbl.bind("<Button-1>", lambda _e: webbrowser.open(URL))

        tk.Frame(root, bg="#e7e5e4", height=1).pack(fill="x", padx=20, pady=12)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = tk.Frame(root, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(0, 16))

        # Pack Quit FIRST so it always claims its space on the right.
        tk.Button(
            btn_row, text="Quit DMS",
            command=self._quit,
            bg=QUIT_BG, fg=QUIT_FG,
            activebackground="#b91c1c", activeforeground=QUIT_FG,
            relief="flat", padx=14, pady=5, cursor="hand2",
        ).pack(side="right")

        tk.Button(
            btn_row, text="Open in Browser",
            command=lambda: webbrowser.open(URL),
            relief="groove", padx=10, pady=4,
        ).pack(side="left")

        tk.Button(
            btn_row, text="Copy URL",
            command=self._copy_url,
            relief="groove", padx=10, pady=4,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btn_row, text="Mobile Upload",
            command=self._show_mobile,
            relief="groove", padx=10, pady=4,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btn_row, text="Remote Upload",
            command=self._show_remote_upload,
            relief="groove", padx=10, pady=4,
        ).pack(side="left", padx=(8, 0))

        # ── Footer ───────────────────────────────────────────────────────────
        tk.Label(
            root, text="Closing this window quits the DMS server.",
            font=("Helvetica", 9), bg=BG, fg=SUB,
        ).pack(anchor="w", padx=20, pady=(0, 12))

        # Window close button (red X) → quit
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        # Mark ready after a short pause, then open browser
        self.root.after(400, self._mark_ready)
        self.root.after(900, lambda: webbrowser.open(URL))

        # Check for server startup errors once, after 3 s
        self.root.after(3000, self._check_server)

    def _mark_ready(self) -> None:
        self.status_var.set(f"Running on {URL}")

    def _check_server(self) -> None:
        if _server_error:
            self.status_var.set("Server failed to start — see Console for details")

    def _copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(URL)
        self.status_var.set("URL copied!")
        self.root.after(2000, self._mark_ready)

    def _show_mobile(self) -> None:
        """Open a dialog showing mobile upload URLs and a QR code."""
        import time as _time
        _cache_bust = int(_time.time())

        all_ips = get_all_local_ips()  # [(ip, iface), ...]

        dlg = tk.Toplevel(self.root)
        dlg.title("Mobile Upload")
        dlg.resizable(False, False)

        BG     = "#fafaf9"
        FG     = "#1c1917"
        SUB    = "#78716c"
        ACCENT = "#0064c8"
        WARN   = "#b45309"
        dlg.configure(bg=BG)

        tk.Label(
            dlg, text="Upload from iPhone / Android",
            font=("Helvetica", 13, "bold"), bg=BG, fg=FG,
        ).pack(padx=24, pady=(18, 4), anchor="w")

        tk.Label(
            dlg,
            text="Connect your phone to the same Wi-Fi as this Mac,\n"
                 "then scan the QR code or type the URL in Safari.",
            font=("Helvetica", 10), bg=BG, fg=SUB, justify="left",
        ).pack(padx=24, anchor="w")

        tk.Frame(dlg, bg="#e7e5e4", height=1).pack(fill="x", padx=18, pady=10)

        # ── IP selector (when multiple IPs exist) ────────────────────────────
        # Build a list of (label, url) for each IP
        options: list[tuple[str, str]] = []
        for ip, iface in all_ips:
            label = f"{ip}  [{iface}]" if iface else ip
            url   = f"http://{ip}:{PORT}/mobile/{_cache_bust}"
            options.append((label, url))

        if not options:
            options = [(f"{LOCAL_IP}  [unknown]", f"http://{LOCAL_IP}:{PORT}/mobile/{_cache_bust}")]

        selected_url = tk.StringVar(value=options[0][1])
        selected_label = tk.StringVar(value=options[0][0])

        # QR display area
        qr_label_widget: list[tk.Label] = []

        def make_qr(url: str) -> None:
            try:
                import qrcode  # type: ignore
                from PIL import ImageTk  # type: ignore
                qr_img = qrcode.make(url).resize((192, 192))
                photo  = ImageTk.PhotoImage(qr_img)
                if qr_label_widget:
                    qr_label_widget[0].config(image=photo)
                    qr_label_widget[0].image = photo  # type: ignore[attr-defined]
                else:
                    lbl = tk.Label(qr_frame, image=photo, bg=BG)
                    lbl.image = photo  # type: ignore[attr-defined]
                    lbl.pack()
                    qr_label_widget.append(lbl)
            except Exception:
                if not qr_label_widget:
                    lbl = tk.Label(qr_frame,
                                   text="(qrcode library not available)",
                                   font=("Helvetica", 9), bg=BG, fg=SUB)
                    lbl.pack(pady=8)
                    qr_label_widget.append(lbl)

        qr_frame = tk.Frame(dlg, bg=BG)
        qr_frame.pack(pady=(0, 4))
        make_qr(options[0][1])

        # URL display
        url_var = tk.StringVar(value=options[0][1])
        url_lbl = tk.Label(
            dlg, textvariable=url_var,
            font=("Helvetica", 10, "underline"), bg=BG, fg=ACCENT, cursor="hand2",
            wraplength=320,
        )
        url_lbl.pack(padx=24, pady=(2, 2))
        url_lbl.bind("<Button-1>", lambda _e: webbrowser.open(url_var.get()))

        # IP selector dropdown (only shown when >1 IP)
        if len(options) > 1:
            tk.Label(
                dlg, text="Try a different network address if connection fails:",
                font=("Helvetica", 9), bg=BG, fg=SUB,
            ).pack(padx=24, pady=(6, 2), anchor="w")

            def on_ip_change(event=None) -> None:
                chosen_label = ip_combo.get()
                for lbl, url in options:
                    if lbl == chosen_label:
                        url_var.set(url)
                        make_qr(url)
                        break

            import tkinter.ttk as ttk
            ip_combo = ttk.Combobox(
                dlg,
                values=[lbl for lbl, _ in options],
                state="readonly",
                width=34,
            )
            ip_combo.set(options[0][0])
            ip_combo.pack(padx=24, pady=(0, 6))
            ip_combo.bind("<<ComboboxSelected>>", on_ip_change)

        tk.Frame(dlg, bg="#e7e5e4", height=1).pack(fill="x", padx=18, pady=(8, 0))

        # ── Troubleshooting note ─────────────────────────────────────────────
        trouble_frame = tk.Frame(dlg, bg="#fffbeb")
        trouble_frame.pack(fill="x", padx=18, pady=6)
        tk.Label(
            trouble_frame,
            text='⚠  If Safari shows "server stopped responding":',
            font=("Helvetica", 9, "bold"), bg="#fffbeb", fg=WARN, justify="left",
        ).pack(anchor="w", padx=10, pady=(6, 2))
        tips = (
            "1. Confirm your iPhone is on the same Wi-Fi as this Mac.\n"
            "2. If multiple addresses appear above, try each one.\n"
            "3. Your router may have 'Client Isolation' enabled —\n"
            "   if so, use Remote Upload below (works on any network)."
        )
        tk.Label(
            trouble_frame, text=tips,
            font=("Helvetica", 9), bg="#fffbeb", fg="#92400e",
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 4))

        def _switch_to_remote() -> None:
            dlg.destroy()
            self._show_remote_upload()

        tk.Button(
            trouble_frame,
            text="Use Remote Upload Instead  →",
            command=_switch_to_remote,
            bg=WARN, fg="white",
            relief="flat", padx=10, pady=6,
            font=("Helvetica", 9, "bold"),
            cursor="hand2",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # ── Buttons ──────────────────────────────────────────────────────────
        def _copy_mobile() -> None:
            dlg.clipboard_clear()
            dlg.clipboard_append(url_var.get())
            copy_btn.config(text="Copied!")
            dlg.after(2000, lambda: copy_btn.config(text="Copy URL"))

        btn_row2 = tk.Frame(dlg, bg=BG)
        btn_row2.pack(pady=(6, 18))
        copy_btn = tk.Button(
            btn_row2, text="Copy URL", command=_copy_mobile,
            relief="groove", padx=14, pady=5,
        )
        copy_btn.pack(side="left", padx=6)
        tk.Button(
            btn_row2, text="Close", command=dlg.destroy,
            relief="groove", padx=14, pady=5,
        ).pack(side="left", padx=6)

    def _show_remote_upload(self) -> None:
        """Open the Remote Upload dialog — generates an ngrok link and sends it via iMessage."""
        import urllib.request as _urllib_req
        import json as _json
        import tkinter.ttk as ttk

        BG     = "#fafaf9"
        FG     = "#1c1917"
        SUB    = "#78716c"
        ACCENT = "#0064c8"
        WARN   = "#b45309"

        dlg = tk.Toplevel(self.root)
        dlg.title("Remote Upload")
        dlg.resizable(False, False)
        dlg.configure(bg=BG)

        tk.Label(
            dlg, text="Remote Upload",
            font=("Helvetica", 13, "bold"), bg=BG, fg=FG,
        ).pack(padx=24, pady=(18, 2), anchor="w")

        tk.Label(
            dlg,
            text="Generate a secure link you can text to a phone.\nThe recipient can upload directly to a specific folder.",
            font=("Helvetica", 10), bg=BG, fg=SUB, justify="left",
        ).pack(padx=24, anchor="w")

        tk.Frame(dlg, bg="#e7e5e4", height=1).pack(fill="x", padx=18, pady=10)

        tk.Frame(dlg, bg="#e7e5e4", height=1).pack(fill="x", padx=18, pady=(4, 8))

        # ── Phone number ─────────────────────────────────────────────────────
        tk.Label(dlg, text="Phone number", font=("Helvetica", 10, "bold"),
                 bg=BG, fg=FG).pack(padx=24, anchor="w")
        tk.Label(dlg, text="Include country code, e.g. +16505551234",
                 font=("Helvetica", 9), bg=BG, fg=SUB).pack(padx=24, anchor="w")

        from dms_server import load_config as _load_cfg, save_config as _save_cfg
        saved_phone = _load_cfg().get("remote_phone", "")
        phone_var = tk.StringVar(value=saved_phone)
        tk.Entry(dlg, textvariable=phone_var, width=24,
                 font=("Helvetica", 12)).pack(padx=24, pady=(4, 10), anchor="w")

        # ── Folder selector ──────────────────────────────────────────────────
        tk.Label(dlg, text="Target folder", font=("Helvetica", 10, "bold"),
                 bg=BG, fg=FG).pack(padx=24, anchor="w")

        # Fetch flat node list from the running server
        folder_options: list[tuple[str, str]] = [("", "— No folder (unattached) —")]
        try:
            raw = _urllib_req.urlopen(f"http://127.0.0.1:{PORT}/api/tree", timeout=2).read()
            tree_data = _json.loads(raw)
            tree = tree_data.get("tree") or tree_data.get("data", {}).get("tree")
            if tree:
                stack = [(tree, "")]
                while stack:
                    node, prefix = stack.pop(0)
                    name = node.get("name") or node.get("id", "")
                    label = f"{prefix} › {name}" if prefix else name
                    folder_options.append((node["id"], label))
                    for child in node.get("children", []):
                        stack.append((child, label))
        except Exception:
            pass

        folder_labels = [lbl for _, lbl in folder_options]
        folder_combo = ttk.Combobox(dlg, values=folder_labels, state="readonly", width=40)
        folder_combo.set(folder_labels[0])
        folder_combo.pack(padx=24, pady=(4, 10), anchor="w")

        tk.Frame(dlg, bg="#e7e5e4", height=1).pack(fill="x", padx=18, pady=(0, 8))

        # ── QR / URL area ────────────────────────────────────────────────────
        qr_label_widgets: list[tk.Label] = []
        generated_url: list[str] = []

        qr_frame = tk.Frame(dlg, bg=BG)
        qr_frame.pack(pady=(0, 4))

        url_var = tk.StringVar(value="")
        url_lbl = tk.Label(dlg, textvariable=url_var,
                           font=("Helvetica", 9, "underline"), bg=BG, fg=ACCENT,
                           cursor="hand2", wraplength=340)
        url_lbl.pack(padx=24, pady=(0, 4))
        url_lbl.bind("<Button-1>", lambda _e: webbrowser.open(url_var.get()) if url_var.get() else None)

        def _make_qr(url: str) -> None:
            try:
                import qrcode  # type: ignore
                from PIL import ImageTk  # type: ignore
                qr_img = qrcode.make(url).resize((192, 192))
                photo = ImageTk.PhotoImage(qr_img)
                if qr_label_widgets:
                    qr_label_widgets[0].config(image=photo)
                    qr_label_widgets[0].image = photo  # type: ignore[attr-defined]
                else:
                    lbl = tk.Label(qr_frame, image=photo, bg=BG)
                    lbl.image = photo  # type: ignore[attr-defined]
                    lbl.pack()
                    qr_label_widgets.append(lbl)
            except Exception:
                if not qr_label_widgets:
                    lbl = tk.Label(qr_frame, text="(qrcode library not available)",
                                   font=("Helvetica", 9), bg=BG, fg=SUB)
                    lbl.pack(pady=8)
                    qr_label_widgets.append(lbl)

        # ── Buttons ──────────────────────────────────────────────────────────
        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(dlg, textvariable=status_var,
                              font=("Helvetica", 10), bg=BG, fg=SUB, wraplength=340)
        status_lbl.pack(padx=24, pady=(0, 4))

        def _generate():
            # Read UI values on the main thread before going to background.
            chosen_label = folder_combo.get()
            phone = phone_var.get().strip()

            generate_btn.config(state="disabled")
            status_var.set("Starting tunnel…")
            status_lbl.config(fg=SUB)

            def _do_background():
                def _ui(fn):
                    try:
                        dlg.after(0, fn)
                    except Exception:
                        pass

                def _update_status(msg):
                    _ui(lambda m=msg: (status_var.set(m), status_lbl.config(fg=SUB)))

                try:
                    public_url = _get_or_start_tunnel(status_fn=_update_status)
                except Exception as exc:
                    _ui(lambda e=str(exc): (
                        status_var.set(f"Tunnel error: {e}"),
                        status_lbl.config(fg="#dc2626"),
                        generate_btn.config(state="normal"),
                    ))
                    return

                # Let the Flask server know the public tunnel URL so it can use
                # it for document download links sent to the phone.
                import dms_server as _dms_srv
                _dms_srv._tunnel_base_url = public_url

                node_id = next((nid for nid, lbl in folder_options if lbl == chosen_label), "")

                # Create remote token on the server
                try:
                    payload = _json.dumps({"node_id": node_id}).encode()
                    req = _urllib_req.Request(
                        f"http://127.0.0.1:{PORT}/api/remote-token",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    resp = _urllib_req.urlopen(req, timeout=5)
                    token = _json.loads(resp.read())["token"]
                except Exception as exc:
                    _ui(lambda e=str(exc): (
                        status_var.set(f"Could not create upload token: {e}"),
                        status_lbl.config(fg="#dc2626"),
                        generate_btn.config(state="normal"),
                    ))
                    return

                full_url = f"{public_url}/mobile/r/{token}"

                # Persist phone number for next time (non-UI work)
                if phone:
                    cfg = _load_cfg()
                    cfg["remote_phone"] = phone
                    _save_cfg(cfg)

                def _finish():
                    generated_url.clear()
                    generated_url.append(full_url)
                    url_var.set(full_url)
                    _make_qr(full_url)
                    send_btn.config(state="normal")
                    if sys.platform.startswith("win"):
                        wechat_btn.config(state="normal")
                    copy_btn.config(state="normal")
                    status_var.set("Link ready — valid for 24 hours.")
                    status_lbl.config(fg="#15803d")
                    generate_btn.config(state="normal")

                _ui(_finish)

            threading.Thread(target=_do_background, daemon=True).start()

        def _send():
            phone = phone_var.get().strip()
            if not phone:
                status_var.set("Enter a phone number first.")
                status_lbl.config(fg=WARN)
                return
            if not generated_url:
                status_var.set("Generate a link first.")
                status_lbl.config(fg=WARN)
                return
            try:
                _send_imessage(phone, generated_url[0])
                status_var.set(f"Sent to {phone} via Messages.")
                status_lbl.config(fg="#15803d")
            except Exception as exc:
                status_var.set(str(exc))
                status_lbl.config(fg="#dc2626")

        def _copy():
            if generated_url:
                dlg.clipboard_clear()
                dlg.clipboard_append(generated_url[0])
                copy_btn.config(text="Copied!")
                dlg.after(2000, lambda: copy_btn.config(text="Copy URL"))

        def _send_wechat():
            if not generated_url:
                status_var.set("Generate a link first.")
                status_lbl.config(fg=WARN)
                return
            # Copy URL to clipboard, then open WeChat so the user can paste and send.
            dlg.clipboard_clear()
            dlg.clipboard_append(generated_url[0])
            try:
                import subprocess as _sp
                _sp.Popen(["cmd", "/c", "start", "", "weixin://"])
                status_var.set("Link copied — paste it in WeChat and send.")
                status_lbl.config(fg="#15803d")
            except Exception:
                status_var.set("Link copied to clipboard. Open WeChat and paste to send.")
                status_lbl.config(fg="#15803d")

        action_row = tk.Frame(dlg, bg=BG)
        action_row.pack(pady=(4, 4))

        generate_btn = tk.Button(action_row, text="Generate Link", command=_generate,
                                 relief="groove", padx=12, pady=5)
        generate_btn.pack(side="left", padx=4)

        send_btn = tk.Button(action_row, text="Send via iMessage", command=_send,
                             relief="groove", padx=12, pady=5, state="disabled")
        if not sys.platform.startswith("win"):
            send_btn.pack(side="left", padx=4)

        if sys.platform.startswith("win"):
            wechat_btn = tk.Button(action_row, text="Send via WeChat", command=_send_wechat,
                                   relief="groove", padx=12, pady=5, state="disabled")
            wechat_btn.pack(side="left", padx=4)

        copy_btn = tk.Button(action_row, text="Copy URL", command=_copy,
                             relief="groove", padx=12, pady=5, state="disabled")
        copy_btn.pack(side="left", padx=4)

        close_row = tk.Frame(dlg, bg=BG)
        close_row.pack(pady=(2, 18))
        tk.Button(close_row, text="Close", command=dlg.destroy,
                  relief="groove", padx=14, pady=5).pack()

    def _quit(self) -> None:
        # Kill the cloudflared tunnel process object directly (instant).
        global _tunnel_proc
        if _tunnel_proc is not None:
            try:
                _tunnel_proc.kill()
            except Exception:
                pass
            _tunnel_proc = None

        # Belt-and-suspenders: sweep for any lingering cloudflared.exe on
        # Windows.  Use Popen (fire-and-forget) so we never block — taskkill
        # will finish on its own; we exit immediately after.
        if sys.platform.startswith("win"):
            try:
                import subprocess as _sp
                _sp.Popen(
                    ["taskkill", "/F", "/IM", "cloudflared.exe"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    creationflags=_sp.CREATE_NO_WINDOW,
                )
            except Exception:
                pass

        os._exit(0)


def main() -> None:
    root = tk.Tk()
    win = LauncherWindow(root)

    # macOS: hook into the application-level Quit event (Cmd+Q, dock Quit).
    # This is the correct Tk/macOS bridge — works regardless of window focus.
    try:
        root.createcommand("::tk::mac::Quit", win._quit)
    except tk.TclError:
        pass  # not on macOS — ignore

    # Signal handlers: schedule _quit via the Tk event loop so it's thread-safe.
    # Fall back to direct os._exit if Tkinter is unresponsive.
    def _signal_quit(*_):
        try:
            root.after(0, win._quit)
        except Exception:
            os._exit(0)

    signal.signal(signal.SIGTERM, _signal_quit)
    signal.signal(signal.SIGINT,  _signal_quit)

    root.mainloop()


if __name__ == "__main__":
    main()

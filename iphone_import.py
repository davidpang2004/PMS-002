"""Read photos/videos from a USB-connected iPhone (the DCIM folder) via Apple's
AFC file service, using the pure-python pymobiledevice3 library.

Used by the "Import from iPhone (USB)" dialog. Everything here is synchronous
(the library is asyncio-based; each call runs its own short-lived event loop)
so the Flask routes can call it directly from a worker thread.

Windows needs Apple's USB driver (installed with iTunes or the Apple Devices
app); macOS needs nothing extra.
"""
from __future__ import annotations

import asyncio
import posixpath
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    LIBS_OK = True
    IMPORT_ERROR = ""
except Exception as _e:  # missing package or a platform without usbmuxd
    LIBS_OK = False
    IMPORT_ERROR = str(_e)

MEDIA_EXTS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".dng", ".tif", ".tiff",
    ".mov", ".mp4", ".m4v", ".3gp", ".aae",
}
SKIP_EXTS = {".aae"}        # edit-sidecar files: not useful on their own
DCIM = "/DCIM"
_PAIR_TIMEOUT = 4           # seconds to wait for the user to tap "Trust"


def _run(coro):
    return asyncio.run(coro)


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a library exception to (state, plain-language reason)."""
    name = type(exc).__name__
    if name in ("PairingDialogResponsePendingError", "NotTrustedError", "NotPairedError"):
        return "not_trusted", "Tap “Trust” on your iPhone (and enter its passcode if asked)."
    if name == "UserDeniedPairingError":
        return "not_trusted", "Trust was declined on the iPhone. Unplug, re-plug and tap “Trust”."
    if name == "PasswordRequiredError":
        return "locked", "Unlock your iPhone with its passcode, then try again."
    if name in ("NoDeviceConnectedError", "DeviceNotFoundError"):
        return "no_device", "No iPhone found."
    if name in ("ConnectionFailedToUsbmuxdError", "MuxException", "ConnectionFailedError"):
        return "no_driver", (
            "DMS can’t reach the USB service. On Windows, install iTunes or the Apple "
            "Devices app (it provides the iPhone USB driver), then re-plug the cable.")
    return "error", f"{name}: {exc}"


async def _status() -> dict:
    devices = [d for d in await usbmux.list_devices() if d.is_usb]
    if not devices:
        return {"state": "no_device", "message": "Plug in your iPhone with its USB cable."}
    dev = devices[0]
    try:
        lk = await create_using_usbmux(serial=dev.serial, autopair=True, pair_timeout=_PAIR_TIMEOUT)
    except Exception as e:
        state, msg = _classify(e)
        return {"state": state, "message": msg, "udid": dev.serial}
    try:
        name = lk.display_name or "iPhone"
        return {
            "state": "ready", "udid": dev.serial, "name": name,
            "model": getattr(lk, "product_type", "") or "",
            "ios": getattr(lk, "product_version", "") or "",
            "message": "Connected.",
        }
    finally:
        await lk.close()


def status() -> dict:
    """{state: no_device|not_trusted|locked|no_driver|ready|error, message, udid?, name?}"""
    if not LIBS_OK:
        return {"state": "no_lib", "message": f"iPhone support library missing: {IMPORT_ERROR}"}
    try:
        return _run(_status())
    except Exception as e:
        state, msg = _classify(e)
        return {"state": state, "message": msg}


class _Session:
    """async context: lockdown + AFC for one device."""

    def __init__(self, udid: str):
        self.udid = udid

    async def __aenter__(self) -> AfcService:
        self.lk = await create_using_usbmux(serial=self.udid, autopair=True, pair_timeout=_PAIR_TIMEOUT)
        try:
            self.afc = AfcService(self.lk)
            await self.afc.__aenter__()
        except BaseException:
            await self.lk.close()
            raise
        return self.afc

    async def __aexit__(self, *exc):
        try:
            await self.afc.__aexit__(*exc)
        finally:
            await self.lk.close()


async def _trashed_names(afc) -> set[str]:
    """Device paths of photos in "Recently Deleted". iOS keeps their files in
    DCIM for ~30 days, but the Photos database flags them as trashed. Returns an
    empty set if the database can't be read (the import then behaves as before)."""
    tmp = tempfile.mkdtemp(prefix="dms-photosdb-")
    try:
        for suffix in ("", "-wal", "-shm"):
            try:
                await afc.pull("/PhotoData/Photos.sqlite" + suffix, tmp, progress_bar=False)
            except Exception:
                if suffix == "":
                    return set()
        con = sqlite3.connect(str(Path(tmp) / "Photos.sqlite"))
        try:
            rows = con.execute(
                "SELECT ZDIRECTORY, ZFILENAME FROM ZASSET WHERE ZTRASHEDSTATE = 1").fetchall()
        finally:
            con.close()
        # ZDIRECTORY is stored as "DCIM/100APPLE" (no leading slash).
        return {posixpath.join("/", d, f).lower() for d, f in rows if d and f}
    except Exception:
        return set()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _list_media(udid: str) -> list[dict]:
    items: list[dict] = []
    async with _Session(udid) as afc:
        trashed = await _trashed_names(afc)
        for folder in await afc.listdir(DCIM):
            fdir = posixpath.join(DCIM, folder)
            try:
                if not await afc.isdir(fdir):
                    continue
                names = await afc.listdir(fdir)
            except Exception:
                continue
            for name in names:
                ext = posixpath.splitext(name)[1].lower()
                if ext not in MEDIA_EXTS or ext in SKIP_EXTS or name.startswith("."):
                    continue
                path = posixpath.join(fdir, name)
                if path.lower() in trashed:
                    continue
                try:
                    st = await afc.stat(path)
                except Exception:
                    continue
                if st.get("st_ifmt") == "S_IFDIR":
                    continue
                mt = st.get("st_mtime")
                ts = mt.timestamp() if isinstance(mt, datetime) else float(mt or 0)
                if ts > 1e12:       # nanoseconds
                    ts /= 1e9
                items.append({
                    "path": path, "name": name, "size": int(st.get("st_size", 0)),
                    "mtime": ts, "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "",
                    "video": ext in (".mov", ".mp4", ".m4v", ".3gp"),
                })
    items.sort(key=lambda i: (i["mtime"], i["name"]), reverse=True)
    return items


def list_media(udid: str) -> list[dict]:
    """Every photo/video in the phone's DCIM folders, newest first."""
    return _run(_list_media(udid))


def _check_device_path(path: str) -> str:
    p = posixpath.normpath(path)
    if not (p == DCIM or p.startswith(DCIM + "/")) or ".." in p.split("/"):
        raise ValueError("Path outside the phone's DCIM folder")
    return p


async def _pull(udid: str, path: str, dest_dir: str) -> str:
    async with _Session(udid) as afc:
        await afc.pull(path, dest_dir, progress_bar=False)
    out = Path(dest_dir) / posixpath.basename(path)
    return str(out)


def pull_file(udid: str, path: str, dest_dir: str) -> str:
    """Copy one DCIM file from the phone into dest_dir; returns the local path."""
    return _run(_pull(udid, _check_device_path(path), dest_dir))


def photo_meta(local_path: str) -> dict:
    """EXIF capture date + GPS of a pulled photo, e.g. {"year","month","day","lat","lon"}.

    The browser's own EXIF reader only understands JPEG, so HEIC photos from the
    phone would lose their GPS (and so their location folder). Keys that can't
    be read are simply left out.
    """
    out: dict = {}
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        from PIL import Image
        img = Image.open(local_path)
        exif = img.getexif()
        dt = ""
        try:
            sub = exif.get_ifd(0x8769)       # Exif sub-IFD: DateTimeOriginal / Digitized
            dt = sub.get(36867) or sub.get(36868) or ""
        except Exception:
            pass
        dt = dt or exif.get(306) or ""       # DateTime
        if isinstance(dt, str) and len(dt) >= 10 and dt[4] == ":" and dt[7] == ":":
            out.update(year=dt[0:4], month=dt[5:7], day=dt[8:10])
    except Exception:
        pass
    try:
        import dms_server
        lat, lon = dms_server._read_photo_gps(Path(local_path).read_bytes())
        if lat is not None and lon is not None:
            out.update(lat=lat, lon=lon)
    except Exception:
        pass
    return out

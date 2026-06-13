# DMS — Document Management System (local server version)

A small document management system for engineering assemblies. Stores PDFs and
photos as real files on your hard drive, organized by a hierarchical assembly
tree, with metadata-based search.

## What's in this folder

- `dms_server.py` — the Python backend. Serves the web UI and handles file storage.
- `dms.html` — the web UI. Loaded automatically by the server.
- `start_dms.command` — Mac/Linux launcher. Double-click to run.
- `start_dms.bat` — Windows launcher. Double-click to run.
- `README.md` — this file.

The server stores its own settings (your chosen storage path) in
`~/.dms_server_config.json`. Your actual documents live wherever you point it.

## First-time setup

### Mac

1. Make sure Python 3 is installed. Open Terminal and run:
   ```
   python3 --version
   ```
   If it prints a version (3.8 or higher), you're set. If not, install from
   https://www.python.org/downloads/ or run `brew install python3`.

2. Save this whole folder somewhere permanent — e.g. `~/Applications/DMS/`.
   Don't run it from `~/Downloads/` since browsers handle that folder oddly.

3. Double-click `start_dms.command` to launch.
   - The first time, macOS may warn "this is from an unidentified developer."
     Right-click the file → Open → Open. After that, double-click works.
   - The launcher will install Flask automatically if it's missing.

4. Your browser opens automatically to http://localhost:8000/. The first screen
   asks where you want to store documents. Type a path like:
   ```
   ~/Documents/Company01/DMS_Storage
   ```
   Click Save. The folder will be created if it doesn't exist.

5. You're in. Start adding folders, uploading drawings.

### Windows

1. Install Python 3 from https://www.python.org/downloads/ (check
   "Add Python to PATH" during install).

2. Save this folder to a permanent location, e.g. `C:\Users\YourName\DMS\`.

3. Double-click `start_dms.bat`. A console window will appear.

4. Your browser opens automatically. Enter a storage path like:
   ```
   C:\Company01\DMS_Storage
   ```

5. Done.

### Linux

1. Make sure `python3` and `pip` are installed:
   ```
   sudo apt install python3 python3-pip
   ```
2. Run the launcher:
   ```
   ./start_dms.command
   ```

## Daily use

1. Double-click the launcher (or run `python3 dms_server.py` from a terminal).
2. The browser opens to the DMS.
3. When you're done, close the terminal/console window (or press Ctrl+C in it).
4. Your documents stay safely on disk in whatever folder you configured.

## Where are my files?

After uploading documents, your storage folder will look like:

```
<your chosen path>/
├── index.json                       <- tree structure + metadata index
└── docs/
    ├── DOC-20260428-A4F7K2.pdf      <- real PDF, opens in any PDF reader
    ├── DOC-20260428-B9X3M1.jpg      <- real JPEG, opens in any image viewer
    └── DOC-20260428-C7H2N9.pdf
```

These are normal files. You can:
- Open them outside the DMS using Finder/Explorer
- Back up the whole folder to OneDrive/Dropbox/iCloud
- Copy the folder to another computer (the DMS will recognize it
  as long as you point the new install at the same path)

## Migration to a real web server

When you're ready for v2.0 (multi-user, network-accessible, on a real server):

1. Copy `dms_server.py` and `dms.html` to a Linux server.
2. Run via `gunicorn` or `uwsgi` instead of the dev server (Flask docs cover this).
3. Put it behind nginx for HTTPS.
4. The frontend (`dms.html`) doesn't change. The data layout on disk doesn't change.
   Even the API contract doesn't change.

The same code, same data, same behavior — just on a network instead of localhost.

## Troubleshooting

**"Could not reach the DMS server" in the browser**
The Python server isn't running. Check the terminal window — it should say
"Serving on http://127.0.0.1:8000". If you closed it, restart with the launcher.

**Port 8000 already in use**
Something else is using port 8000. Run with a different port:
```
python3 dms_server.py --port 8123
```

**Files not appearing in my chosen folder**
Type `dmsDiag()` in the browser console (Cmd+Opt+I on Mac, F12 on Windows).
It will print the actual server-side state including the resolved path and
all files currently in the folder.

**"Permission denied" when saving**
The path you chose isn't writable by your user account. Try a path inside
your home folder. Avoid system folders like `/Applications`, `/etc`, `C:\Program Files`.

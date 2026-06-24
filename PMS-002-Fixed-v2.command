#!/bin/bash
# PMS-002 fixed launcher v2.
# Put this file in the PMS-002 app folder, then double-click it.
# It patches the folder-name save behavior, preserves subfolders, moves indexed
# files into the renamed folder tree, backs up changed files, and starts PMS.

set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$APP_DIR/dms_server.py" ] || [ ! -f "$APP_DIR/dms.html" ]; then
  PARENT_DIR="$(cd "$APP_DIR/.." && pwd)"
  if [ -f "$PARENT_DIR/dms_server.py" ] && [ -f "$PARENT_DIR/dms.html" ]; then
    APP_DIR="$PARENT_DIR"
  else
    echo
    echo "ERROR: Could not find dms_server.py and dms.html."
    echo "Move this file into the PMS-002 folder, or into PMS-002/dist, then run it again."
    echo
    read -p "Press Enter to exit..."
    exit 1
  fi
fi

cd "$APP_DIR"

python3 - <<'PY'
from pathlib import Path
import datetime

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path: Path) -> None:
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    if not bak.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        print(f"[OK] {label} already patched")
        return text
    if old not in text:
        raise RuntimeError(f"Could not find expected code block for: {label}")
    print(f"[PATCH] {label}")
    return text.replace(old, new, 1)


server_path = Path("dms_server.py")
html_path = Path("dms.html")

server = server_path.read_text(encoding="utf-8")
html = html_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Frontend fix:
# The selected node name field must not save on every keystroke.
# ---------------------------------------------------------------------------
html_name_old = '''                    <input
                      type="text" value={selectedNode.name}
                      onChange={(e) => updateNodeMeta({ name: e.target.value })}
                      placeholder="节点名称"
                      className="w-full text-xl font-semibold border border-stone-300 rounded px-2 py-1"
                    />'''

html_name_new = '''                    <input
                      type="text"
                      defaultValue={selectedNode.name}
                      onBlur={(e) => {
                        const nextName = e.target.value.trim();
                        if (nextName && nextName !== selectedNode.name) {
                          handleCommitRename(selectedNode.id, nextName);
                        } else {
                          e.target.value = selectedNode.name;
                        }
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") e.currentTarget.blur();
                        if (e.key === "Escape") {
                          e.currentTarget.value = selectedNode.name;
                          e.currentTarget.blur();
                        }
                      }}
                      placeholder="节点名称"
                      className="w-full text-xl font-semibold border border-stone-300 rounded px-2 py-1"
                    />'''

if html_name_new not in html:
    html = replace_once(
        html,
        html_name_old,
        html_name_new,
        "frontend: commit selected node name only after typing is finished",
    )
else:
    print("[OK] frontend: name field already commits after typing")

# ---------------------------------------------------------------------------
# Backend v2:
# Create the current node folder hierarchy, but do NOT delete general empty
# folders. Then move indexed document files into the folder path that matches
# each document's originalNodeId. This preserves/rebuilds subfolders after a
# root rename such as OldRoot -> Family Photo.
# ---------------------------------------------------------------------------
original_create = '''def _create_local_folder_structure(tree: dict | None) -> "Path | None":
    """Create the on-disk folder hierarchy that mirrors the imported tree."""
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
'''

v1_create = '''def _prune_empty_orphan_node_dirs(docs_dir: Path, expected_dirs: set[tuple[str, ...]]) -> None:
    """Remove empty docs subfolders that do not match the current tree.

    This is deliberately conservative: only empty folders are removed. If a
    stale folder contains user files, it is left untouched so no data is lost.
    """
    if not docs_dir.exists():
        return
    for path in sorted((p for p in docs_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            rel = tuple(path.relative_to(docs_dir).parts)
            if rel in expected_dirs:
                continue
            if any(path.iterdir()):
                continue
            path.rmdir()
        except OSError:
            continue


def _create_local_folder_structure(tree: dict | None) -> "Path | None":
    """Create the on-disk folder hierarchy that mirrors the imported tree."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        return None
    docs_dir.mkdir(parents=True, exist_ok=True)

    if not tree:
        _prune_empty_legacy_photo_dirs(docs_dir)
        return docs_dir

    _migrate_legacy_photo_files(docs_dir, tree)

    expected_dirs: set[tuple[str, ...]] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        node_id = node.get("id")
        if node_id:
            parts = tuple(_get_node_path_parts(tree, node_id))
            if parts:
                expected_dirs.add(parts)
            _get_node_docs_dir(node_id, tree)
        for child in reversed(node.get("children") or []):
            stack.append(child)

    _prune_empty_orphan_node_dirs(docs_dir, expected_dirs)
    _prune_empty_legacy_photo_dirs(docs_dir)
    return docs_dir
'''

v2_create = '''def _create_local_folder_structure(tree: dict | None) -> "Path | None":
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
'''

if v2_create in server:
    print("[OK] backend: v2 folder sync already installed")
elif v1_create in server:
    server = server.replace(v1_create, v2_create, 1)
    print("[PATCH] backend: replace v1 cleanup with v2 folder-preserving sync")
elif original_create in server:
    server = server.replace(original_create, v2_create, 1)
    print("[PATCH] backend: install v2 folder-preserving sync")
else:
    raise RuntimeError("Could not find _create_local_folder_structure block")

put_tree_old = '''    write_index(idx)
    _create_local_folder_structure(idx.get("tree"))
    return jsonify({"ok": True})
'''

put_tree_new = '''    write_index(idx)
    _create_local_folder_structure(idx.get("tree"))
    _sync_doc_files_to_tree_paths(idx.get("tree"))
    return jsonify({"ok": True})
'''

if put_tree_new not in server:
    server = replace_once(
        server,
        put_tree_old,
        put_tree_new,
        "backend: move indexed files after tree save",
    )
else:
    print("[OK] backend: tree save already syncs indexed files")

if server != server_path.read_text(encoding="utf-8"):
    backup(server_path)
    server_path.write_text(server, encoding="utf-8")
if html != html_path.read_text(encoding="utf-8"):
    backup(html_path)
    html_path.write_text(html, encoding="utf-8")

print("[OK] PMS-002 folder-name/subfolder preservation fix v2 applied.")
PY

echo
echo "============================================================"
echo "  PMS-002 fixed app launcher v2"
echo "============================================================"
echo "  Folder: $APP_DIR"
echo "  Server: http://localhost:8001"
echo

if [ -f "venv/bin/python3" ]; then
  PYTHON="venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "ERROR: Python 3 is not installed."
  read -p "Press Enter to exit..."
  exit 1
fi

NEEDED_PACKAGES="flask pypdf reportlab Pillow"
MISSING=""
for pkg in $NEEDED_PACKAGES; do
  case "$pkg" in
    Pillow) import_name="PIL" ;;
    *) import_name="$pkg" ;;
  esac
  if ! $PYTHON -c "import $import_name" 2>/dev/null; then
    MISSING="$MISSING $pkg"
  fi
done

if [ -n "$MISSING" ]; then
  echo "Installing missing packages:$MISSING"
  $PYTHON -m pip install --user $MISSING
fi

$PYTHON dms_server.py "$@"

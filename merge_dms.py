"""
Merge multiple .dms exports of the SAME project into one .dms file.

Typical use case: you exported several ".dms" backups from the same DMS
project, each covering a different subfolder (e.g. via "export selected"),
and want to import them back as a single combined project instead of
importing them one at a time (which would overwrite the previous import's
index.json each time).

Because "export selected" backups keep the *full* folder tree and only
filter which documents (docIndex entries) are included, merging is safe:
  - tree: taken from the first input file (they should all be identical,
    since they came from the same project) — a warning is printed if the
    others look different.
  - docIndex: concatenated and de-duplicated by document id.
  - docs/ files: unioned; if two inputs disagree on the content of the same
    path, the first one wins and a warning is printed.
  - .dms_project.json (project name/password identity): taken from whichever
    input has it.

Usage:
    python3 merge_dms.py A.dms B.dms C.dms -o merged.dms
    python3 merge_dms.py A.dms B.dms C.dms -o merged.dms --password secret

If any input file is password-protected, pass --password (same password
for all inputs — exports of one project always share one password). The
merged output is re-encrypted with that same password; omit --password to
write an unencrypted merged file.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

_DMS_ENC_MAGIC = b"DMSENC1\0"
_DMS_ENC_SALT_LEN = 16


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    import base64
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=32)
    return base64.urlsafe_b64encode(raw)


def _is_encrypted(data: bytes) -> bool:
    return data[: len(_DMS_ENC_MAGIC)] == _DMS_ENC_MAGIC


def _decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet
    salt = blob[len(_DMS_ENC_MAGIC):len(_DMS_ENC_MAGIC) + _DMS_ENC_SALT_LEN]
    token = blob[len(_DMS_ENC_MAGIC) + _DMS_ENC_SALT_LEN:]
    key = _derive_fernet_key(password, salt)
    return Fernet(key).decrypt(token)


def _encrypt(data: bytes, password: str) -> bytes:
    import secrets
    from cryptography.fernet import Fernet
    salt = secrets.token_bytes(_DMS_ENC_SALT_LEN)
    key = _derive_fernet_key(password, salt)
    token = Fernet(key).encrypt(data)
    return _DMS_ENC_MAGIC + salt + token


def load_dms(path: Path, password: str) -> dict:
    raw = path.read_bytes()
    if _is_encrypted(raw):
        if not password:
            sys.exit(f"error: {path} is password-protected; pass --password")
        try:
            raw = _decrypt(raw, password)
        except Exception:
            sys.exit(f"error: wrong password for {path}")

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            sys.exit(f"error: {path} is not a valid .dms file (no manifest.json)")
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != "dms-project":
            sys.exit(f"error: {path} is not a DMS project file")

        index = json.loads(zf.read("index.json")) if "index.json" in names else {"tree": None, "docIndex": []}
        project_meta = zf.read(".dms_project.json") if ".dms_project.json" in names else None

        docs: dict[str, bytes] = {}
        dirs: set[str] = set()
        for name in names:
            if name in ("manifest.json", "index.json", ".dms_project.json"):
                continue
            if name.endswith("/"):
                dirs.add(name)
                continue
            docs[name] = zf.read(name)

    return {
        "path": path,
        "manifest": manifest,
        "index": index,
        "project_meta": project_meta,
        "docs": docs,
        "dirs": dirs,
    }


def merge(loaded: list[dict]) -> tuple[dict, bytes | None, dict[str, bytes], set[str]]:
    base_tree = loaded[0]["index"].get("tree")
    base_sn = json.dumps(base_tree, sort_keys=True) if base_tree else None
    for other in loaded[1:]:
        other_tree = other["index"].get("tree")
        if json.dumps(other_tree, sort_keys=True) != base_sn:
            print(f"warning: tree in {other['path'].name} differs from {loaded[0]['path'].name}; "
                  f"using the tree from {loaded[0]['path'].name}", file=sys.stderr)

    merged_doc_index: list[dict] = []
    seen_ids: set[str] = set()
    for entry in loaded:
        for doc in entry["index"].get("docIndex") or []:
            did = doc.get("id")
            if did in seen_ids:
                continue
            seen_ids.add(did)
            merged_doc_index.append(doc)

    merged_index = {**loaded[0]["index"], "tree": base_tree, "docIndex": merged_doc_index}

    project_meta = None
    for entry in loaded:
        if entry["project_meta"]:
            project_meta = entry["project_meta"]
            break

    merged_docs: dict[str, bytes] = {}
    conflicts = 0
    for entry in loaded:
        for name, data in entry["docs"].items():
            if name in merged_docs and merged_docs[name] != data:
                conflicts += 1
                continue  # first one wins
            merged_docs[name] = data
    if conflicts:
        print(f"warning: {conflicts} file path(s) had conflicting content across inputs; kept the first seen",
              file=sys.stderr)

    merged_dirs: set[str] = set()
    for entry in loaded:
        merged_dirs |= entry["dirs"]

    return merged_index, project_meta, merged_docs, merged_dirs


def write_merged(out_path: Path, index: dict, project_meta: bytes | None,
                  docs: dict[str, bytes], dirs: set[str], sources: list[Path], password: str) -> None:
    from datetime import datetime
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "format": "dms-project",
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": f"merged from {len(sources)} files: " + ", ".join(p.name for p in sources),
            "includes_files": bool(docs),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("index.json", json.dumps(index, indent=2, ensure_ascii=False))
        if project_meta:
            zf.writestr(".dms_project.json", project_meta)
        for d in sorted(dirs):
            if d not in docs:
                zf.writestr(d, "")
        for name, data in docs.items():
            zf.writestr(name, data)

    data = buf.getvalue()
    if password:
        data = _encrypt(data, password)
    out_path.write_bytes(data)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="two or more .dms files to merge")
    ap.add_argument("-o", "--output", required=True, type=Path, help="path to write the merged .dms file")
    ap.add_argument("--password", default="", help="password if the inputs are encrypted (same for all)")
    args = ap.parse_args()

    if len(args.inputs) < 2:
        sys.exit("error: give at least two .dms files to merge")
    for p in args.inputs:
        if not p.exists():
            sys.exit(f"error: {p} not found")

    loaded = [load_dms(p, args.password) for p in args.inputs]
    index, project_meta, docs, dirs = merge(loaded)
    write_merged(args.output, index, project_meta, docs, dirs, args.inputs, args.password)

    doc_count = len(index.get("docIndex") or [])
    file_count = len(docs)
    print(f"Merged {len(args.inputs)} files -> {args.output}")
    print(f"  {doc_count} document entries, {file_count} document files")


if __name__ == "__main__":
    main()

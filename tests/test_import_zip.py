"""Tier 2 hardening coverage: POST /api/docs/import-zip distributes a
zip's files to tree nodes by matching a "<FolderName>#filename.ext"
convention, writing docIndex/tree entries and calling write_index() itself
(unlike the single-file upload routes, which leave index registration to
the client) -- a bulk-write path that had no coverage.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import dms_server


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


def _make_zip(entries: dict) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


class ImportZipDocsTests(unittest.TestCase):
    def test_allocates_files_by_hash_prefix_folder_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "SA-100", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            zip_buf = _make_zip({
                "SA-100#Drawing-001.pdf": b"pdf-bytes-1",
                "SA-100#Drawing-002.pdf": b"pdf-bytes-2",
            })
            resp = client.post("/api/docs/import-zip", data={
                "file": (zip_buf, "import.zip"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(len(body["allocated"]), 2)
            self.assertEqual(body["unmatched"], [])

            idx = dms_server.read_index()
            self.assertEqual(len(idx["docIndex"]), 2)
            node_a = dms_server._find_node(idx["tree"], "NODE-A")
            self.assertEqual(len(node_a["documents"]), 2)

            folder = dms_server._get_node_docs_dir("NODE-A", idx["tree"])
            on_disk = {p.read_bytes() for p in folder.glob("*") if p.is_file()}
            self.assertEqual(on_disk, {b"pdf-bytes-1", b"pdf-bytes-2"})

    def test_unmatched_entries_are_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            zip_buf = _make_zip({
                "no-hash-separator.pdf": b"x",
                "UnknownFolder#file.pdf": b"y",
            })
            resp = client.post("/api/docs/import-zip", data={
                "file": (zip_buf, "import.zip"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["allocated"], [])
            reasons = {u["filename"]: u["reason"] for u in body["unmatched"]}
            self.assertIn("No '#' separator in filename", reasons["no-hash-separator.pdf"])
            self.assertIn("No folder named", reasons["UnknownFolder#file.pdf"])

            idx = dms_server.read_index()
            self.assertEqual(idx["docIndex"], [])

    def test_macos_metadata_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "SA-100", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            zip_buf = _make_zip({
                # Real macOS Finder zip junk: AppleDouble resource-fork
                # files under __MACOSX/, named "._<original-name>" as
                # their own basename (not prefixed by the folder-name#
                # convention at all).
                "__MACOSX/SA-100/._Drawing-001.pdf": b"resource-fork-junk",
                "._Drawing-002.pdf": b"dotfile-junk",
                "SA-100#Drawing-003.pdf": b"real-content",
            })
            resp = client.post("/api/docs/import-zip", data={
                "file": (zip_buf, "import.zip"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(len(body["allocated"]), 1)
            self.assertEqual(body["allocated"][0]["filename"], "SA-100#Drawing-003.pdf")
            self.assertEqual(body["unmatched"], [])

    def test_path_style_prefix_resolves_nested_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-PARENT", "name": "Parent", "children": [
                    {"id": "NODE-CHILD", "name": "Child", "children": [], "documents": []},
                ], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            zip_buf = _make_zip({"Parent/Child#doc.pdf": b"nested-content"})
            resp = client.post("/api/docs/import-zip", data={
                "file": (zip_buf, "import.zip"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(len(body["allocated"]), 1)

            idx = dms_server.read_index()
            child = dms_server._find_node(idx["tree"], "NODE-CHILD")
            self.assertEqual(len(child["documents"]), 1)

    def test_invalid_zip_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/import-zip", data={
                "file": (io.BytesIO(b"not a zip file"), "import.zip"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

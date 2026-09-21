"""Regression coverage for a real user-reported bug: folder uploads
(which mirror a source directory's full nested structure, producing much
deeper on-disk paths than picking one already-open folder) occasionally
failed with an opaque HTTP 500, while re-uploading the same file singly
worked. Tracing both upload routes found the actual gap: the disk-write
step in post_doc (for photos, a bare `out_path.write_bytes(...)` with no
try/except at all) and post_doc_stream (a temp-file-then-replace pattern
that caught its own exception only to re-raise it) both let any OSError
(disk full, permission denied, or -- the leading theory here -- a path
that exceeds the OS's length limit once a deep mirrored folder structure
is involved) propagate uncaught, producing an unhelpful generic 500.

Both routes now catch OSError from the write step and return a clean
{"error": "Could not save file to disk: <reason>"} response instead of
crashing -- these tests simulate that failure (mocking the write itself,
since portably forcing a *real* OS path-length error isn't practical) and
confirm the clean-error behavior plus that no stray .part temp file is
left behind.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dms_server


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


def _tree_with_folder():
    return {"id": "NODE-ROOT", "name": "Root", "children": [
        {"id": "NODE-A", "name": "Folder A", "children": [], "documents": []},
    ], "documents": []}


class PostDocWriteFailureTests(unittest.TestCase):
    def test_photo_write_failure_returns_clean_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            with patch.object(Path, "write_bytes", side_effect=OSError("File name too long")):
                resp = client.post("/api/docs", data={
                    "file": (io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg-bytes"), "photo.jpg"),
                    "doc_id": "DOC-1", "node_id": "NODE-A",
                }, content_type="multipart/form-data")

            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertIn("Could not save file to disk", body["error"])
            self.assertIn("File name too long", body["error"])

    def test_non_photo_write_failure_returns_clean_error_and_cleans_up_part_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            with patch.object(Path, "replace", side_effect=OSError("File name too long")):
                resp = client.post("/api/docs", data={
                    "file": (io.BytesIO(b"plain text"), "notes.txt"),
                    "doc_id": "DOC-2", "node_id": "NODE-A",
                }, content_type="multipart/form-data")

            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertIn("Could not save file to disk", body["error"])

            folder = dms_server._get_node_docs_dir("NODE-A", _tree_with_folder())
            self.assertEqual(list(folder.glob("*.part")), [], "no leftover temp file after a failed write")


class PostDocStreamWriteFailureTests(unittest.TestCase):
    def test_stream_write_failure_returns_clean_error_and_cleans_up_part_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            with patch.object(Path, "replace", side_effect=OSError("File name too long")):
                resp = client.post(
                    "/api/docs/upload-stream?doc_id=DOC-3&node_id=NODE-A&filename=big.bin",
                    data=b"raw-stream-bytes",
                    content_type="application/octet-stream",
                )

            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertIn("Could not save file to disk", body["error"])

            folder = dms_server._get_node_docs_dir("NODE-A", _tree_with_folder())
            self.assertEqual(list(folder.glob("*.part")), [], "no leftover temp file after a failed write")


if __name__ == "__main__":
    unittest.main()

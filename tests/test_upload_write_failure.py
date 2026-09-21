"""Regression coverage for a real user-reported bug: uploading a folder
via "Sort into year/month folders (including files in subfolders)"
occasionally failed a few files out of a large batch with an error
recalled as "500", while re-uploading the same file singly worked.

Tracing both upload routes found several previously-unguarded spots where
an OSError could propagate uncaught into an opaque HTTP 500 with no
actionable message: the disk-write step itself (post_doc's photo branch
had no try/except at all; post_doc's non-photo branch and post_doc_stream
caught their own exception only to re-raise it), and separately the
destination-folder creation step (_route_upload_out_dir and the helpers
it calls -- _get_node_docs_dir, _photo_year_month_dir -- each do their
own unguarded mkdir()). All of these are now caught and turned into a
clean {"error": "..."} response instead of crashing.

Note: the original "deep mirrored folder path" theory (year/month mode
recreating the whole source subfolder tree) turned out not to fit this
report -- year-month mode flattens everything into shared year/month
folders, it doesn't mirror subfolder depth, and the user confirmed the
source folder was on local disk, not a cloud-sync placeholder. The exact
per-file trigger for "a few files out of many" is still not 100% confirmed
-- these fixes make whatever it is fail cleanly and informatively instead
of crashing, so the next occurrence's error message should pin it down.

These tests simulate the failures (mocking the write/mkdir calls, since
portably forcing a *real* OS error isn't practical) and confirm the
clean-error behavior plus that no stray .part temp file is left behind.

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
    def test_destination_folder_creation_failure_returns_clean_error(self):
        """_route_upload_out_dir (and _get_node_docs_dir/_photo_year_month_dir
        underneath it) create the destination folder with an unguarded
        mkdir() along the way -- separate from the write step covered by
        the other tests here, and just as capable of raising an uncaught
        OSError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            with patch.object(dms_server, "_route_upload_out_dir", side_effect=OSError("Permission denied")):
                resp = client.post("/api/docs", data={
                    "file": (io.BytesIO(b"plain text"), "notes.txt"),
                    "doc_id": "DOC-0", "node_id": "NODE-A",
                }, content_type="multipart/form-data")

            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertIn("Could not create destination folder", body["error"])

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

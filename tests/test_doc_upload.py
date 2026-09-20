"""Tier 2 hardening coverage: the two document-upload routes (POST /api/docs
and POST /api/docs/upload-stream, the streamed variant used above a size
threshold) share _route_upload_out_dir for where a file lands and the same
path-traversal/upload-limit guards, but had no test coverage before this.

Uploads only write bytes to disk -- registering the new doc in the tree/
docIndex is the client's job via separate PUT /api/tree + PUT /api/doc-index
calls (same split responsibility as delete_doc, see
tests/test_node_and_doc_delete.py) -- so these tests check placement,
content, and the security/limit guards, not index state.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import io
import tempfile
import unittest
from pathlib import Path

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


class PostDocTests(unittest.TestCase):
    def setUp(self):
        self._orig_max_docs = dms_server._get_max_documents
        self.addCleanup(setattr, dms_server, "_get_max_documents", self._orig_max_docs)

    def test_uploads_into_the_requested_node_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = _tree_with_folder()
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/docs", data={
                "file": (io.BytesIO(b"plain text content"), "notes.txt"),
                "doc_id": "DOC-1",
                "node_id": "NODE-A",
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            matches = list(folder.glob("*DOC-1*"))
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].read_bytes(), b"plain text content")
            self.assertNotIn("part", matches[0].suffix)

    def test_rejects_path_traversal_doc_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/docs", data={
                "file": (io.BytesIO(b"x"), "notes.txt"),
                "doc_id": "../../etc/passwd",
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 400)

    def test_missing_file_or_doc_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/docs", data={"doc_id": "DOC-1"}, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 400)

            resp = client.post("/api/docs", data={
                "file": (io.BytesIO(b"x"), "notes.txt"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 400)

    def test_upload_limit_reached_rejects_further_uploads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            dms_server._get_max_documents = lambda: 1
            client = dms_server.app.test_client()

            resp = client.post("/api/docs", data={
                "file": (io.BytesIO(b"first"), "a.txt"), "doc_id": "DOC-1", "node_id": "NODE-A",
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            resp = client.post("/api/docs", data={
                "file": (io.BytesIO(b"second"), "b.txt"), "doc_id": "DOC-2", "node_id": "NODE-A",
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.get_json()["code"], "upload_limit_reached")

            folder = dms_server._get_node_docs_dir("NODE-A", _tree_with_folder())
            self.assertEqual(list(folder.glob("*DOC-2*")), [])


class PostDocStreamTests(unittest.TestCase):
    def test_uploads_into_the_requested_node_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = _tree_with_folder()
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post(
                "/api/docs/upload-stream?doc_id=DOC-1&node_id=NODE-A&filename=big.bin",
                data=b"raw-stream-bytes" * 1000,
                content_type="application/octet-stream",
            )
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            matches = list(folder.glob("*DOC-1*"))
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].read_bytes(), b"raw-stream-bytes" * 1000)
            self.assertFalse(list(folder.glob("*.part")), "no leftover temp file after a clean upload")

    def test_rejects_path_traversal_doc_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post(
                "/api/docs/upload-stream?doc_id=..%2F..%2Fescape&filename=x.bin",
                data=b"x", content_type="application/octet-stream",
            )
            self.assertEqual(resp.status_code, 400)

    def test_missing_doc_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": _tree_with_folder(), "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post(
                "/api/docs/upload-stream?filename=x.bin",
                data=b"x", content_type="application/octet-stream",
            )
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

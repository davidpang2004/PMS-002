"""Tier 2 hardening coverage: POST /api/docs/<id>/rename-file (renames the
on-disk file only, deliberately not index.json -- see its own docstring
about avoiding a race with the client's docIndex save) and POST
/api/docs/download-to-folder (copies files out to an arbitrary folder on
disk, e.g. the user's Downloads). Neither had test coverage before this.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
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


class RenameDocFileTests(unittest.TestCase):
    def test_renames_file_and_preserves_extension_when_new_name_has_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            docs_dir = dms_server.get_docs_dir()
            path = docs_dir / dms_server._doc_filename("DOC-1", "old.pdf")
            path.write_bytes(b"content")
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/DOC-1/rename-file", json={"name": "new report"})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertFalse(path.exists())

            matches = list(docs_dir.glob("*DOC-1*"))
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0].name.startswith("new report"))
            self.assertEqual(matches[0].suffix, ".pdf")
            self.assertEqual(matches[0].read_bytes(), b"content")

    def test_renaming_to_the_same_name_is_a_harmless_no_op(self):
        """dest == src (same computed filename) must not be treated as a
        409 collision -- it's the same file, not two different ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            docs_dir = dms_server.get_docs_dir()
            src = docs_dir / dms_server._doc_filename("DOC-1", "a.pdf")
            src.write_bytes(b"content")
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/DOC-1/rename-file", json={"name": "a.pdf"})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertTrue(src.exists())
            self.assertEqual(src.read_bytes(), b"content")

    def test_404_when_document_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.get_docs_dir()
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/DOC-MISSING/rename-file", json={"name": "x.pdf"})
            self.assertEqual(resp.status_code, 404)

    def test_path_traversal_doc_id_rejected(self):
        """A doc_id containing an embedded slash never even reaches this
        handler -- Werkzeug's default route converter won't match a path
        segment with a "/" in it, so that case 404s at the routing layer.
        The explicit "/", "\\", ".." guard in the handler itself is what
        catches a single traversal segment like "..", which *does* match
        the route."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.get_docs_dir()
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/../rename-file", json={"name": "x.pdf"})
            self.assertEqual(resp.status_code, 400)

    def test_rename_does_not_touch_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": [{"id": "DOC-1"}]}
            dms_server.write_index({"tree": tree, "docIndex": [{"id": "DOC-1", "name": "old.pdf"}]})
            docs_dir = dms_server.get_docs_dir()
            (docs_dir / dms_server._doc_filename("DOC-1", "old.pdf")).write_bytes(b"content")
            before_mtime = dms_server.get_index_path().stat().st_mtime_ns
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/DOC-1/rename-file", json={"name": "new.pdf"})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            after_mtime = dms_server.get_index_path().stat().st_mtime_ns
            self.assertEqual(before_mtime, after_mtime)
            self.assertEqual(dms_server.read_index()["docIndex"], [{"id": "DOC-1", "name": "old.pdf"}])


class DownloadDocsToFolderTests(unittest.TestCase):
    def test_copies_selected_docs_using_their_display_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            docs_dir = dms_server.get_docs_dir()
            (docs_dir / dms_server._doc_filename("DOC-1", "internal-name.pdf")).write_bytes(b"pdf-bytes")
            dms_server.write_index({
                "tree": {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []},
                "docIndex": [{"id": "DOC-1", "name": "Display Name.pdf"}],
            })
            target = tmp_path / "downloads"
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/download-to-folder", json={
                "doc_ids": ["DOC-1"], "target_path": str(target),
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["count"], 1)
            self.assertEqual(body["skipped"], [])

            dest = target / "Display Name.pdf"
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), b"pdf-bytes")
            # Original stays in place -- this is a copy, not a move.
            self.assertTrue((docs_dir / dms_server._doc_filename("DOC-1", "internal-name.pdf")).exists())

    def test_missing_docs_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            docs_dir = dms_server.get_docs_dir()
            (docs_dir / dms_server._doc_filename("DOC-1", "a.pdf")).write_bytes(b"data")
            dms_server.write_index({
                "tree": {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []},
                "docIndex": [{"id": "DOC-1", "name": "a.pdf"}],
            })
            target = tmp_path / "downloads"
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/download-to-folder", json={
                "doc_ids": ["DOC-1", "DOC-GONE"], "target_path": str(target),
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["count"], 1)
            self.assertEqual(body["skipped"], ["DOC-GONE"])

    def test_no_doc_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/download-to-folder", json={"doc_ids": []})
            self.assertEqual(resp.status_code, 400)

    def test_name_collision_gets_a_unique_suffix_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            docs_dir = dms_server.get_docs_dir()
            (docs_dir / dms_server._doc_filename("DOC-1", "a.pdf")).write_bytes(b"first")
            (docs_dir / dms_server._doc_filename("DOC-2", "b.pdf")).write_bytes(b"second")
            dms_server.write_index({
                "tree": {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []},
                "docIndex": [
                    {"id": "DOC-1", "name": "Report.pdf"},
                    {"id": "DOC-2", "name": "Report.pdf"},
                ],
            })
            target = tmp_path / "downloads"
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/download-to-folder", json={
                "doc_ids": ["DOC-1", "DOC-2"], "target_path": str(target),
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["count"], 2)

            contents = {p.read_bytes() for p in target.glob("Report*.pdf")}
            self.assertEqual(contents, {b"first", b"second"})
            self.assertEqual(len(list(target.glob("Report*.pdf"))), 2)


if __name__ == "__main__":
    unittest.main()

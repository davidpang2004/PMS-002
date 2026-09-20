"""Tier 2 hardening coverage: POST /api/docs/combine-to-folder merges
selected documents into one PDF and soft-deletes the sources into the
"Not Show in Tree" recycle bin -- structurally the same soft-delete
pattern already covered for convert-to-pdf (tests/test_convert_to_pdf.py)
and delete_node (tests/test_node_and_doc_delete.py), but untested itself.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import io
import tempfile
import unittest
from pathlib import Path

import dms_server
from pypdf import PdfReader

NOT_SHOW = dms_server.NOT_SHOW_FOLDER_NAME


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


def _make_pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


class CombineToFolderTests(unittest.TestCase):
    def _setup_two_docs(self, storage_root):
        tree = {"id": "NODE-ROOT", "name": "Root", "children": [
            {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [
                {"id": "DOC-20260101-AAAAAAAA"}, {"id": "DOC-20260101-BBBBBBBB"},
            ]},
            {"id": "NODE-DEST", "name": "Destination", "children": [], "documents": []},
        ], "documents": []}
        # Realistic-format ids (see genDocId in dms.html / the server's own
        # "DOC-" + yyyymmdd + "-" + token_hex(4) generation): short ids like
        # "DOC-1"/"DOC-2" can be an accidental *prefix* of a same-day
        # server-generated id (e.g. "DOC-2" matching the leading "DOC-2..."
        # of "DOC-20260920-..."), which false-matches in _find_doc_files'
        # substring search (`rglob(f"*{doc_id}*")`) and grabs the wrong
        # file. That's a real, if low-probability with real fixed-length
        # ids, latent bug in _find_doc_files itself -- worth a dedicated
        # test, not something to paper over here by picking safe ids and
        # saying nothing.
        doc_index = [
            {"id": "DOC-20260101-AAAAAAAA", "name": "one.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-A"},
            {"id": "DOC-20260101-BBBBBBBB", "name": "two.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-A"},
        ]
        dms_server.write_index({"tree": tree, "docIndex": doc_index})
        dms_server._create_local_folder_structure(tree)
        folder = dms_server._get_node_docs_dir("NODE-A", tree)
        (folder / dms_server._doc_filename("DOC-20260101-AAAAAAAA", "one.pdf")).write_bytes(_make_pdf_bytes("one"))
        (folder / dms_server._doc_filename("DOC-20260101-BBBBBBBB", "two.pdf")).write_bytes(_make_pdf_bytes("two"))
        return tree

    def test_combines_into_new_pdf_placed_in_target_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            self._setup_two_docs(storage_root)
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/combine-to-folder", json={
                "node_id": "NODE-DEST",
                "title": "Merged Report",
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-20260101-AAAAAAAA", "DOC-20260101-BBBBBBBB"]}],
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            new_doc_id = body["doc_id"]
            self.assertEqual(body["name"], "Merged Report.pdf")

            idx = dms_server.read_index()
            new_doc = next(d for d in idx["docIndex"] if d["id"] == new_doc_id)
            self.assertEqual(new_doc["originalNodeId"], "NODE-DEST")
            dest_node = dms_server._find_node(idx["tree"], "NODE-DEST")
            self.assertIn({"id": new_doc_id}, dest_node["documents"])

            dest_folder = dms_server._get_node_docs_dir("NODE-DEST", idx["tree"])
            matches = list(dest_folder.glob(f"*{new_doc_id}*"))
            self.assertEqual(len(matches), 1)
            reader = PdfReader(str(matches[0]))
            self.assertEqual(len(reader.pages), 2, "should contain both source docs' pages")

    def test_source_docs_unlinked_and_soft_deleted_to_recycle_bin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = self._setup_two_docs(storage_root)
            client = dms_server.app.test_client()

            resp = client.post("/api/docs/combine-to-folder", json={
                "node_id": "NODE-DEST",
                "title": "Merged",
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-20260101-AAAAAAAA", "DOC-20260101-BBBBBBBB"]}],
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            idx = dms_server.read_index()
            folder_a = dms_server._find_node(idx["tree"], "NODE-A")
            self.assertEqual(folder_a["documents"], [])

            recycle_children = [c for c in idx["tree"]["children"] if c.get("name") == NOT_SHOW]
            self.assertEqual(len(recycle_children), 1)
            recycle_doc_ids = {d["id"] for d in recycle_children[0]["documents"]}
            self.assertEqual(recycle_doc_ids, {"DOC-20260101-AAAAAAAA", "DOC-20260101-BBBBBBBB"})

            for doc_id in ("DOC-20260101-AAAAAAAA", "DOC-20260101-BBBBBBBB"):
                doc = next(d for d in idx["docIndex"] if d["id"] == doc_id)
                self.assertEqual(doc["originalNodeId"], recycle_children[0]["id"])
                # File itself must survive the move, not just the bookkeeping.
                matches = dms_server._find_doc_files(dms_server.get_docs_dir(), doc_id)
                self.assertEqual(len(matches), 1)

    def test_missing_node_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            self._setup_two_docs(storage_root)
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/combine-to-folder", json={
                "title": "x", "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-20260101-AAAAAAAA"]}],
            })
            self.assertEqual(resp.status_code, 400)

    def test_missing_selection_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            self._setup_two_docs(storage_root)
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/combine-to-folder", json={
                "node_id": "NODE-DEST", "title": "x", "selection": [],
            })
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

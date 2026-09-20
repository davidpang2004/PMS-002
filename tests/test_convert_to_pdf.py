"""Folder "Convert to PDF" action: each selected document becomes its own
standalone PDF (no merging, no renaming beyond the extension) and the
original files are moved into the recycle bin ("Not Show in Tree" /
"Deleted files").

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import tempfile
import unittest
from pathlib import Path

import dms_server
from pypdf import PdfReader


def _isolate(tmp_path: Path):
    storage_root = tmp_path / "storage"
    config_path = tmp_path / "config.json"
    dms_server.CONFIG_PATH = config_path
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


class ConvertToPdfTests(unittest.TestCase):
    def _setup_folder(self, storage_root):
        tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
            {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [
                {"id": "DOC-IMG"}, {"id": "DOC-PDF"},
            ]},
        ], "documents": []}
        doc_index = [
            {"id": "DOC-IMG", "name": "photo.jpg", "mime": "image/jpeg", "size": 1,
             "docDate": "2025-12-03"},
            {"id": "DOC-PDF", "name": "scan.pdf", "mime": "application/pdf", "size": 1},
        ]
        dms_server.write_index({"tree": tree, "docIndex": doc_index})
        dms_server._create_local_folder_structure(tree)
        folder = dms_server._get_node_docs_dir("NODE-A", tree)

        from PIL import Image
        Image.new("RGB", (3024, 4032), (30, 90, 150)).save(folder / "DOC-IMG__photo.jpg")

        from reportlab.pdfgen import canvas
        import io
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "already a pdf")
        c.showPage()
        c.save()
        (folder / "DOC-PDF__scan.pdf").write_bytes(buf.getvalue())
        return folder

    def test_converts_image_and_recycles_original(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            folder = self._setup_folder(storage_root)

            client = dms_server.app.test_client()
            resp = client.post("/api/docs/convert-to-pdf", json={
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-IMG", "DOC-PDF"]}],
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()

            # Only the image gets converted; the already-PDF doc is skipped.
            # The image has a docDate, so the output is date-prefixed.
            self.assertEqual(len(body["converted"]), 1)
            self.assertEqual(body["converted"][0]["name"], "2025-12-03-photo.pdf")
            self.assertEqual(len(body["skipped"]), 1)
            self.assertEqual(body["skipped"][0]["reason"], "Already a PDF")

            idx = dms_server.read_index()
            new_doc_id = body["converted"][0]["doc_id"]
            new_doc = next(d for d in idx["docIndex"] if d["id"] == new_doc_id)
            self.assertEqual(new_doc["name"], "2025-12-03-photo.pdf")
            self.assertEqual(new_doc["mime"], "application/pdf")

            # New PDF lives in the same folder, downscaled to the same
            # _MAX_EMBED_DIM cap the merge feature uses (keeps file size in
            # the same few-hundred-KB range rather than full sensor res).
            node_a = dms_server._find_node_by_id(idx["tree"], "NODE-A")
            self.assertIn({"id": new_doc_id}, node_a["documents"])
            new_path = list(folder.glob(f"*{new_doc_id}*"))[0]
            self.assertTrue(new_path.name.startswith("2025-12-03-photo+"), new_path.name)
            reader = PdfReader(str(new_path))
            self.assertEqual(len(reader.pages), 1)
            page = reader.pages[0]
            xo = page["/Resources"]["/XObject"].get_object()
            img_obj = next(v.get_object() for v in xo.values() if v.get_object().get("/Subtype") == "/Image")
            self.assertLessEqual(int(img_obj["/Width"]), 900)
            self.assertLessEqual(int(img_obj["/Height"]), 900)
            # Embedded as JPEG (DCTDecode), not a raw/deflate pixel array --
            # otherwise a multi-megapixel photo balloons into a huge PDF.
            filt = img_obj.get("/Filter")
            filt = [filt] if isinstance(filt, str) else list(filt)
            self.assertIn("/DCTDecode", filt)
            self.assertLess(new_path.stat().st_size, 500_000)

            # The original DOC-IMG is no longer linked in Folder A ...
            self.assertNotIn({"id": "DOC-IMG"}, node_a["documents"])
            # ...but DOC-PDF (skipped, already a PDF) is untouched and still there.
            self.assertIn({"id": "DOC-PDF"}, node_a["documents"])

            # ... and its file moved into the recycle bin ("Deleted files").
            recycle = next(c for c in idx["tree"]["children"] if c["name"] == dms_server.NOT_SHOW_FOLDER_NAME)
            self.assertIn({"id": "DOC-IMG"}, recycle["documents"])
            self.assertFalse((folder / "DOC-IMG__photo.jpg").exists())
            recycle_dir = dms_server._get_node_docs_dir(recycle["id"], idx["tree"])
            self.assertTrue(list(recycle_dir.glob("DOC-IMG__*")))

    def test_no_docdate_leaves_name_unprefixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-IMG"}]},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [
                {"id": "DOC-IMG", "name": "photo.jpg", "mime": "image/jpeg", "size": 1}]})
            dms_server._create_local_folder_structure(tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            from PIL import Image
            Image.new("RGB", (100, 100)).save(folder / "DOC-IMG__photo.jpg")

            client = dms_server.app.test_client()
            resp = client.post("/api/docs/convert-to-pdf", json={
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-IMG"]}],
            })
            body = resp.get_json()
            self.assertEqual(body["converted"][0]["name"], "photo.pdf")

    def test_name_already_starting_with_docdate_is_not_double_prefixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-IMG"}]},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [
                {"id": "DOC-IMG", "name": "2025-12-03-photo.jpg", "mime": "image/jpeg", "size": 1,
                 "docDate": "2025-12-03"}]})
            dms_server._create_local_folder_structure(tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            from PIL import Image
            Image.new("RGB", (100, 100)).save(folder / "DOC-IMG__2025-12-03-photo.jpg")

            client = dms_server.app.test_client()
            resp = client.post("/api/docs/convert-to-pdf", json={
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-IMG"]}],
            })
            body = resp.get_json()
            self.assertEqual(body["converted"][0]["name"], "2025-12-03-photo.pdf")

    def test_falls_back_to_exif_date_when_docdate_unset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-IMG"}]},
            ], "documents": []}
            # No docDate set -- only EXIF has the date.
            dms_server.write_index({"tree": tree, "docIndex": [
                {"id": "DOC-IMG", "name": "photo.jpg", "mime": "image/jpeg", "size": 1}]})
            dms_server._create_local_folder_structure(tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)

            import piexif
            from PIL import Image
            exif_bytes = piexif.dump({
                "Exif": {piexif.ExifIFD.DateTimeOriginal: "2024:06:15 10:30:00"},
            })
            Image.new("RGB", (400, 300)).save(folder / "DOC-IMG__photo.jpg", exif=exif_bytes)

            client = dms_server.app.test_client()
            resp = client.post("/api/docs/convert-to-pdf", json={
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-IMG"]}],
            })
            body = resp.get_json()
            self.assertEqual(body["converted"][0]["name"], "2024-06-15-photo.pdf")

    def test_rejects_empty_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            dms_server.write_index({"tree": {"id": "NODE-ROOT", "name": "P",
                                             "children": [], "documents": []},
                                    "docIndex": []})
            client = dms_server.app.test_client()
            resp = client.post("/api/docs/convert-to-pdf", json={"selection": []})
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

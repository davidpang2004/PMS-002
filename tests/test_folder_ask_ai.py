"""Folder-level "Ask AI": explicit-selection Q&A, per-folder Q&A log, and
the request-cover-page PDF for external tools.

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import io
import json
import sys
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


class AppendQaLogTests(unittest.TestCase):
    def test_creates_then_appends_folder_qa_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            dms_server._create_local_folder_structure(tree)
            docs_dir = storage_root / "docs"

            idx = dms_server.read_index()
            node = dms_server._find_node_by_id(idx["tree"], "NODE-A")

            log_id, log_name = dms_server._append_qa_log(
                idx, node, "NODE-A", "Folder A",
                {"timestamp": "2026-01-01 10:00", "model": "test",
                 "question": "Q1?", "answer": "A1."},
                docs_dir,
            )
            self.assertTrue(log_id)
            self.assertEqual(log_name, "Folder A - AI Q&A.pdf")

            idx2 = dms_server.read_index()
            entry = next(d for d in idx2["docIndex"] if d["id"] == log_id)
            self.assertEqual(len(entry["aiQaLog"]), 1)
            self.assertIn({"id": log_id},
                          dms_server._find_node_by_id(idx2["tree"], "NODE-A")["documents"])

            # Second question appends rather than replacing.
            node2 = dms_server._find_node_by_id(idx2["tree"], "NODE-A")
            log_id2, _ = dms_server._append_qa_log(
                idx2, node2, "NODE-A", "Folder A",
                {"timestamp": "2026-01-01 11:00", "model": "test",
                 "question": "Q2?", "answer": "A2."},
                docs_dir,
            )
            self.assertEqual(log_id2, log_id)
            idx3 = dms_server.read_index()
            entry3 = next(d for d in idx3["docIndex"] if d["id"] == log_id)
            self.assertEqual([r["question"] for r in entry3["aiQaLog"]], ["Q1?", "Q2?"])

            log_files = list(docs_dir.rglob(f"{log_id}__*"))
            self.assertEqual(len(log_files), 1)
            self.assertGreater(len(PdfReader(str(log_files[0])).pages), 0)


class FolderAiPdfTests(unittest.TestCase):
    def test_builds_request_cover_plus_document_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [
                    {"id": "DOC-1"}, {"id": "DOC-2"}, {"id": "DOC-3"},
                ]},
            ], "documents": []}
            doc_index = [
                {"id": "DOC-1", "name": "notes.txt", "mime": "text/plain"},
                {"id": "DOC-2", "name": "scan.pdf", "mime": "application/pdf"},
                {"id": "DOC-3", "name": "photo.jpg", "mime": "image/jpeg",
                 "docDate": "2025-12-03", "lat": 42.37817, "lon": -71.04874,
                 "description": "north face of the pier",
                 "metadata": {"Location": {"actual": "Boston, Massachusetts"}}},
            ]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)
            docs_dir = storage_root / "docs"
            folder_a = dms_server._get_node_docs_dir("NODE-A", tree)
            (folder_a / "DOC-1__notes.txt").write_text("hello world")
            # A real one-page PDF for DOC-2 so it embeds (not a placeholder).
            from reportlab.pdfgen import canvas
            buf = io.BytesIO()
            c = canvas.Canvas(buf)
            c.drawString(72, 720, "scan page")
            c.showPage()
            c.save()
            (folder_a / "DOC-2__scan.pdf").write_bytes(buf.getvalue())
            from PIL import Image
            Image.new("RGB", (240, 180), (30, 90, 150)).save(folder_a / "DOC-3__photo.jpg")

            client = dms_server.app.test_client()
            resp = client.post("/api/folder-ai-pdf", json={
                "node_id": "NODE-A",
                "request": "Please review these files.",
                "selection": [{"nodeId": "NODE-A", "docIds": ["DOC-1", "DOC-2", "DOC-3"]}],
            })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "application/pdf")

            reader = PdfReader(io.BytesIO(resp.data))
            # request cover + placeholder for the .txt + 1 pdf page + 1 photo page
            self.assertEqual(len(reader.pages), 4)
            self.assertIn("Please review these files.", reader.pages[0].extract_text())
            # cover page carries no document filename list
            self.assertNotIn("photo.jpg", reader.pages[0].extract_text())

            # the photo page actually embeds an image (not just its name)
            def _image_count(page):
                xo = page.get("/Resources", {}).get("/XObject")
                if not xo:
                    return 0
                return sum(1 for v in xo.get_object().values()
                           if v.get_object().get("/Subtype") == "/Image")
            self.assertEqual(_image_count(reader.pages[3]), 1)

            # the photo page carries its date / location / note as a caption
            photo_text = reader.pages[3].extract_text()
            self.assertIn("2025-12-03", photo_text)
            self.assertIn("Boston, Massachusetts", photo_text)
            self.assertIn("42.37817", photo_text)
            self.assertIn("north face of the pier", photo_text)

            saved = resp.headers.get("X-Databook-Saved-Path")
            self.assertTrue(saved)

    def test_rejects_empty_request_or_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            dms_server.write_index({"tree": {"id": "NODE-ROOT", "name": "P",
                                             "children": [], "documents": []},
                                    "docIndex": []})
            client = dms_server.app.test_client()
            self.assertEqual(client.post("/api/folder-ai-pdf", json={
                "node_id": "NODE-ROOT", "request": "", "selection": [{"nodeId": "x", "docIds": ["y"]}]}).status_code, 400)
            self.assertEqual(client.post("/api/folder-ai-pdf", json={
                "node_id": "NODE-ROOT", "request": "hi", "selection": []}).status_code, 400)


class AskAiExplicitSelectionTests(unittest.TestCase):
    def test_doc_ids_restrict_candidates_and_archive_files_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            dms_server.save_config({
                "storage_path": str(storage_root),
                "gemini_api_key_enc": "fake-enc",
            })
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [
                    {"id": "DOC-1"}, {"id": "DOC-2"}, {"id": "DOC-3"},
                ]},
            ], "documents": []}
            doc_index = [
                {"id": "DOC-1", "name": "a.txt", "description": "alpha text"},
                {"id": "DOC-2", "name": "b.txt", "description": "beta text"},
                {"id": "DOC-3", "name": "c.txt", "description": "gamma text"},
            ]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)

            captured = {}

            def fake_answer(question, snippets, api_key, **kw):
                captured["ids"] = [s["doc_id"] for s in snippets]
                return {"answer": "stub answer", "cited_doc_ids": ["DOC-1"], "model": "stub"}

            import ai_extraction
            orig_answer = ai_extraction.answer_question_with_gemini
            orig_decrypt = dms_server.decrypt_secret
            ai_extraction.answer_question_with_gemini = fake_answer
            dms_server.decrypt_secret = lambda enc: "key"
            try:
                client = dms_server.app.test_client()
                resp = client.post("/api/ask-ai", json={
                    "question": "what is this?",
                    "doc_ids": ["DOC-1", "DOC-3"],
                    "archive": True,
                    "node_id": "NODE-A",
                    "provider": "gemini",
                })
            finally:
                ai_extraction.answer_question_with_gemini = orig_answer
                dms_server.decrypt_secret = orig_decrypt

            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["answer"], "stub answer")
            self.assertEqual(sorted(captured["ids"]), ["DOC-1", "DOC-3"])
            self.assertEqual(body["log_name"], "Folder A - AI Q&A.pdf")

            idx = dms_server.read_index()
            log = next(d for d in idx["docIndex"] if d.get("name") == "Folder A - AI Q&A.pdf")
            self.assertEqual(len(log["aiQaLog"]), 1)
            self.assertEqual(log["aiQaLog"][0]["question"], "what is this?")


class AskAiVisionTests(unittest.TestCase):
    def test_image_docs_go_to_multimodal_with_page_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            dms_server.save_config({
                "storage_path": str(storage_root),
                "gemini_api_key_enc": "fake-enc",
            })
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [
                    {"id": "DOC-IMG"},
                ]},
            ], "documents": []}
            doc_index = [{"id": "DOC-IMG", "name": "photo.jpg", "mime": "image/jpeg"}]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            from PIL import Image
            Image.new("RGB", (32, 24), (200, 120, 60)).save(folder / "DOC-IMG__photo.jpg")

            captured = {}

            def fake_mm(question, doc_items, api_key, **kw):
                captured["items"] = doc_items
                return {"answer": "a red-brown rectangle", "cited_doc_ids": ["DOC-IMG"], "model": "gemini"}

            import ai_extraction
            orig_mm = ai_extraction.answer_question_with_gemini_multimodal
            orig_txt = ai_extraction.answer_question_with_gemini
            orig_decrypt = dms_server.decrypt_secret
            ai_extraction.answer_question_with_gemini_multimodal = fake_mm
            ai_extraction.answer_question_with_gemini = lambda *a, **k: (_ for _ in ()).throw(AssertionError("text path used"))
            dms_server.decrypt_secret = lambda enc: "key"
            try:
                resp = dms_server.app.test_client().post("/api/ask-ai", json={
                    "question": "what is in this photo?",
                    "doc_ids": ["DOC-IMG"],
                    "node_id": "NODE-A",
                    "provider": "gemini",
                })
            finally:
                ai_extraction.answer_question_with_gemini_multimodal = orig_mm
                ai_extraction.answer_question_with_gemini = orig_txt
                dms_server.decrypt_secret = orig_decrypt

            self.assertEqual(resp.status_code, 200, resp.get_json())
            body = resp.get_json()
            self.assertTrue(body["visionUsed"])
            self.assertEqual(body["imagesUsed"], 1)
            self.assertEqual(len(captured["items"][0]["images"]), 1)
            self.assertEqual(captured["items"][0]["images"][0][:3], b"\xff\xd8\xff")

    def test_vision_false_keeps_text_only_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            dms_server.save_config({"storage_path": str(storage_root), "gemini_api_key_enc": "fake-enc"})
            tree = {"id": "NODE-ROOT", "name": "Proj", "children": [
                {"id": "NODE-A", "name": "A", "children": [], "documents": [{"id": "DOC-IMG"}]},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [
                {"id": "DOC-IMG", "name": "photo.jpg", "mime": "image/jpeg", "description": "a wall"}]})
            dms_server._create_local_folder_structure(tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            from PIL import Image
            Image.new("RGB", (16, 16)).save(folder / "DOC-IMG__photo.jpg")

            import ai_extraction
            orig_txt = ai_extraction.answer_question_with_gemini
            orig_decrypt = dms_server.decrypt_secret
            ai_extraction.answer_question_with_gemini = lambda q, s, k, **kw: {"answer": "ok", "cited_doc_ids": [], "model": "g"}
            dms_server.decrypt_secret = lambda enc: "key"
            try:
                resp = dms_server.app.test_client().post("/api/ask-ai", json={
                    "question": "q", "doc_ids": ["DOC-IMG"], "node_id": "NODE-A",
                    "provider": "gemini", "vision": False,
                })
            finally:
                ai_extraction.answer_question_with_gemini = orig_txt
                dms_server.decrypt_secret = orig_decrypt
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.get_json()["visionUsed"])


class RenderHeicTests(unittest.TestCase):
    def test_render_pages_for_ai_handles_heic_when_pillow_heif_present(self):
        try:
            import pillow_heif  # noqa: F401
        except ImportError:
            self.skipTest("pillow-heif not installed")
        import pdf_extraction
        with tempfile.TemporaryDirectory() as tmpdir:
            from PIL import Image
            pillow_heif.register_heif_opener()
            src = Path(tmpdir) / "p.heic"
            Image.new("RGB", (4000, 3000), (10, 20, 30)).save(src, format="HEIF")
            out = pdf_extraction.render_pages_for_ai(src, max_pages=2)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0][:3], b"\xff\xd8\xff")  # JPEG
            from PIL import Image as I
            import io as _io
            self.assertLessEqual(max(I.open(_io.BytesIO(out[0])).size), 1600)


if __name__ == "__main__":
    unittest.main()

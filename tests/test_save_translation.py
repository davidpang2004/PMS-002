"""POST /api/docs/<id>/save-translation: saves a PDF's translation as
"<original stem> Chinese.pdf" in the original's folder."""
import tempfile
import unittest
from pathlib import Path

import dms_server


class SaveTranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "storage"
        dms_server.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        dms_server._storage_path_override = str(root)
        dms_server.save_config({"storage_path": str(root)})
        self.root = root
        dms_server.app.testing = True
        self.client = dms_server.app.test_client()

    def test_saves_named_pdf_in_root(self):
        r = self.client.post("/api/docs/DOC-1/save-translation", json={
            "pages": {"1": "这是一个测试。\n第二行", "2": "第二页"},
            "language": "Simplified Chinese (简体中文)", "name": "Report.pdf"})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertEqual(d["name"], "Report Chinese.pdf")
        files = list(self.root.rglob("*.pdf"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].read_bytes().startswith(b"%PDF"))

    def test_second_save_does_not_collide(self):
        body = {"pages": {"1": "x"}, "language": "", "name": "A.pdf"}
        n1 = self.client.post("/api/docs/D/save-translation", json=body).get_json()["name"]
        n2 = self.client.post("/api/docs/D/save-translation", json=body).get_json()["name"]
        self.assertEqual((n1, n2), ("A Chinese.pdf", "A Chinese (2).pdf"))

    def test_one_page_per_source_page_with_source_sizes(self):
        import pymupdf
        r = self.client.post("/api/docs/D/save-translation", json={
            "pages": {"1": "长文本。" * 800, "3": "第三页"}, "name": "B.pdf",
            "sizes": [[612, 792], [300, 400], [612, 792], [200, 200]]})
        self.assertEqual(r.status_code, 200, r.get_json())
        pdf = pymupdf.open(next(self.root.rglob("*.pdf")))
        self.assertEqual(len(pdf), 4)               # blanks keep pages aligned
        self.assertEqual(round(pdf[1].rect.width), 300)
        self.assertIn("第三页", pdf[2].get_text())
        self.assertIn("长文本", pdf[0].get_text())

    def test_empty_pages_rejected(self):
        r = self.client.post("/api/docs/D/save-translation", json={"pages": {}})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()


class EditTranslationTests(unittest.TestCase):
    setUp = SaveTranslationTests.setUp

    def test_get_and_put_round_trip(self):
        r = self.client.post("/api/docs/D/save-translation", json={
            "pages": {"1": "第一行\n第二行", "3": "第三页"}, "name": "E.pdf",
            "sizes": [[612, 792], [300, 400], [612, 792]]})
        doc_id = r.get_json()["doc_id"]
        g = self.client.get(f"/api/docs/{doc_id}/translation-text").get_json()
        self.assertEqual(g["count"], 3)
        self.assertEqual(g["pages"]["1"], "第一行\n第二行")
        self.assertEqual(g["pages"].get("2", ""), "")
        p = self.client.put(f"/api/docs/{doc_id}/translation-text", json={
            "pages": {"1": "改过的文字", "2": "新增第二页", "3": "第三页"}})
        self.assertEqual(p.status_code, 200, p.get_json())
        g2 = self.client.get(f"/api/docs/{doc_id}/translation-text").get_json()
        self.assertEqual(g2["pages"]["1"], "改过的文字")
        self.assertEqual(g2["pages"]["2"], "新增第二页")
        import pymupdf
        pdf = pymupdf.open(next(self.root.rglob("*.pdf")))
        self.assertEqual(len(pdf), 3)
        self.assertEqual(round(pdf[1].rect.width), 300)
        self.assertIn("改过的文字", pdf[0].get_text())

    def test_missing_doc_404(self):
        self.assertEqual(self.client.get("/api/docs/nope/translation-text").status_code, 404)


class MarkupTests(unittest.TestCase):
    def test_markup_renders_color(self):
        import pymupdf
        pdf = pymupdf.open("pdf", dms_server._build_translation_pdf(
            [(1, "普通 [[y:黄色高亮]] 和 [[r:红字]]\n第二行")]))
        d = pdf[0]
        self.assertIn("黄色高亮", d.get_text())
        self.assertNotIn("[[", d.get_text())
        self.assertTrue(d.get_drawings())  # highlight background


class PasteTextTests(unittest.TestCase):
    setUp = SaveTranslationTests.setUp

    def test_paste_text_makes_multipage_pdf_in_folder(self):
        import pymupdf
        tree = {"id": "root", "name": "Root", "children": [], "documents": []}
        dms_server.write_index({"tree": tree, "docIndex": []})
        text = "血糖 CBC 测试\n" + "第二段很长的内容。 " * 2000
        r = self.client.post("/api/nodes/root/paste-text", json={"text": text})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertTrue(d["name"].startswith("Pasted text ") and d["name"].endswith(".pdf"))
        pdf = pymupdf.open(next(self.root.rglob("*.pdf")))
        self.assertGreater(len(pdf), 1)
        self.assertIn("血糖", pdf[0].get_text())

    def test_empty_text_rejected(self):
        self.assertEqual(self.client.post("/api/nodes/root/paste-text", json={"text": "  "}).status_code, 400)


class LayoutTranslationTests(unittest.TestCase):
    setUp = SaveTranslationTests.setUp

    def _table_pdf(self, path):
        import io, pymupdf
        html = ('<table style="width:100%"><tr><td style="width:130pt"><b>Hemoglobin</b></td>'
                '<td style="width:280pt">Complete this on or around the date. Must be done by next year</td>'
                '<td>Go to CPL</td></tr><tr><td><b>Thyroid</b></td><td>Fasting required</td>'
                '<td>12345</td></tr></table>')
        st = pymupdf.Story(html=html)
        buf = io.BytesIO()
        w = pymupdf.DocumentWriter(buf)
        d = w.begin_page(pymupdf.paper_rect("letter"))
        st.place(pymupdf.Rect(40, 40, 572, 752)); st.draw(d); w.end_page(); w.close()
        Path(path).write_bytes(buf.getvalue())

    def test_units_keep_columns_and_translations_land_in_place(self):
        import pymupdf, layout_translate as L
        src = Path(self.tmp.name) / "t.pdf"
        self._table_pdf(src)
        fake = {"Hemoglobin": "血红蛋白", "Thyroid": "甲状腺",
                "Fasting required": "需要空腹",
                "Complete this on or around the date. Must be done by next year": "请在该日期前后完成。必须在明年前完成"}
        units, _ = L.build_translated_units(src, lambda ts: [fake.get(t, "译") for t in ts], True)
        srcs = [u["src"] for u in units[1]]
        self.assertIn("Hemoglobin", srcs)
        # a wrapped cell is one unit; neighbouring columns stay separate units
        self.assertIn("Complete this on or around the date. Must be done by next year", srcs)
        numeric = next(u for u in units[1] if u["src"] == "12345")
        self.assertEqual(numeric["text"], "")      # numbers are left alone
        out = pymupdf.open("pdf", L.render(src, units, dms_server._translation_markup_to_html))
        text = out[0].get_text()
        self.assertIn("血红蛋白", text); self.assertIn("需要空腹", text)
        self.assertNotIn("Hemoglobin", text); self.assertIn("12345", text)
        # translation sits where the original text was
        a = next(u for u in units[1] if u["src"] == "Thyroid")["rect"]
        hit = out[0].search_for("甲状腺")[0]
        self.assertLess(abs(hit.y0 - a[1]), 8); self.assertLess(abs(hit.x0 - a[0]), 8)

    def test_route_saves_layout_pdf_and_it_is_editable(self):
        import pymupdf
        tree = {"id": "root", "name": "Root", "children": [], "documents": []}
        dms_server.write_index({"tree": tree, "docIndex": []})
        src_id = "SRC1"
        # place the source PDF where the server expects documents
        dd = dms_server.get_docs_dir(); dd.mkdir(parents=True, exist_ok=True)
        self._table_pdf(dd / dms_server._doc_filename(src_id, "Orders.pdf"))
        orig = dms_server._translate_texts
        dms_server._translate_texts = lambda texts, *a, **k: ["译:" + t for t in texts]
        try:
            r = self.client.post(f"/api/docs/{src_id}/save-translation-layout",
                                 json={"provider": "google", "name": "Orders.pdf", "language": "", "node_id": None})
        finally:
            dms_server._translate_texts = orig
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertEqual(d["name"], "Orders Chinese.pdf")
        g = self.client.get(f"/api/docs/{d['doc_id']}/translation-text").get_json()
        self.assertTrue(g["layout"])
        units = g["units"]["1"]
        edited = [u["text"] for u in units]
        i = next(n for n, u in enumerate(units) if u["src"] == "Thyroid")
        edited[i] = "[[y:甲状腺功能]]"
        p = self.client.put(f"/api/docs/{d['doc_id']}/translation-text", json={"units": {"1": edited}})
        self.assertEqual(p.status_code, 200, p.get_json())
        out = pymupdf.open(next(self.root.rglob("Orders Chinese*.pdf")))
        self.assertIn("甲状腺功能", out[0].get_text())
        self.assertNotIn("[[", out[0].get_text())

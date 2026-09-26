"""Auto-organized Word databook (databook.build_databook_docx): nested
chapter numbering from a folder's subfolders, document order preserved,
each document's free-text note printed unlabeled (omitted when empty),
missing-file placeholders, and PDF pages rasterized into the .docx via
PyMuPDF.

Pure-function tests against databook.py directly -- no server/config
involved, so no isolation setup is needed (see feedback_dms_config_isolation
memory, which only applies to dms_server.load_config()/save_config()).
"""
import io
import tempfile
import unittest
from pathlib import Path

import docx
from PIL import Image
from reportlab.pdfgen import canvas

import databook


def _make_tree_and_docs(docs_dir: Path):
    (docs_dir / "NODE-A").mkdir(parents=True)
    Image.new("RGB", (800, 600), color=(200, 50, 50)).save(
        docs_dir / "NODE-A" / "photo1+DOC-1.jpg", "JPEG"
    )

    (docs_dir / "NODE-B").mkdir(parents=True)
    pdf_path = docs_dir / "NODE-B" / "report+DOC-2.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 700, "Page one")
    c.showPage()
    c.drawString(100, 700, "Page two")
    c.showPage()
    c.save()

    tree = {
        "id": "NODE-ROOT", "name": "Root Folder", "documents": [], "children": [
            {"id": "NODE-A", "name": "Chapter A", "documents": [{"id": "DOC-1"}], "children": [
                {"id": "NODE-A1", "name": "Sub A1", "documents": [{"id": "DOC-3"}], "children": []},
            ]},
            {"id": "NODE-B", "name": "Chapter B", "documents": [{"id": "DOC-2"}], "children": []},
        ],
    }
    doc_index = [
        {"id": "DOC-1", "name": "photo1.jpg", "mime": "image/jpeg", "description": "A red square photo"},
        {"id": "DOC-2", "name": "report.pdf", "mime": "application/pdf", "description": ""},
        {"id": "DOC-3", "name": "missing.jpg", "mime": "image/jpeg", "description": "gone"},
    ]
    return tree, doc_index


class BuildDatabookDocxTests(unittest.TestCase):
    def test_nested_chapters_order_descriptions_and_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            docs_dir = Path(td) / "docs"
            tree, doc_index = _make_tree_and_docs(docs_dir)

            out = databook.build_databook_docx(
                node_id="NODE-ROOT", tree=tree, doc_index=doc_index, docs_dir=docs_dir,
                title="Test Databook", subtitle="Sub",
            )
            d = docx.Document(io.BytesIO(out))
            texts = [p.text for p in d.paragraphs if p.text.strip()]

            # Chapter numbering: top-level "1"/"2", nested subfolder "1.1".
            self.assertIn("1  Chapter A", texts)
            self.assertIn("1.1  Sub A1", texts)
            self.assertIn("2  Chapter B", texts)

            # Chapter A's chapter heading precedes its document, which
            # precedes the nested Sub A1 chapter -- i.e. tree order preserved.
            self.assertLess(texts.index("1  Chapter A"), texts.index("photo1.jpg"))
            self.assertLess(texts.index("photo1.jpg"), texts.index("1.1  Sub A1"))

            # Every document with a note is immediately followed by that text,
            # unlabeled (no "Description:" prefix -- it reads as a caption).
            self.assertIn("A red square photo", texts)
            self.assertIn("gone", texts)
            # DOC-2 has no description -- nothing extra is printed for it.
            self.assertNotIn("Description: ", texts)
            self.assertFalse(any(t.startswith("Description") for t in texts))

            # Missing on-disk file gets a placeholder instead of failing the build.
            self.assertIn("⚠ File not found on disk", texts)

            # 1 photo + 2 rasterized PDF pages = 3 embedded pictures.
            self.assertEqual(len(d.inline_shapes), 3)

    def test_blank_cover_when_none_supplied(self):
        with tempfile.TemporaryDirectory() as td:
            docs_dir = Path(td) / "docs"
            tree, doc_index = _make_tree_and_docs(docs_dir)
            out = databook.build_databook_docx(
                node_id="NODE-ROOT", tree=tree, doc_index=doc_index, docs_dir=docs_dir,
                title="T", cover_pages=None,
            )
            d = docx.Document(io.BytesIO(out))
            # First page holds nothing but the initial empty paragraph
            # python-docx always starts a document with; the title text is
            # the first real content and must land after a page break.
            first_para = d.paragraphs[0]
            self.assertEqual(first_para.text, "")

    def test_unknown_node_id_raises(self):
        with tempfile.TemporaryDirectory() as td:
            docs_dir = Path(td) / "docs"
            tree, doc_index = _make_tree_and_docs(docs_dir)
            with self.assertRaises(ValueError):
                databook.build_databook_docx(
                    node_id="NODE-DOES-NOT-EXIST", tree=tree, doc_index=doc_index, docs_dir=docs_dir,
                )

    def test_empty_folder_raises(self):
        with tempfile.TemporaryDirectory() as td:
            docs_dir = Path(td) / "docs"
            docs_dir.mkdir()
            tree = {"id": "NODE-ROOT", "name": "Empty", "documents": [], "children": []}
            with self.assertRaises(ValueError):
                databook.build_databook_docx(
                    node_id="NODE-ROOT", tree=tree, doc_index=[], docs_dir=docs_dir,
                )


if __name__ == "__main__":
    unittest.main()

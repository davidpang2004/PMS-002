"""Send-to-iPhone: folder -> token link -> token-gated gallery/file routes."""
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


class SendToIphoneTests(unittest.TestCase):
    def test_link_gallery_and_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "N1", "name": "Root", "documents": [{"id": "DOC-20260101-AAAAAA"}, {"id": "DOC-20260101-BBBBBB"}],
                    "children": [{"id": "N2", "name": "Sub", "children": [], "documents": [{"id": "DOC-20260101-CCCCCC"}]}]}
            dms_server.write_index({"tree": tree, "docIndex": [
                {"id": "DOC-20260101-AAAAAA", "name": "a.jpg", "mime": "image/jpeg"},
                {"id": "DOC-20260101-BBBBBB", "name": "b.pdf", "mime": "application/pdf"},
                {"id": "DOC-20260101-CCCCCC", "name": "c.mov", "mime": "video/quicktime"}]})
            docs = dms_server.get_docs_dir()
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "a+DOC-20260101-AAAAAA.jpg").write_bytes(b"JPEGDATA")
            c = dms_server.app.test_client()

            r = c.post("/api/send-to-iphone-link", json={"node_id": "N1", "recursive": False})
            self.assertEqual((r.get_json()["count"], r.get_json()["videos"]), (1, 0))
            r = c.post("/api/send-to-iphone-link", json={"node_id": "N1", "recursive": True})
            j = r.get_json()
            self.assertEqual((j["count"], j["photos"], j["videos"]), (2, 1, 1))
            token = j["url"].rsplit("/", 1)[1]

            self.assertIn("a.jpg", c.get(f"/send/{token}").get_data(as_text=True))
            self.assertEqual(c.get(f"/send/{token}/file/DOC-20260101-AAAAAA").data, b"JPEGDATA")
            lst = c.get(f"/send/{token}/list.txt").get_data(as_text=True).splitlines()
            self.assertEqual(len(lst), 2)
            self.assertTrue(lst[0].startswith("http://") and lst[0].endswith("/a.jpg"))
            self.assertEqual(c.get("/send/%s/file/DOC-20260101-AAAAAA/a.jpg" % token).data, b"JPEGDATA")
            sc = c.get("/send/DMS-Photos.shortcut")
            self.assertEqual(sc.status_code, 200)
            self.assertIn("DMS Photos.shortcut", sc.headers["Content-Disposition"])
            self.assertIn("shortcuts://run-shortcut", c.get(f"/send/{token}").get_data(as_text=True))
            # not part of the token (a PDF) -> 404; bad token -> 410
            self.assertEqual(c.get(f"/send/{token}/file/DOC-20260101-BBBBBB").status_code, 404)
            self.assertEqual(c.get("/send/nope").status_code, 410)

    def test_empty_folder_is_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "N1", "name": "R", "children": [], "documents": []}, "docIndex": []})
            r = dms_server.app.test_client().post("/api/send-to-iphone-link", json={"node_id": "N1"})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

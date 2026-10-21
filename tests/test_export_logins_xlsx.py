"""GET /api/export-logins-xlsx: one row per saved website login."""
import io
import tempfile
import unittest
from pathlib import Path

import dms_server


class ExportLoginsXlsxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "storage"
        dms_server.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        dms_server.SECRET_KEY_PATH = Path(self.tmp.name) / "secret.key"
        dms_server._storage_path_override = str(root)
        dms_server.save_config({"storage_path": str(root)})
        dms_server.app.testing = True
        self.client = dms_server.app.test_client()

    def test_rows(self):
        tree = {"id": "R", "name": "Root", "children": [
            {"id": "A", "name": "Bank", "description": "line1<br>a &amp; b", "children": [], "logins": [
                {"id": "L1", "websiteUrl": "https://a.com", "accountName": "u1", "password": "=pw1"},
                {"id": "L2", "websiteUrl": "https://b.com", "accountName": "u2", "password": "pw2"}]},
            {"id": "B", "name": "NoLogin", "children": []},
            {"id": "C", "name": "Legacy", "children": [], "websiteUrl": "https://c.com",
             "accountName": "u3", "password": "pw3"},
        ]}
        r = self.client.put("/api/tree", json={"tree": tree})
        self.assertEqual(r.status_code, 200, r.get_json())
        opened = []
        dms_server._open_with_default_app = opened.append
        r = self.client.post("/api/export-logins-xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(opened, [r.get_json()["path"]])
        from openpyxl import load_workbook
        rows = list(load_workbook(opened[0]).active.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("Folder", "Website", "User name", "Password", "Folder description"))
        self.assertEqual(rows[1], ("Bank", "https://a.com", "u1", "=pw1", "line1\na & b"))
        self.assertEqual(rows[2], ("Bank", "https://b.com", "u2", "pw2", "line1\na & b"))
        self.assertEqual(rows[3], ("Legacy", "https://c.com", "u3", "pw3", None))
        self.assertEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()

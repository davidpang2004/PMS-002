"""Tier 1 hardening coverage: POST /api/project/new creates a fresh project
and switches the server to it -- the main safety property worth locking in
is that it refuses to touch a non-empty target folder rather than silently
overwriting someone's existing files.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import json
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


class NewProjectTests(unittest.TestCase):
    def test_creates_fresh_project_and_switches_to_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            target = tmp_path / "brand-new"
            client = dms_server.app.test_client()

            resp = client.post("/api/project/new", json={"target_path": str(target)})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            self.assertEqual(dms_server.get_storage_root(), target.resolve())
            idx = json.loads((target / "index.json").read_text())
            # Deliberately not None -- see new_project()'s comment: tree=None
            # means "first run, seed the demo project" to the frontend, which
            # would make a brand new project look like leftover sample data.
            self.assertIsNotNone(idx["tree"])
            self.assertEqual(idx["tree"]["children"], [])
            self.assertEqual(idx["docIndex"], [])
            self.assertTrue((target / "docs").is_dir())

    def test_refuses_a_non_empty_target_and_leaves_it_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            target = tmp_path / "occupied"
            target.mkdir()
            existing_file = target / "do-not-touch.txt"
            existing_file.write_text("precious data")
            client = dms_server.app.test_client()

            resp = client.post("/api/project/new", json={"target_path": str(target)})
            self.assertEqual(resp.status_code, 400)

            self.assertEqual(existing_file.read_text(), "precious data")
            self.assertFalse((target / "index.json").exists())
            # Must not have switched the running server to point at a
            # folder it refused to initialize.
            self.assertNotEqual(dms_server.get_storage_root(), target.resolve())

    def test_missing_target_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            resp = client.post("/api/project/new", json={})
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

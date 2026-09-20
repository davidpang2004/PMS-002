"""Tier 1 hardening coverage: PUT /api/doc-index wholesale-replaces
idx["docIndex"] -- same blast radius as PUT /api/tree (a bad payload wipes
every document's metadata in one write_index() call) but had no coverage.

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


class DocIndexRouteTests(unittest.TestCase):
    def test_put_then_get_round_trips_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            doc_index = [
                {"id": "DOC-1", "name": "a.pdf", "mime": "application/pdf"},
                {"id": "DOC-2", "name": "b.jpg", "mime": "image/jpeg"},
            ]
            resp = client.put("/api/doc-index", json={"docIndex": doc_index})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            resp = client.get("/api/doc-index")
            self.assertEqual(resp.get_json()["docIndex"], doc_index)

    def test_put_doc_index_does_not_disturb_the_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Family Archive", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [{"id": "DOC-1", "name": "old.pdf"}]})
            client = dms_server.app.test_client()

            client.put("/api/doc-index", json={"docIndex": [{"id": "DOC-2", "name": "new.pdf"}]})

            saved_tree = dms_server.read_index()["tree"]
            self.assertEqual(saved_tree, tree)

    def test_missing_doc_index_key_wipes_to_empty_list(self):
        """Documents current (risky) behavior: put_doc_index() does
        `data.get("docIndex", [])`, so a malformed/incomplete payload -- the
        key missing entirely, e.g. from a client bug -- silently wipes every
        document's metadata rather than rejecting the request. Flagging via
        a test rather than "fixing" silently: this may be intentional (an
        explicit `docIndex: []` is how the client clears everything when a
        project goes empty) and changing it is a product decision, not
        just a bug fix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [{"id": "DOC-1", "name": "a.pdf"}]})
            client = dms_server.app.test_client()

            resp = client.put("/api/doc-index", json={})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(dms_server.read_index()["docIndex"], [])


if __name__ == "__main__":
    unittest.main()

"""Tier 1 hardening coverage: DELETE /api/nodes/<id> (delete_node) and
DELETE /api/docs/<id> (delete_doc) are the two most destructive routes in
dms_server.py -- delete_node in particular has a fair amount of logic
(soft-delete to a recycle bin, subtree sweep, a disk-vs-bookkeeping
mismatch guard) that had no coverage at all before this.

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import tempfile
import unittest
from pathlib import Path

import dms_server

NOT_SHOW = dms_server.NOT_SHOW_FOLDER_NAME


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


class DeleteNodeTests(unittest.TestCase):
    def _write_doc_file(self, storage_root, node_id, tree, doc_id, name, content=b"hello"):
        folder = dms_server._get_node_docs_dir(node_id, tree)
        path = folder / dms_server._doc_filename(doc_id, name)
        path.write_bytes(content)
        return path

    def test_delete_leaf_node_moves_doc_to_recycle_bin_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [],
                 "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            doc_index = [{"id": "DOC-1", "name": "photo.jpg", "mime": "image/jpeg",
                          "size": 5, "originalNodeId": "NODE-A"}]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)
            self._write_doc_file(storage_root, "NODE-A", tree, "DOC-1", "photo.jpg", b"orig-bytes")

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-A")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["savedDocs"], 1)
            recycle_id = body["recycleBinId"]
            self.assertTrue(recycle_id)

            idx = dms_server.read_index()
            tree_ids = dms_server._collect_node_ids(idx["tree"])
            self.assertNotIn("NODE-A", tree_ids)
            self.assertIn(recycle_id, tree_ids)

            recycle_node = dms_server._find_node(idx["tree"], recycle_id)
            self.assertEqual(recycle_node["name"], NOT_SHOW)
            self.assertIn({"id": "DOC-1"}, recycle_node["documents"])

            new_doc = next(d for d in idx["docIndex"] if d["id"] == "DOC-1")
            self.assertEqual(new_doc["originalNodeId"], recycle_id)

            matches = dms_server._find_doc_files(dms_server.get_docs_dir(), "DOC-1")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].read_bytes(), b"orig-bytes")

    def test_delete_node_with_nested_children_sweeps_all_descendant_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Parent", "documents": [{"id": "DOC-1"}], "children": [
                    {"id": "NODE-B", "name": "Child", "documents": [{"id": "DOC-2"}], "children": []},
                ]},
            ], "documents": []}
            doc_index = [
                {"id": "DOC-1", "name": "a.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-A"},
                {"id": "DOC-2", "name": "b.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-B"},
            ]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)
            self._write_doc_file(storage_root, "NODE-A", tree, "DOC-1", "a.pdf")
            self._write_doc_file(storage_root, "NODE-B", tree, "DOC-2", "b.pdf")

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-A")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["savedDocs"], 2)

            idx = dms_server.read_index()
            recycle_id = resp.get_json()["recycleBinId"]
            recycle_docs = {d["id"] for d in dms_server._find_node(idx["tree"], recycle_id)["documents"]}
            self.assertEqual(recycle_docs, {"DOC-1", "DOC-2"})
            tree_ids = dms_server._collect_node_ids(idx["tree"])
            self.assertNotIn("NODE-A", tree_ids)
            self.assertNotIn("NODE-B", tree_ids)

    def test_cannot_delete_root_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-ROOT")
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(dms_server.read_index()["tree"]["id"], "NODE-ROOT")

    def test_cannot_delete_not_show_folder_via_node_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-RECYCLE", "name": NOT_SHOW, "children": [], "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": [{"id": "DOC-1", "name": "x.pdf"}]})

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-RECYCLE")
            self.assertEqual(resp.status_code, 400)
            idx = dms_server.read_index()
            self.assertIn("NODE-RECYCLE", dms_server._collect_node_ids(idx["tree"]))

    def test_delete_empty_node_removes_folder_without_creating_recycle_bin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Empty Folder", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            dms_server._create_local_folder_structure(tree)
            node_folder = dms_server._get_node_docs_dir("NODE-A", tree)
            self.assertTrue(node_folder.exists())

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-A")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertEqual(body["savedDocs"], 0)
            self.assertIsNone(body["recycleBinId"])

            idx = dms_server.read_index()
            self.assertNotIn(NOT_SHOW, [c.get("name") for c in idx["tree"].get("children", [])])
            self.assertFalse(node_folder.exists())

    def test_delete_node_appends_to_existing_recycle_bin_without_losing_prior_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-RECYCLE", "name": NOT_SHOW, "children": [], "documents": [{"id": "DOC-OLD"}]},
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-NEW"}]},
            ], "documents": []}
            doc_index = [
                {"id": "DOC-OLD", "name": "old.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-RECYCLE"},
                {"id": "DOC-NEW", "name": "new.pdf", "mime": "application/pdf", "size": 1, "originalNodeId": "NODE-A"},
            ]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            dms_server._create_local_folder_structure(tree)
            self._write_doc_file(storage_root, "NODE-A", tree, "DOC-NEW", "new.pdf")

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-A")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["recycleBinId"], "NODE-RECYCLE")

            idx = dms_server.read_index()
            recycle_docs = {d["id"] for d in dms_server._find_node(idx["tree"], "NODE-RECYCLE")["documents"]}
            self.assertEqual(recycle_docs, {"DOC-OLD", "DOC-NEW"})

    def test_delete_node_with_untracked_files_on_disk_soft_deletes_instead_of_wiping(self):
        """Belt-and-suspenders guard: a folder can hold real files the tree/
        docIndex don't know about (see the comment in delete_node about a
        name-collision gap in the tree-merge). Must never fall through to
        the unconditional hard-delete (shutil.rmtree) in that case."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            dms_server._create_local_folder_structure(tree)
            node_folder = dms_server._get_node_docs_dir("NODE-A", tree)
            stray = node_folder / "mystery+DOC-STRAY.pdf"
            stray.write_bytes(b"untracked-but-real")

            client = dms_server.app.test_client()
            resp = client.delete("/api/nodes/NODE-A")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertIsNotNone(body["recycleBinId"])

            matches = list((storage_root / "docs").rglob("*mystery*"))
            self.assertEqual(len(matches), 1, "the untracked file must survive somewhere on disk")
            self.assertEqual(matches[0].read_bytes(), b"untracked-but-real")


class DeleteDocTests(unittest.TestCase):
    def test_delete_doc_removes_file_from_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            docs_dir = dms_server.get_docs_dir()
            path = docs_dir / dms_server._doc_filename("DOC-1", "a.pdf")
            path.write_bytes(b"data")

            client = dms_server.app.test_client()
            resp = client.delete("/api/docs/DOC-1")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["deleted"], [path.name])
            self.assertFalse(path.exists())

    def test_delete_missing_doc_returns_ok_with_empty_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.get_docs_dir()

            client = dms_server.app.test_client()
            resp = client.delete("/api/docs/DOC-NONEXISTENT")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["deleted"], [])

    def test_delete_doc_does_not_touch_index(self):
        """delete_doc is the low-level "remove these bytes from disk"
        primitive the client calls only for a document already in the
        recycle bin, after it has already updated docIndex/tree itself
        (see unlinkDocFromNode in dms.html) -- it must not also rewrite
        index.json, or it could race with / clobber that client-driven
        update."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": [{"id": "DOC-1"}]}
            doc_index = [{"id": "DOC-1", "name": "a.pdf"}]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            before_mtime = dms_server.get_index_path().stat().st_mtime_ns

            docs_dir = dms_server.get_docs_dir()
            (docs_dir / dms_server._doc_filename("DOC-1", "a.pdf")).write_bytes(b"data")

            client = dms_server.app.test_client()
            resp = client.delete("/api/docs/DOC-1")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            after_mtime = dms_server.get_index_path().stat().st_mtime_ns
            self.assertEqual(before_mtime, after_mtime)
            idx = dms_server.read_index()
            self.assertEqual(idx["docIndex"], [{"id": "DOC-1", "name": "a.pdf"}])


if __name__ == "__main__":
    unittest.main()

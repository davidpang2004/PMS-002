"""Tier 1 hardening coverage: PUT /api/tree's background disk-sync step
(_sync_folder_moves / _cleanup_stale_node_folders, run after every
put_tree() call -- see put_tree's _background_sync) actually moves real
files on disk whenever the user renames or reparents a folder in the UI.
This had no test coverage before this: test_password_marker_regression.py
only exercises put_tree() for password-marker resolution, deliberately
avoiding renames so it wouldn't race with that background thread.

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import tempfile
import threading
import unittest
from pathlib import Path

import dms_server


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


class PutTreeFolderSyncTests(unittest.TestCase):
    def _put_tree_and_wait(self, client, tree):
        # put_tree() fires a background thread (folder moves, local folder
        # structure, doc-file resync, stale-folder cleanup) after returning
        # its HTTP response. Wait for it so assertions right after aren't
        # racing a still-in-flight disk sync.
        before = set(threading.enumerate())
        resp = client.put("/api/tree", json={"tree": tree})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        for t in set(threading.enumerate()) - before:
            t.join(timeout=5)
        return resp

    def test_rename_moves_folder_and_preserves_file_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Old Name", "children": [], "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            self._put_tree_and_wait(client, tree)
            old_folder = dms_server._get_node_docs_dir("NODE-A", tree)
            doc_path = old_folder / dms_server._doc_filename("DOC-1", "a.pdf")
            doc_path.write_bytes(b"content-1")
            dms_server.write_index({
                "tree": tree,
                "docIndex": [{"id": "DOC-1", "name": "a.pdf", "originalNodeId": "NODE-A"}],
            })

            renamed = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "New Name", "children": [], "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            self._put_tree_and_wait(client, renamed)

            new_tree = dms_server.read_index()["tree"]
            new_folder = dms_server._get_node_docs_dir("NODE-A", new_tree)
            self.assertFalse(old_folder.exists())
            matches = list(new_folder.glob("*DOC-1*"))
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].read_bytes(), b"content-1")

    def test_reparent_moves_subtree_with_children_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-SRC", "name": "Source Parent", "children": [
                    {"id": "NODE-CHILD", "name": "Child", "children": [], "documents": [{"id": "DOC-2"}]},
                ], "documents": []},
                {"id": "NODE-DST", "name": "Destination Parent", "children": [], "documents": []},
            ], "documents": []}
            self._put_tree_and_wait(client, tree)
            child_folder = dms_server._get_node_docs_dir("NODE-CHILD", tree)
            (child_folder / dms_server._doc_filename("DOC-2", "b.pdf")).write_bytes(b"content-2")
            dms_server.write_index({
                "tree": tree,
                "docIndex": [{"id": "DOC-2", "name": "b.pdf", "originalNodeId": "NODE-CHILD"}],
            })

            # Drag "Source Parent" (with its child) under "Destination Parent".
            reparented = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-DST", "name": "Destination Parent", "children": [
                    {"id": "NODE-SRC", "name": "Source Parent", "children": [
                        {"id": "NODE-CHILD", "name": "Child", "children": [], "documents": [{"id": "DOC-2"}]},
                    ], "documents": []},
                ], "documents": []},
            ], "documents": []}
            self._put_tree_and_wait(client, reparented)

            new_tree = dms_server.read_index()["tree"]
            new_child_folder = dms_server._get_node_docs_dir("NODE-CHILD", new_tree)
            self.assertIn("Destination Parent", new_child_folder.parts)
            self.assertIn("Source Parent", new_child_folder.parts)
            matches = list(new_child_folder.glob("*DOC-2*"))
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].read_bytes(), b"content-2")

    def test_reparent_into_folder_with_existing_content_merges_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            # Two same-named top-level folders that will collide once "A"
            # moves under "Container" where "Existing" already sits at the
            # resulting path -- exercise the merge-not-clobber branch of
            # _sync_folder_moves rather than the simple rename/move branch.
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-CONTAINER", "name": "Container", "children": [
                    {"id": "NODE-EXISTING", "name": "Shared Name", "children": [], "documents": [{"id": "DOC-OLD"}]},
                ], "documents": []},
                {"id": "NODE-MOVING", "name": "Shared Name", "children": [], "documents": [{"id": "DOC-NEW"}]},
            ], "documents": []}
            self._put_tree_and_wait(client, tree)
            existing_folder = dms_server._get_node_docs_dir("NODE-EXISTING", tree)
            (existing_folder / dms_server._doc_filename("DOC-OLD", "old.pdf")).write_bytes(b"old-content")
            moving_folder = dms_server._get_node_docs_dir("NODE-MOVING", tree)
            (moving_folder / dms_server._doc_filename("DOC-NEW", "new.pdf")).write_bytes(b"new-content")
            dms_server.write_index({
                "tree": tree,
                "docIndex": [
                    {"id": "DOC-OLD", "name": "old.pdf", "originalNodeId": "NODE-EXISTING"},
                    {"id": "DOC-NEW", "name": "new.pdf", "originalNodeId": "NODE-MOVING"},
                ],
            })

            # This produces two tree nodes ("NODE-EXISTING" and "NODE-MOVING")
            # that resolve to the *same* on-disk path (Container/Shared Name)
            # -- an edge case, but one _sync_folder_moves explicitly handles
            # via its merge branch rather than silently clobbering.
            merged = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-CONTAINER", "name": "Container", "children": [
                    {"id": "NODE-EXISTING", "name": "Shared Name", "children": [], "documents": [{"id": "DOC-OLD"}]},
                    {"id": "NODE-MOVING", "name": "Shared Name", "children": [], "documents": [{"id": "DOC-NEW"}]},
                ], "documents": []},
            ], "documents": []}
            self._put_tree_and_wait(client, merged)

            docs_dir = dms_server.get_docs_dir()
            old_matches = list(docs_dir.rglob("*DOC-OLD*"))
            new_matches = list(docs_dir.rglob("*DOC-NEW*"))
            self.assertEqual(len(old_matches), 1)
            self.assertEqual(len(new_matches), 1)
            self.assertEqual(old_matches[0].read_bytes(), b"old-content")
            self.assertEqual(new_matches[0].read_bytes(), b"new-content")

    def test_node_dropped_from_raw_tree_edit_keeps_its_files_on_disk(self):
        """A plain PUT /api/tree that simply omits a node (as opposed to the
        dedicated DELETE /api/nodes/<id> route, which soft-deletes to a
        recycle bin) must never destroy real files. What actually happens:
        _sync_doc_files_to_tree_paths resolves the orphaned doc's node id to
        no path and relocates its file to the docs/ root (see
        _get_node_docs_dir falling back to bare docs_dir when the node isn't
        found), and only then does _cleanup_stale_node_folders remove the
        now-empty old folder -- the file survives, just relocated, not
        deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            self._put_tree_and_wait(client, tree)
            folder = dms_server._get_node_docs_dir("NODE-A", tree)
            doc_path = folder / dms_server._doc_filename("DOC-1", "a.pdf")
            doc_path.write_bytes(b"irreplaceable")
            dms_server.write_index({
                "tree": tree,
                "docIndex": [{"id": "DOC-1", "name": "a.pdf", "originalNodeId": "NODE-A"}],
            })

            dropped = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            self._put_tree_and_wait(client, dropped)

            self.assertFalse(folder.exists(), "the now-empty old folder should be cleaned up")
            survivors = list(dms_server.get_docs_dir().rglob("*DOC-1*"))
            self.assertEqual(len(survivors), 1, "the file itself must survive somewhere on disk")
            self.assertEqual(survivors[0].read_bytes(), b"irreplaceable")


if __name__ == "__main__":
    unittest.main()

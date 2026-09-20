"""Tier 2 hardening coverage: POST /api/hierarchy/create builds/merges a
node tree from a CSV hierarchy definition (name,parent,description per
row). The data-safety property that matters most here -- a new hierarchy
whose root name doesn't match the existing tree's root preserves the old
tree as a child rather than discarding it -- had no test coverage before
this, despite the route's own docstring/comment calling it out explicitly.

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


def _names(node):
    return {node.get("name")} | {n for c in (node.get("children") or []) for n in _names(c)}


class HierarchyCreateTests(unittest.TestCase):
    def test_creates_new_hierarchy_from_scratch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": None, "docIndex": []})
            client = dms_server.app.test_client()

            text = "BOP001,,Root assembly\nSA-100,BOP001,Sub-assembly\nPT-001,SA-100,Part"
            resp = client.post("/api/hierarchy/create", json={"text": text})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            body = resp.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["created"]), 3)
            self.assertEqual(body["skipped"], [])

            idx = dms_server.read_index()
            self.assertEqual(idx["tree"]["name"], "BOP001")
            self.assertEqual(_names(idx["tree"]), {"BOP001", "SA-100", "PT-001"})

    def test_reimporting_the_same_hierarchy_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": None, "docIndex": []})
            client = dms_server.app.test_client()
            text = "BOP001,,Root\nSA-100,BOP001,Sub"

            first = client.post("/api/hierarchy/create", json={"text": text})
            self.assertEqual(first.get_json()["created"], ["BOP001", "SA-100"])

            second = client.post("/api/hierarchy/create", json={"text": text})
            self.assertEqual(second.status_code, 200)
            body = second.get_json()
            self.assertEqual(body["created"], [])
            self.assertEqual(set(body["skipped"]), {"BOP001", "SA-100"})

            idx = dms_server.read_index()
            # No duplicate nodes created by the second, idempotent run.
            self.assertEqual(len(idx["tree"].get("children", [])), 1)

    def test_new_root_name_preserves_old_tree_as_a_child(self):
        """The whole-tree-root-replace path must never silently drop the
        previous project's data -- see the route's own "Preserve old tree
        as a child so no data is lost" comment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            old_tree = {"id": "NODE-OLD", "name": "Old Project", "children": [
                {"id": "NODE-PRECIOUS", "name": "Precious Folder", "children": [], "documents": [{"id": "DOC-1"}]},
            ], "documents": []}
            dms_server.write_index({"tree": old_tree, "docIndex": [{"id": "DOC-1", "name": "a.pdf"}]})
            client = dms_server.app.test_client()

            resp = client.post("/api/hierarchy/create", json={"text": "NewRoot,,A different hierarchy"})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            idx = dms_server.read_index()
            self.assertEqual(idx["tree"]["name"], "NewRoot")
            all_names = _names(idx["tree"])
            self.assertIn("Old Project", all_names)
            self.assertIn("Precious Folder", all_names)
            # The DOC-1 in docIndex is untouched -- only the tree changed.
            self.assertEqual(idx["docIndex"], [{"id": "DOC-1", "name": "a.pdf"}])

    def test_target_node_id_attaches_under_existing_folder_without_touching_rest_of_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [
                {"id": "NODE-OTHER", "name": "Unrelated Sibling", "children": [], "documents": []},
                {"id": "NODE-TARGET", "name": "Import Here", "children": [], "documents": []},
            ], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/hierarchy/create", json={
                "text": "SA-200,,New sub-hierarchy",
                "target_node_id": "NODE-TARGET",
            })
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            idx = dms_server.read_index()
            # Root wasn't replaced -- unlike the no-target-node case above.
            self.assertEqual(idx["tree"]["id"], "NODE-ROOT")
            unrelated = dms_server._find_node(idx["tree"], "NODE-OTHER")
            self.assertIsNotNone(unrelated)
            target = dms_server._find_node(idx["tree"], "NODE-TARGET")
            self.assertEqual([c["name"] for c in target["children"]], ["SA-200"])

    def test_invalid_target_node_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            tree = {"id": "NODE-ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/hierarchy/create", json={
                "text": "SA-200,,x", "target_node_id": "NODE-DOES-NOT-EXIST",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertFalse(body["ok"])
            self.assertIn("Target folder", body["errors"][0])
            # Tree must be untouched on this validation failure.
            self.assertEqual(dms_server.read_index()["tree"], tree)

    def test_duplicate_name_without_dedupe_is_a_validation_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            dms_server.write_index({"tree": None, "docIndex": []})
            client = dms_server.app.test_client()

            resp = client.post("/api/hierarchy/create", json={
                "text": "Root,,\nDupe,Root,\nDupe,Root,",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertFalse(body["ok"])
            self.assertTrue(body["errors"])
            # Nothing should have been written on a validation failure.
            self.assertIsNone(dms_server.read_index()["tree"])


if __name__ == "__main__":
    unittest.main()

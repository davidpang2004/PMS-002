"""Tier 1 hardening coverage: the project backup/restore round trip
(POST /api/project/export-full, /export-selected, /import) had zero test
coverage before this, despite being the actual disaster-recovery path a
user reaches for after data loss -- if this silently drops or corrupts
something, you only find out when you need the backup for real.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import dms_server


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


def _build_project(storage_root: Path):
    tree = {"id": "NODE-ROOT", "name": "Family Archive", "children": [
        {"id": "NODE-A", "name": "Folder A", "children": [], "documents": [{"id": "DOC-1"}]},
        {"id": "NODE-B", "name": "Folder B", "children": [], "documents": [{"id": "DOC-2"}]},
    ], "documents": []}
    doc_index = [
        {"id": "DOC-1", "name": "a.pdf", "mime": "application/pdf", "size": 9, "originalNodeId": "NODE-A"},
        {"id": "DOC-2", "name": "b.jpg", "mime": "image/jpeg", "size": 9, "originalNodeId": "NODE-B"},
    ]
    dms_server.write_index({"tree": tree, "docIndex": doc_index})
    dms_server._create_local_folder_structure(tree)
    folder_a = dms_server._get_node_docs_dir("NODE-A", tree)
    folder_b = dms_server._get_node_docs_dir("NODE-B", tree)
    (folder_a / dms_server._doc_filename("DOC-1", "a.pdf")).write_bytes(b"content-a")
    (folder_b / dms_server._doc_filename("DOC-2", "b.jpg")).write_bytes(b"content-b")
    return tree, doc_index


class ExportImportRoundTripTests(unittest.TestCase):
    def test_export_full_then_import_round_trips_tree_docindex_and_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            _build_project(storage_root)
            client = dms_server.app.test_client()

            resp = client.post("/api/project/export-full")
            self.assertEqual(resp.status_code, 200)  # binary zip body, no text message
            zip_bytes = resp.data

            target = tmp_path / "restored"
            resp = client.post("/api/project/import", data={
                "file": (io.BytesIO(zip_bytes), "backup.dms"),
                "target_path": str(target),
                "mode": "new",
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

            restored_idx = json.loads((target / "index.json").read_text())
            self.assertEqual(restored_idx["tree"]["name"], "Family Archive")
            self.assertEqual(
                {d["id"] for d in restored_idx["docIndex"]},
                {"DOC-1", "DOC-2"},
            )
            a_matches = list((target / "docs").rglob("*DOC-1*"))
            b_matches = list((target / "docs").rglob("*DOC-2*"))
            self.assertEqual(len(a_matches), 1)
            self.assertEqual(len(b_matches), 1)
            self.assertEqual(a_matches[0].read_bytes(), b"content-a")
            self.assertEqual(b_matches[0].read_bytes(), b"content-b")

            # import_project() switches the running server to the restored
            # folder -- confirm that actually took effect.
            self.assertEqual(dms_server.get_storage_root(), target.resolve())

    def test_export_selected_only_includes_chosen_subtree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            _build_project(storage_root)
            client = dms_server.app.test_client()

            resp = client.post("/api/project/export-selected", json={"node_ids": ["NODE-A"]})
            self.assertEqual(resp.status_code, 200)  # binary zip body, no text message

            with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
                filtered_idx = json.loads(zf.read("index.json"))
                names = zf.namelist()

            doc_ids = {d["id"] for d in filtered_idx["docIndex"]}
            self.assertEqual(doc_ids, {"DOC-1"})
            self.assertTrue(any("DOC-1" in n for n in names))
            self.assertFalse(any("DOC-2" in n for n in names))
            # The full tree structure is kept (just the doc list is
            # filtered) so the folder layout isn't lost on a partial export.
            self.assertEqual(filtered_idx["tree"]["name"], "Family Archive")

    def test_import_rejects_zip_slip_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            client = dms_server.app.test_client()

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"format": "dms-project", "version": 1}))
                zf.writestr("../../evil.txt", "pwned")
            buf.seek(0)

            target = tmp_path / "victim"
            resp = client.post("/api/project/import", data={
                "file": (buf, "malicious.dms"),
                "target_path": str(target),
            }, content_type="multipart/form-data")

            self.assertEqual(resp.status_code, 400)
            self.assertFalse((tmp_path / "evil.txt").exists())

    def test_import_rejects_file_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            _isolate(tmp_path)
            client = dms_server.app.test_client()

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("index.json", "{}")
            buf.seek(0)

            resp = client.post("/api/project/import", data={
                "file": (buf, "not-a-project.dms"),
                "target_path": str(tmp_path / "target"),
            }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

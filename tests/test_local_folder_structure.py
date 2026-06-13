import io
import tempfile
import unittest
from pathlib import Path

import dms_server


class LocalFolderStructureTests(unittest.TestCase):
    def test_create_local_folder_structure_creates_matching_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = tmp_path / "storage"
            config_path = tmp_path / "config.json"

            dms_server.CONFIG_PATH = config_path
            dms_server._storage_path_override = str(storage_root)

            dms_server.save_config({"storage_path": str(storage_root)})
            expected_root = storage_root.resolve()

            tree = {
                "id": "NODE-ROOT",
                "name": "Root Folder",
                "children": [
                    {"id": "NODE-CHILD", "name": "Child Folder", "children": [], "documents": []}
                ],
                "documents": [],
            }

            result = dms_server._create_local_folder_structure(tree)

            self.assertEqual(result, expected_root / "docs")
            self.assertTrue((expected_root / "docs" / "Root Folder").exists())
            self.assertTrue((expected_root / "docs" / "Child Folder").exists())
            self.assertFalse((expected_root / "docs" / "Root Folder" / "Child Folder").exists())

    def test_post_doc_uses_node_folder_instead_of_legacy_myphoto_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = tmp_path / "storage"
            config_path = tmp_path / "config.json"

            dms_server.CONFIG_PATH = config_path
            dms_server._storage_path_override = str(storage_root)
            dms_server.save_config({"storage_path": str(storage_root)})

            tree = {
                "id": "NODE-ROOT",
                "name": "New Project",
                "children": [],
                "documents": [],
            }
            dms_server.write_index({"tree": tree, "docIndex": []})
            dms_server._create_local_folder_structure(tree)

            client = dms_server.app.test_client()
            payload = io.BytesIO(b"hello")
            response = client.post(
                "/api/docs",
                data={
                    "file": (payload, "photo.jpg"),
                    "doc_id": "DOC-TEST-001",
                    "node_id": "NODE-ROOT",
                    "photo_year": "2024",
                    "photo_month": "05",
                },
                content_type="multipart/form-data",
            )

            self.assertEqual(response.status_code, 200)
            self.assertFalse((storage_root / "docs" / "MyPhoto").exists())
            self.assertTrue((storage_root / "docs" / "New Project" / "DOC-TEST-001__photo.jpg").exists())


if __name__ == "__main__":
    unittest.main()

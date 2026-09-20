"""On-disk document filename layout: new files are named
"<name>+<DOC-ID>.<ext>" (DOC-ID as a suffix); files saved before this
layout existed are named "<DOC-ID>__<name>.<ext>" (DOC-ID as a prefix) and
are never renamed to match. Every doc-id <-> file lookup/derivation must
therefore work under both layouts -- these tests pin that down directly,
since it's easy for a single missed call site to silently drop support for
one of the two layouts.

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import tempfile
import unittest
from pathlib import Path

import dms_server


def _isolate(tmp_path: Path):
    storage_root = tmp_path / "storage"
    config_path = tmp_path / "config.json"
    dms_server.CONFIG_PATH = config_path
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


class DocFilenameTests(unittest.TestCase):
    def test_doc_filename_is_name_then_id_suffix(self):
        self.assertEqual(
            dms_server._doc_filename("DOC-20260917-A414CDB6", "photo.jpg"),
            "photo+DOC-20260917-A414CDB6.jpg",
        )

    def test_doc_filename_handles_display_name_with_no_extension(self):
        self.assertEqual(
            dms_server._doc_filename("DOC-X", "notes"),
            "notes+DOC-X",
        )


class DocIdFromFilenameTests(unittest.TestCase):
    def test_recovers_id_from_new_suffix_layout(self):
        self.assertEqual(dms_server._doc_id_from_filename("photo+DOC-X.jpg"), "DOC-X")

    def test_recovers_id_from_old_prefix_layout(self):
        self.assertEqual(dms_server._doc_id_from_filename("DOC-X__photo.jpg"), "DOC-X")

    def test_display_name_containing_plus_still_resolves_by_last_separator(self):
        # The DOC-ID itself never contains "+", so the *last* "+" in the
        # stem is always the real separator even if the original display
        # name happened to contain one.
        self.assertEqual(
            dms_server._doc_id_from_filename("A+B+DOC-X.jpg"), "DOC-X",
        )


class FindDocFilesTests(unittest.TestCase):
    def test_finds_file_under_either_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            docs_dir = tmp_path / "docs"
            docs_dir.mkdir()
            (docs_dir / "photo+DOC-NEW.jpg").write_bytes(b"x")
            (docs_dir / "DOC-OLD__scan.pdf").write_bytes(b"y")

            self.assertEqual(
                dms_server._find_doc_files(docs_dir, "DOC-NEW")[0].name,
                "photo+DOC-NEW.jpg",
            )
            self.assertEqual(
                dms_server._find_doc_files(docs_dir, "DOC-OLD")[0].name,
                "DOC-OLD__scan.pdf",
            )


class SyncDocFilesToTreePathsTests(unittest.TestCase):
    def test_moves_new_and_old_layout_files_to_renamed_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            storage_root = _isolate(tmp_path)
            tree = {"id": "NODE-ROOT", "name": "Renamed Folder", "children": [], "documents": [
                {"id": "DOC-NEW"}, {"id": "DOC-OLD"},
            ]}
            doc_index = [
                {"id": "DOC-NEW", "name": "photo.jpg", "originalNodeId": "NODE-ROOT"},
                {"id": "DOC-OLD", "name": "scan.pdf", "originalNodeId": "NODE-ROOT"},
            ]
            dms_server.write_index({"tree": tree, "docIndex": doc_index})
            docs_dir = storage_root / "docs"
            old_folder = docs_dir / "Old Folder Name"
            old_folder.mkdir(parents=True)
            # DOC-NEW sits under the *current* on-disk layout (suffix);
            # DOC-OLD is a leftover from before that layout existed (prefix).
            (old_folder / "photo+DOC-NEW.jpg").write_bytes(b"x")
            (old_folder / "DOC-OLD__scan.pdf").write_bytes(b"y")

            dms_server._sync_doc_files_to_tree_paths(tree)

            new_folder = docs_dir / "Renamed Folder"
            self.assertTrue((new_folder / "photo+DOC-NEW.jpg").exists())
            self.assertTrue((new_folder / "DOC-OLD__scan.pdf").exists())
            self.assertFalse((old_folder / "photo+DOC-NEW.jpg").exists())
            self.assertFalse((old_folder / "DOC-OLD__scan.pdf").exists())


if __name__ == "__main__":
    unittest.main()

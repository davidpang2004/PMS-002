"""write_index() snapshots the prior index.json into .snapshots/ before each
overwrite, throttled and pruned -- see _snapshot_index_before_write in
dms_server.py. This is the recovery path for a *bad* write (a bug, or an
accidental bulk delete) that the atomic temp-file+replace doesn't cover.

All tests isolate CONFIG_PATH + the storage path so nothing touches the
real ~/.pms_dms_config.json or a live storage folder.
"""
import tempfile
import unittest
from pathlib import Path

import dms_server


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    config_path = tmp_path / "config.json"
    dms_server.CONFIG_PATH = config_path
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


class IndexSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._orig_interval = dms_server._SNAPSHOT_INTERVAL_SECONDS
        self._orig_retention = dms_server._SNAPSHOT_RETENTION

    def tearDown(self):
        dms_server._SNAPSHOT_INTERVAL_SECONDS = self._orig_interval
        dms_server._SNAPSHOT_RETENTION = self._orig_retention

    def test_first_write_creates_no_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": None, "docIndex": []})
            self.assertFalse((storage_root / ".snapshots").exists())

    def test_second_write_snapshots_the_prior_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server._SNAPSHOT_INTERVAL_SECONDS = 0

            dms_server.write_index({"tree": None, "docIndex": [{"id": "DOC-1"}]})
            dms_server.write_index({"tree": None, "docIndex": []})

            snaps = sorted((storage_root / ".snapshots").glob("index-*.json"))
            self.assertEqual(len(snaps), 1)
            self.assertIn('"DOC-1"', snaps[0].read_text())

    def test_rapid_writes_are_throttled_to_one_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server._SNAPSHOT_INTERVAL_SECONDS = 600

            for i in range(5):
                dms_server.write_index({"tree": None, "docIndex": [{"id": f"DOC-{i}"}]})

            snaps = list((storage_root / ".snapshots").glob("index-*.json"))
            self.assertEqual(len(snaps), 1)

    def test_old_snapshots_are_pruned_to_retention_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server._SNAPSHOT_INTERVAL_SECONDS = 0
            dms_server._SNAPSHOT_RETENTION = 3

            snap_dir = storage_root / ".snapshots"
            dms_server.write_index({"tree": None, "docIndex": []})
            for i in range(5):
                dms_server.write_index({"tree": None, "docIndex": [{"id": f"DOC-{i}"}]})

            snaps = sorted(snap_dir.glob("index-*.json"))
            self.assertEqual(len(snaps), 3)


if __name__ == "__main__":
    unittest.main()

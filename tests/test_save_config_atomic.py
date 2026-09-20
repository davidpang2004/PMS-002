"""save_config() writes ~/.pms_dms_config.json the same way write_index()
writes index.json -- temp file + os.replace() -- rather than a plain
write_text() that could interleave under concurrent writers and corrupt
the file. See tests/test_index_write_race.py for the same property on
write_index(); this is the equivalent coverage for save_config().

Isolates CONFIG_PATH so nothing touches the real ~/.pms_dms_config.json.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import dms_server


class SaveConfigAtomicTests(unittest.TestCase):
    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dms_server.CONFIG_PATH = Path(tmpdir) / "config.json"
            dms_server.save_config({**dms_server.DEFAULT_CONFIG, "storage_path": "/some/path"})
            self.assertEqual(dms_server.load_config()["storage_path"], "/some/path")

    def test_no_leftover_temp_file_after_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dms_server.CONFIG_PATH = Path(tmpdir) / "config.json"
            dms_server.save_config(dict(dms_server.DEFAULT_CONFIG))
            leftovers = list(Path(tmpdir).glob("config.json.tmp-*"))
            self.assertEqual(leftovers, [])

    def test_concurrent_writes_never_corrupt_config_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dms_server.CONFIG_PATH = Path(tmpdir) / "config.json"

            # Sized large enough (tens of KB) that a torn write has room to
            # interleave -- see test_index_write_race.py for why small
            # payloads rarely reproduce the old bug.
            payloads = [
                {**dms_server.DEFAULT_CONFIG, "storage_path": f"/path-{i}", "note": "x" * (i * 20000)}
                for i in range(10)
            ]
            barrier = threading.Barrier(len(payloads))

            def _write(payload):
                barrier.wait()
                dms_server.save_config(payload)

            threads = [threading.Thread(target=_write, args=(p,)) for p in payloads]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            raw = dms_server.CONFIG_PATH.read_text()
            parsed = json.loads(raw)  # raises if torn/truncated
            self.assertIn(parsed, payloads)


if __name__ == "__main__":
    unittest.main()

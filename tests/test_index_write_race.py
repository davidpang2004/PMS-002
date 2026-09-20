"""Regression coverage for the index.json write race: write_index() used
to be a plain path.write_text(), which truncates-then-writes in place --
not a single atomic syscall. Two overlapping writers (e.g. two autosaves
from rapid keystrokes firing close together) could interleave, corrupting
the file with a torn mix of both writes. write_index() now writes to a
temp file and os.replace()s it into place instead (see its docstring), so
whichever write's rename lands last wins outright, in full.

This drives many overlapping writes at once and asserts index.json always
ends up as valid, complete JSON matching exactly one of the writes -- never
a torn/interleaved mix.

Isolates CONFIG_PATH + the storage path so nothing touches the real
~/.pms_dms_config.json or a live storage folder.
"""
import json
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


class IndexWriteRaceTests(unittest.TestCase):
    def test_concurrent_writes_never_corrupt_index_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server._SNAPSHOT_INTERVAL_SECONDS = 999999  # not what's under test here

            # Deliberately different-length payloads, sized large enough
            # (tens of KB) that a torn write actually has room to
            # interleave -- verified against the old plain write_text()
            # implementation, which corrupted ~50% of rounds at this size;
            # payloads a few hundred bytes long rarely triggered it, since
            # write() calls that small tend to complete within one
            # scheduler quantum.
            payloads = [
                {"tree": None, "docIndex": [{"id": f"DOC-{i}", "note": "x" * (i * 20000)}]}
                for i in range(10)
            ]

            barrier = threading.Barrier(len(payloads))

            def _write(payload):
                barrier.wait()
                dms_server.write_index(payload)

            threads = [threading.Thread(target=_write, args=(p,)) for p in payloads]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            index_path = storage_root / "index.json"
            raw = index_path.read_text()
            parsed = json.loads(raw)  # raises if torn/truncated

            self.assertIn(parsed, payloads)
            self.assertFalse(list(storage_root.glob("index.json.tmp-*")),
                              "leftover temp file after a write")

    def test_concurrent_writes_stress_no_corruption_across_many_rounds(self):
        """Same property, repeated, to make a race far less likely to hide
        behind a lucky thread-scheduling order on a single run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
            dms_server._SNAPSHOT_INTERVAL_SECONDS = 999999

            for round_num in range(8):
                payloads = [
                    {"tree": None, "docIndex": [{"id": f"R{round_num}-{i}", "note": "y" * (i * 20000)}]}
                    for i in range(8)
                ]
                barrier = threading.Barrier(len(payloads))

                def _write(payload):
                    barrier.wait()
                    dms_server.write_index(payload)

                threads = [threading.Thread(target=_write, args=(p,)) for p in payloads]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                parsed = json.loads((storage_root / "index.json").read_text())
                self.assertIn(parsed, payloads)


if __name__ == "__main__":
    unittest.main()

"""/api/quit must record this instance's own project into the shared
config's storage_path ("last closed wins") before exiting -- otherwise
"Continue with previous project" on the next plain launch keeps reopening
whatever was configured before, even after closing a project opened via
"Open another project in a new window" (see dms_launcher.py's
LauncherWindow._quit(), which this mirrors).

Tests call _record_last_project_on_quit() directly rather than hitting the
route or quit_app() itself -- the real route spawns a thread that calls
os._exit(0), which would kill the test runner.

All tests isolate CONFIG_PATH (never touching the real
~/.pms_dms_config.json) -- see feedback_dms_config_isolation memory.
"""
import tempfile
import unittest
from pathlib import Path

import dms_server


class RecordLastProjectOnQuitTests(unittest.TestCase):
    def test_pinned_instance_overwrites_stale_shared_config_path(self):
        # Simulates: project A is the shared config's storage_path (set by
        # an earlier launch), but *this* instance was opened via "Open
        # another project in a new window" and is pinned to project B
        # (_storage_path_override). Quitting this instance should leave the
        # shared config pointing at B, not A -- "last closed wins".
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            project_a = tmp_path / "project_a"
            project_b = tmp_path / "project_b"
            project_a.mkdir()
            project_b.mkdir()

            dms_server.CONFIG_PATH = tmp_path / "config.json"
            dms_server.save_config({"storage_path": str(project_a)})
            dms_server._storage_path_override = str(project_b)
            try:
                dms_server._record_last_project_on_quit()
                cfg = dms_server.load_config()
                self.assertEqual(cfg["storage_path"], str(project_b.resolve()))
            finally:
                dms_server._storage_path_override = ""

    def test_unpinned_instance_keeps_shared_config_in_sync(self):
        # No CLI/spawned-window override (e.g. a plain single-instance
        # launch) -- get_storage_root() already falls back to the config's
        # own value, so this is a same-value rewrite, not a behavior change.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            project = tmp_path / "only_project"
            project.mkdir()

            dms_server.CONFIG_PATH = tmp_path / "config.json"
            dms_server.save_config({"storage_path": str(project)})
            dms_server._storage_path_override = ""

            dms_server._record_last_project_on_quit()
            cfg = dms_server.load_config()
            self.assertEqual(cfg["storage_path"], str(project.resolve()))

    def test_no_crash_when_no_project_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            dms_server.CONFIG_PATH = tmp_path / "config.json"
            dms_server._storage_path_override = ""
            # No config file at all yet -- get_storage_root() returns None,
            # and the helper must just no-op rather than raise.
            dms_server._record_last_project_on_quit()
            self.assertFalse(dms_server.CONFIG_PATH.exists())


if __name__ == "__main__":
    unittest.main()

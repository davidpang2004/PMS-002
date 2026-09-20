"""Regression coverage for the saved-password marker bug: PUT /api/tree
replaces the whole stored tree with whatever the client sends, and the
client never holds a real passwordEnc in memory once a password has been
saved -- it keeps only the SAVED_PASSWORD_MARKER ("•") as a presence
marker (see dms_server.py's comment above SAVED_PASSWORD_MARKER). Every
subsequent autosave on that node (an unrelated rename, a description edit,
...) sends that marker back in passwordEnc. put_tree must resolve the
marker back to the real ciphertext already on disk -- see
_resolve_password_markers -- or the saved password is destroyed the next
time anything else on that node is edited.

All tests isolate CONFIG_PATH, the storage path, and SECRET_KEY_PATH so
nothing touches the real ~/.pms_dms_config.json, ~/.pms_dms_secret.key, or
a live storage folder.
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
    dms_server.SECRET_KEY_PATH = tmp_path / "secret.key"
    return storage_root


class PasswordMarkerRegressionTests(unittest.TestCase):
    def _put_tree(self, client, tree):
        # put_tree() fires a background disk-sync thread (folder moves,
        # local folder structure, ...) that can still be mid-write when the
        # test's tmpdir gets torn down. Wait for it so each test stays
        # hermetic; unrelated to what these tests actually cover.
        before = set(threading.enumerate())
        resp = client.put("/api/tree", json={"tree": tree})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        for t in set(threading.enumerate()) - before:
            t.join(timeout=5)

    def _reveal(self, client, node_id, login_id=""):
        resp = client.post(f"/api/nodes/{node_id}/reveal-credential",
                            json={"login_id": login_id})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def test_legacy_node_password_survives_unrelated_autosave(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Router", "children": [], "documents": [],
                     "websiteUrl": "https://router.local", "accountName": "admin",
                     "password": "hunter2"}
            self._put_tree(client, tree)
            self.assertEqual(self._reveal(client, "NODE-ROOT")["password"], "hunter2")

            # Client scrubs the real value from memory after saving and
            # only ever autosaves the marker back -- simulate an unrelated
            # edit (description) in the same session. Deliberately not a
            # rename: that would trigger put_tree's background folder-move
            # sync thread, which is unrelated to what this test covers and
            # can still be mid-flight when the test's tmpdir is torn down.
            saved_tree = dms_server.read_index()["tree"]
            self.assertNotEqual(saved_tree["passwordEnc"], dms_server.SAVED_PASSWORD_MARKER)
            autosave_tree = {"id": "NODE-ROOT", "name": "Router", "children": [],
                              "documents": [], "description": "moved to the closet",
                              "websiteUrl": "https://router.local", "accountName": "admin",
                              "passwordEnc": dms_server.SAVED_PASSWORD_MARKER}
            self._put_tree(client, autosave_tree)

            self.assertEqual(self._reveal(client, "NODE-ROOT")["password"], "hunter2")
            self.assertEqual(dms_server.read_index()["tree"]["description"], "moved to the closet")

    def test_login_entry_password_survives_unrelated_autosave(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Accounts", "children": [], "documents": [],
                    "logins": [{"id": "LOGIN-1", "websiteUrl": "https://bank.example",
                                "accountName": "david", "password": "s3cr3t!"}]}
            self._put_tree(client, tree)
            self.assertEqual(self._reveal(client, "NODE-ROOT", "LOGIN-1")["password"], "s3cr3t!")

            autosave_tree = {"id": "NODE-ROOT", "name": "Accounts", "children": [],
                              "documents": [], "description": "updated note",
                              "logins": [{"id": "LOGIN-1", "websiteUrl": "https://bank.example",
                                          "accountName": "david",
                                          "passwordEnc": dms_server.SAVED_PASSWORD_MARKER}]}
            self._put_tree(client, autosave_tree)

            self.assertEqual(self._reveal(client, "NODE-ROOT", "LOGIN-1")["password"], "s3cr3t!")

    def test_explicit_clear_still_clears_a_saved_password(self):
        """Sending password="" is a deliberate clear, distinct from the
        marker -- must not be treated as "leave it alone"."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Router", "children": [], "documents": [],
                    "password": "hunter2"}
            self._put_tree(client, tree)
            self.assertEqual(self._reveal(client, "NODE-ROOT")["password"], "hunter2")

            cleared_tree = {"id": "NODE-ROOT", "name": "Router", "children": [], "documents": [],
                             "password": ""}
            self._put_tree(client, cleared_tree)
            self.assertEqual(self._reveal(client, "NODE-ROOT")["password"], "")

    def test_marker_with_no_prior_ciphertext_resolves_to_empty_not_literal_marker(self):
        """A marker with nothing to resolve against (e.g. a node id that
        never had a real saved password) must not be written to disk as if
        it were real ciphertext -- that would make the next reveal attempt
        crash trying to Fernet-decrypt the literal "•" character."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()

            tree = {"id": "NODE-ROOT", "name": "Fresh", "children": [], "documents": [],
                    "passwordEnc": dms_server.SAVED_PASSWORD_MARKER}
            self._put_tree(client, tree)

            saved = dms_server.read_index()["tree"]
            self.assertNotEqual(saved.get("passwordEnc"), dms_server.SAVED_PASSWORD_MARKER)
            self.assertEqual(self._reveal(client, "NODE-ROOT")["password"], "")


if __name__ == "__main__":
    unittest.main()

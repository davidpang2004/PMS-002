"""Regression coverage for a real bug found while writing Tier 2 tests for
combine_to_folder: _find_doc_files() used to resolve a doc_id to its
on-disk file via `docs_dir.rglob(f"*{doc_id}*")` -- a plain substring
search, not an exact-token match. If one document's id happened to be a
literal substring of another document's on-disk filename, the wrong file
was returned. In combine_to_folder this was observed to grab a freshly
merged PDF instead of the intended source document and soft-delete the
wrong file into the recycle bin.

With the app's own real id formats -- client genDocId() in dms.html
("DOC-" + yyyymmdd + "-" + 6-char base36) and the server's own "DOC-" +
yyyymmdd + "-" + token_hex(4) -- two ids from the SAME generator are
always the same fixed length, so a substring match between two of them
used to reduce to exact equality (never a real collision). The risk was a
same-day CLIENT id (19 chars) being a coincidental substring of a
same-day SERVER-generated id (21 chars, as combine-to-folder/convert-to-
pdf/plot-snapshot/etc. all produce) or vice versa -- both share the
"DOC-YYYYMMDD-" prefix, so it only took a 6-character coincidental match
at the right offset, not a full-id coincidence. Low probability with
today's id formats, but the match wasn't actually bounded by anything, so
this was a latent correctness bug, not a hypothetical one.

Fixed 2026-09-20: _find_doc_files now requires _doc_id_from_filename(f.name)
== doc_id exactly (still narrowing with the cheap substring glob first,
since most files in a large library contain no candidate at all). This
test locks that fix in place -- it reproduces the collision directly
against _find_doc_files rather than relying on getting lucky/unlucky with
random ids in a route-level test.

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


class FindDocFilesIdCollisionTests(unittest.TestCase):
    def test_short_id_that_is_a_prefix_of_a_longer_id_resolves_only_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            docs_dir = dms_server.get_docs_dir()

            # A same-day client-style id ("DOC-2" is a stand-in for a full
            # "DOC-20260920-XXXXXX" -- shortened here only to make the
            # collision explicit and deterministic) and a same-day
            # server-generated id that happens to start with the same text.
            short_id = "DOC-2"
            colliding_long_id = "DOC-20260920-B34D7C88"
            self.assertTrue(colliding_long_id.startswith(short_id))

            wanted = docs_dir / dms_server._doc_filename(short_id, "wanted.pdf")
            wanted.write_bytes(b"the file this id actually names")
            unrelated = docs_dir / dms_server._doc_filename(colliding_long_id, "Merged Report.pdf")
            unrelated.write_bytes(b"a completely different document")

            matches = dms_server._find_doc_files(docs_dir, short_id)
            self.assertEqual(matches, [wanted])

            other_matches = dms_server._find_doc_files(docs_dir, colliding_long_id)
            self.assertEqual(other_matches, [unrelated])

    def test_legacy_prefix_layout_id_collision_also_resolves_correctly(self):
        """Same property under the older "DOC-ID__name.ext" on-disk layout
        (pre-dating the name+DOC-ID suffix format -- see _doc_filename)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            docs_dir = dms_server.get_docs_dir()

            short_id = "DOC-2"
            colliding_long_id = "DOC-20260920-B34D7C88"
            wanted = docs_dir / f"{short_id}__wanted.pdf"
            wanted.write_bytes(b"legacy-format file this id actually names")
            unrelated = docs_dir / f"{colliding_long_id}__unrelated.pdf"
            unrelated.write_bytes(b"a completely different legacy-format document")

            matches = dms_server._find_doc_files(docs_dir, short_id)
            self.assertEqual(matches, [wanted])


if __name__ == "__main__":
    unittest.main()

"""Documents a real bug found while writing Tier 2 tests for
combine_to_folder: _find_doc_files() (dms_server.py) resolves a doc_id to
its on-disk file via `docs_dir.rglob(f"*{doc_id}*")` -- a plain substring
search, not an exact-token match. If one document's id happens to be a
literal substring of another document's on-disk filename, the wrong file
gets returned. In combine_to_folder this was observed to grab a freshly
merged PDF instead of the intended source document and soft-delete the
wrong file into the recycle bin.

With the app's own real id formats -- client genDocId() in dms.html
("DOC-" + yyyymmdd + "-" + 6-char base36) and the server's own "DOC-" +
yyyymmdd + "-" + token_hex(4) -- two ids from the SAME generator are
always the same fixed length, so a substring match between two of them
reduces to exact equality (never a real collision). The risk is a
same-day CLIENT id (19 chars) being a coincidental substring of a
same-day SERVER-generated id (21 chars, as combine-to-folder/convert-to-
pdf/plot-snapshot/etc. all produce) or vice versa -- both share the
"DOC-YYYYMMDD-" prefix, so it takes only a 6-character coincidental match
at the right offset, not a full-id coincidence. Low probability with
today's id formats, but the underlying match is not actually bounded by
anything, so this is a latent correctness bug, not a hypothetical one --
this test reproduces it directly against _find_doc_files() rather than
relying on getting lucky/unlucky with random ids in a route-level test.

Not treated as fixed here -- _find_doc_files/_doc_id_from_filename are
used by ~20 call sites (see the doc-filename-layout refactor), so
tightening the match is a deliberate follow-up, not a drive-by change.

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
    def test_short_id_that_is_a_prefix_of_a_longer_id_can_match_the_wrong_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = _isolate(Path(tmpdir))
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

            # KNOWN BUG: both files match the substring search, so callers
            # taking matches[0] (delete_doc, rename_doc_file,
            # combine_to_folder's recycle-bin move, ...) can silently act
            # on the wrong document. This assertion documents the current
            # (broken) behavior; flip it to assertEqual(matches, [wanted])
            # once _find_doc_files is tightened to an exact-token match.
            self.assertEqual(len(matches), 2)


if __name__ == "__main__":
    unittest.main()

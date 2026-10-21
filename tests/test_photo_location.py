"""Photo location folders: Year/Month/<place> grouping.

Covers the pure rules (photo_location.py), the remembered-radius settings
endpoints, the place-name endpoint, the optional photo_location upload field,
and the phone-upload path (mobile_upload), which does its own clustering and
background naming server-side. The desktop client's JS copy of the rules is
exercised separately (see the loc-helpers block in dms.html).

Isolates CONFIG_PATH + the storage path so nothing touches real settings or
a live storage folder.
"""
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dms_server
import photo_location as ploc

BOSTON = (42.3601, -71.0589)
CAMBRIDGE = (42.3736, -71.1097)      # ~2.6 mi from Boston
NYC = (40.7128, -74.0060)            # ~190 mi from Boston


def _isolate(tmp_path: Path) -> Path:
    storage_root = tmp_path / "storage"
    dms_server.CONFIG_PATH = tmp_path / "config.json"
    dms_server._storage_path_override = str(storage_root)
    dms_server.save_config({"storage_path": str(storage_root)})
    return storage_root


def _jpeg(lat=None, lon=None, taken="2026:08:15 10:00:00") -> bytes:
    from PIL import Image
    img = Image.new("RGB", (8, 8), "red")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = taken
    if lat is not None:
        gps = exif.get_ifd(0x8825)
        gps[1] = "N" if lat >= 0 else "S"
        gps[2] = _dms(abs(lat))
        gps[3] = "E" if lon >= 0 else "W"
        gps[4] = _dms(abs(lon))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _dms(deg):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((deg - d) * 60 - m) * 60, 4)
    return (d, m, s)


class PureRuleTests(unittest.TestCase):
    def test_haversine_known_distances(self):
        self.assertAlmostEqual(ploc.haversine_miles(*BOSTON, *NYC), 190, delta=5)
        self.assertLess(ploc.haversine_miles(*BOSTON, *CAMBRIDGE), 4)
        self.assertEqual(ploc.haversine_miles(*BOSTON, *BOSTON), 0)

    def test_unit_conversion(self):
        self.assertAlmostEqual(ploc.radius_to_miles(16.09344, "km"), 10)
        self.assertEqual(ploc.radius_to_miles(10, "mi"), 10)

    def test_normalize_settings_defaults_and_validation(self):
        self.assertEqual(ploc.normalize_settings(None), {"enabled": True, "radius": 10.0, "unit": "mi"})
        self.assertEqual(ploc.normalize_settings({"radius": "abc"})["radius"], 10.0)
        self.assertEqual(ploc.normalize_settings({"radius": 0})["radius"], 10.0)
        self.assertEqual(ploc.normalize_settings({"radius": -3})["radius"], 10.0)
        self.assertEqual(ploc.normalize_settings({"radius": 1e9})["radius"], 10.0)
        self.assertEqual(ploc.normalize_settings({"radius": float("nan")})["radius"], 10.0)
        self.assertEqual(ploc.normalize_settings({"radius": 5, "unit": "KM"}), {"enabled": True, "radius": 5.0, "unit": "km"})
        self.assertFalse(ploc.normalize_settings({"enabled": False})["enabled"])
        self.assertFalse(ploc.normalize_settings({"enabled": "false"})["enabled"])

    def test_find_cluster_nearest_within_radius(self):
        centers = [{"lat": BOSTON[0], "lon": BOSTON[1], "n": "bos"},
                   {"lat": NYC[0], "lon": NYC[1], "n": "nyc"}]
        self.assertEqual(ploc.find_cluster(centers, *CAMBRIDGE, 10)["n"], "bos")
        self.assertIsNone(ploc.find_cluster(centers, *CAMBRIDGE, 1))
        self.assertEqual(ploc.find_cluster(centers, 40.8, -74.0, 50)["n"], "nyc")
        self.assertIsNone(ploc.find_cluster([], *BOSTON, 10))

    def test_node_centers_only_location_folders(self):
        month = {"children": [
            {"name": "plain"},
            {"name": "A", "geoCluster": True, "geoCenter": {"lat": 1, "lon": 2}},
            {"name": "B", "geoCenter": {"lat": 3, "lon": 4}},          # no geoCluster flag
            {"name": "C", "geoCluster": True},                          # no center
        ]}
        got = ploc.node_centers(month)
        self.assertEqual([c["node"]["name"] for c in got], ["A"])

    def test_names(self):
        self.assertEqual(ploc.provisional_name([]), "Location 1")
        self.assertEqual(ploc.provisional_name(["Location 1", "location 2"]), "Location 3")
        self.assertEqual(ploc.unique_name("Boston", ["Boston"]), "Boston (2)")
        self.assertEqual(ploc.unique_name("Boston", ["boston", "Boston (2)"]), "Boston (3)")
        self.assertEqual(ploc.unique_name("Boston", ["Salem"]), "Boston")


class SettingsEndpointTests(unittest.TestCase):
    def test_default_then_remembered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            self.assertEqual(client.get("/api/photo-location-settings").get_json(),
                             {"enabled": True, "radius": 10.0, "unit": "mi"})
            r = client.post("/api/photo-location-settings", json={"enabled": True, "radius": 25, "unit": "km"})
            self.assertEqual(r.get_json(), {"enabled": True, "radius": 25.0, "unit": "km"})
            self.assertEqual(client.get("/api/photo-location-settings").get_json(),
                             {"enabled": True, "radius": 25.0, "unit": "km"})

    def test_invalid_post_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            r = client.post("/api/photo-location-settings", json={"radius": "junk", "unit": "km"})
            self.assertEqual(r.get_json(), {"enabled": True, "radius": 10.0, "unit": "mi"})

    def test_turning_off_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            client.post("/api/photo-location-settings", json={"enabled": False, "radius": 3, "unit": "mi"})
            self.assertFalse(client.get("/api/photo-location-settings").get_json()["enabled"])


class NameEndpointTests(unittest.TestCase):
    def setUp(self):
        dms_server._GEOCODE_CACHE.clear()
        self.addCleanup(dms_server._GEOCODE_CACHE.clear)
        p = mock.patch.object(dms_server, "_NOMINATIM_MIN_INTERVAL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_names_shortened_and_folder_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _isolate(Path(tmpdir))
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_network",
                                   return_value="Cambridge, Middlesex County, Massachusetts") as net:
                r = client.post("/api/photo-location-names", json={"points": [
                    {"id": "N1", "lat": CAMBRIDGE[0], "lon": CAMBRIDGE[1]},
                    {"id": "N2", "lat": CAMBRIDGE[0], "lon": CAMBRIDGE[1]},   # same spot: cached
                    {"id": "bad", "lat": "x"},                                  # skipped
                ]})
            self.assertEqual(r.get_json()["names"], {"N1": "Cambridge, Massachusetts", "N2": "Cambridge, Massachusetts"})
            self.assertEqual(net.call_count, 1)

    def test_two_part_and_unsafe_characters(self):
        self.assertEqual(dms_server._short_place_name("Paris, France"), "Paris, France")
        self.assertEqual(dms_server._short_place_name(""), "")
        with mock.patch.object(dms_server, "_reverse_geocode_network", return_value="A/B: C, Somewhere"):
            self.assertNotIn("/", dms_server._place_name_for(1.0, 2.0))


class UploadFieldTests(unittest.TestCase):
    def test_photo_location_field_nests_file_under_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            tree = {"id": "ROOT", "name": "Root", "children": [], "documents": []}
            dms_server.write_index({"tree": tree, "docIndex": []})
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_and_patch"):
                resp = client.post("/api/docs", data={
                    "file": (io.BytesIO(_jpeg(*BOSTON)), "a.jpg"), "doc_id": "DOC-1", "node_id": "ROOT",
                    "photo_year": "2026", "photo_month": "08", "photo_location": "Location 1",
                    "photo_lat": str(BOSTON[0]), "photo_lon": str(BOSTON[1]),
                }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            found = list((storage / "docs").rglob("*DOC-1*"))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].parent.relative_to(storage / "docs").parts, ("Root", "2026", "08", "Location 1"))

    def test_without_field_behaviour_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []}, "docIndex": []})
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_and_patch"):
                resp = client.post("/api/docs", data={
                    "file": (io.BytesIO(_jpeg(*BOSTON)), "a.jpg"), "doc_id": "DOC-2", "node_id": "ROOT",
                    "photo_year": "2026", "photo_month": "08",
                }, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200)
            found = list((storage / "docs").rglob("*DOC-2*"))
            self.assertEqual(found[0].parent.relative_to(storage / "docs").parts, ("Root", "2026", "08"))

    def test_unsafe_location_name_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []}, "docIndex": []})
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_and_patch"):
                client.post("/api/docs", data={
                    "file": (io.BytesIO(_jpeg()), "a.jpg"), "doc_id": "DOC-3", "node_id": "ROOT",
                    "photo_year": "2026", "photo_month": "08", "photo_location": "../../evil",
                }, content_type="multipart/form-data")
            found = list(Path(tmpdir).rglob("*DOC-3*"))
            self.assertEqual(len(found), 1)
            self.assertTrue(str(found[0]).startswith(str(storage / "docs")))


class MobileUploadTests(unittest.TestCase):
    def setUp(self):
        dms_server._GEOCODE_CACHE.clear()
        self.addCleanup(dms_server._GEOCODE_CACHE.clear)
        p = mock.patch.object(dms_server, "_NOMINATIM_MIN_INTERVAL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)
        orig = dms_server._get_max_documents
        self.addCleanup(setattr, dms_server, "_get_max_documents", orig)

    def _upload(self, client, files):
        data = {"node_id": "ROOT", "file": [(io.BytesIO(b), n) for n, b in files]}
        return client.post("/api/mobile/upload", data=data, content_type="multipart/form-data")

    def _wait_named(self, storage):
        for _ in range(100):
            tree = dms_server.read_index()["tree"]
            if not _pending(tree):
                return tree
            time.sleep(0.05)
        self.fail("location folders never got named")

    def test_clusters_by_radius_names_and_syncs_disk(self):
        places = {round(BOSTON[0], 3): "Boston, Suffolk County, Massachusetts",
                  round(NYC[0], 3): "New York, New York County, New York"}

        def fake_geo(lat, lon):
            return places.get(round(lat, 3), "Somewhere, Else")

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                    "docIndex": [], "photoRootNodeId": "ROOT"})
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_network", side_effect=fake_geo):
                r = self._upload(client, [
                    ("bos.jpg", _jpeg(*BOSTON)),
                    ("cam.jpg", _jpeg(*CAMBRIDGE)),       # within 10 mi of Boston -> same folder
                    ("nyc.jpg", _jpeg(*NYC)),             # far -> its own folder
                    ("nogps.jpg", _jpeg()),               # stays in the month folder
                ])
                self.assertEqual(r.status_code, 302)
                self.assertIn("ok=", r.headers["Location"])
                tree = self._wait_named(storage)

            month = tree["children"][0]["children"][0]
            self.assertEqual((tree["children"][0]["name"], month["name"]), ("2026", "08"))
            names = sorted(c["name"] for c in month["children"])
            self.assertEqual(names, ["Boston, Massachusetts", "New York, New York"])
            by_name = {c["name"]: c for c in month["children"]}
            self.assertEqual(len(by_name["Boston, Massachusetts"]["documents"]), 2)
            self.assertEqual(len(by_name["New York, New York"]["documents"]), 1)
            self.assertEqual(len(month["documents"]), 1)          # the no-GPS photo
            for c in month["children"]:
                self.assertTrue(c["geoCluster"])
                self.assertNotIn("geoNamePending", c)

            # Files on disk follow the renamed folders.
            deadline = time.time() + 5
            while time.time() < deadline:
                dirs = sorted(p.parent.name for p in (storage / "docs").rglob("*.jpg"))
                if dirs.count("Boston, Massachusetts") == 2 and dirs.count("New York, New York") == 1:
                    break
                time.sleep(0.05)
            dirs = sorted(p.parent.name for p in (storage / "docs").rglob("*.jpg"))
            self.assertEqual(dirs.count("Boston, Massachusetts"), 2, dirs)
            self.assertEqual(dirs.count("New York, New York"), 1, dirs)
            self.assertEqual(dirs.count("08"), 1, dirs)

    def test_later_upload_joins_existing_renamed_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                    "docIndex": [], "photoRootNodeId": "ROOT"})
            client = dms_server.app.test_client()
            with mock.patch.object(dms_server, "_reverse_geocode_network", return_value="Boston, MA"):
                self._upload(client, [("bos.jpg", _jpeg(*BOSTON))])
                self._wait_named(storage)
                self._upload(client, [("cam.jpg", _jpeg(*CAMBRIDGE))])
                tree = self._wait_named(storage)
            month = tree["children"][0]["children"][0]
            self.assertEqual(len(month["children"]), 1)
            self.assertEqual(len(month["children"][0]["documents"]), 2)

    def test_small_radius_splits_and_disabled_means_flat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                    "docIndex": [], "photoRootNodeId": "ROOT"})
            client = dms_server.app.test_client()
            client.post("/api/photo-location-settings", json={"enabled": True, "radius": 1, "unit": "mi"})
            with mock.patch.object(dms_server, "_reverse_geocode_network", side_effect=["Boston, MA", "Cambridge, MA"]):
                self._upload(client, [("bos.jpg", _jpeg(*BOSTON)), ("cam.jpg", _jpeg(*CAMBRIDGE))])
                tree = self._wait_named(storage)
            self.assertEqual(len(tree["children"][0]["children"][0]["children"]), 2)

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                    "docIndex": [], "photoRootNodeId": "ROOT"})
            client = dms_server.app.test_client()
            client.post("/api/photo-location-settings", json={"enabled": False, "radius": 10, "unit": "mi"})
            with mock.patch.object(dms_server, "_reverse_geocode_and_patch"):
                self._upload(client, [("bos.jpg", _jpeg(*BOSTON))])
            month = dms_server.read_index()["tree"]["children"][0]["children"][0]
            self.assertEqual(month["children"], [])
            self.assertEqual(len(month["documents"]), 1)

    def test_km_radius_is_converted(self):
        # Boston -> Cambridge is ~2.6 mi (~4.2 km): 3 km splits them, 5 km joins them.
        for radius, expected in ((3, 2), (5, 1)):
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = _isolate(Path(tmpdir))
                dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                        "docIndex": [], "photoRootNodeId": "ROOT"})
                client = dms_server.app.test_client()
                client.post("/api/photo-location-settings", json={"enabled": True, "radius": radius, "unit": "km"})
                with mock.patch.object(dms_server, "_reverse_geocode_network", side_effect=["P1, X", "P2, Y"]):
                    self._upload(client, [("bos.jpg", _jpeg(*BOSTON)), ("cam.jpg", _jpeg(*CAMBRIDGE))])
                    tree = self._wait_named(storage)
                self.assertEqual(len(tree["children"][0]["children"][0]["children"]), expected, radius)

    def test_duplicate_place_names_get_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _isolate(Path(tmpdir))
            dms_server.write_index({"tree": {"id": "ROOT", "name": "Root", "children": [], "documents": []},
                                    "docIndex": [], "photoRootNodeId": "ROOT"})
            client = dms_server.app.test_client()
            client.post("/api/photo-location-settings", json={"enabled": True, "radius": 1, "unit": "mi"})
            with mock.patch.object(dms_server, "_reverse_geocode_network", return_value="Same Place, MA"):
                # Two far-apart photos whose lookups return the same name.
                self._upload(client, [("bos.jpg", _jpeg(*BOSTON)), ("nyc.jpg", _jpeg(*NYC))])
                tree = self._wait_named(storage)
            names = sorted(c["name"] for c in tree["children"][0]["children"][0]["children"])
            self.assertEqual(names, ["Same Place, MA", "Same Place, MA (2)"])


def _pending(node):
    if node.get("geoNamePending"):
        return True
    return any(_pending(c) for c in node.get("children") or [])


if __name__ == "__main__":
    unittest.main()

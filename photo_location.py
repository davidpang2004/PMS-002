"""Pure helpers for grouping photos into per-location folders.

Under each Year/Month photo folder, photos that carry GPS are filed into a
"location folder": a child node holding a ``geoCenter`` ({lat, lon}) and the
``geoCluster`` flag. A photo joins the nearest existing location folder whose
center is within the radius, otherwise it starts a new one centered on
itself. Photos without GPS stay directly in the month folder.

dms.html carries a JS copy of the same rules (see the ``loc-helpers`` block
there) because the desktop client builds the folder tree itself; keep the
two in step. No I/O happens in this module, so it is cheap to unit-test.
"""
from __future__ import annotations

import math

KM_PER_MILE = 1.609344
EARTH_RADIUS_MILES = 3958.7613

DEFAULT_SETTINGS = {"enabled": True, "radius": 10.0, "unit": "mi"}
MIN_RADIUS_MILES = 0.05
MAX_RADIUS_MILES = 12000.0


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def radius_to_miles(value: float, unit: str) -> float:
    return value / KM_PER_MILE if unit == "km" else value


def normalize_settings(raw: dict | None) -> dict:
    """Coerce stored/posted settings into a valid {enabled, radius, unit}."""
    raw = raw or {}
    unit = "km" if str(raw.get("unit", "")).lower() == "km" else "mi"
    try:
        radius = float(raw.get("radius", DEFAULT_SETTINGS["radius"]))
    except (TypeError, ValueError):
        radius = float("nan")
    miles = radius_to_miles(radius, unit)
    if not math.isfinite(radius) or miles < MIN_RADIUS_MILES or miles > MAX_RADIUS_MILES:
        radius = DEFAULT_SETTINGS["radius"]
        unit = DEFAULT_SETTINGS["unit"]
    enabled = raw.get("enabled", DEFAULT_SETTINGS["enabled"])
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("false", "0", "no", "")
    return {"enabled": bool(enabled), "radius": radius, "unit": unit}


def settings_radius_miles(settings: dict) -> float:
    s = normalize_settings(settings)
    return radius_to_miles(s["radius"], s["unit"])


def find_cluster(centers, lat: float, lon: float, radius_miles: float):
    """Return the nearest entry of ``centers`` within the radius, else None.

    ``centers`` is an iterable of dicts with ``lat`` and ``lon`` (any extra
    keys, such as the folder node's id, ride along untouched).
    """
    best, best_d = None, None
    for c in centers:
        d = haversine_miles(lat, lon, c["lat"], c["lon"])
        if d <= radius_miles and (best_d is None or d < best_d):
            best, best_d = c, d
    return best


def node_centers(month_node: dict) -> list:
    """Location-folder centers among a month node's direct children."""
    out = []
    for child in month_node.get("children") or []:
        gc = child.get("geoCenter")
        if child.get("geoCluster") and gc and gc.get("lat") is not None and gc.get("lon") is not None:
            out.append({"lat": float(gc["lat"]), "lon": float(gc["lon"]), "node": child})
    return out


def unique_name(base: str, taken) -> str:
    taken_l = {str(n).lower() for n in taken}
    if base.lower() not in taken_l:
        return base
    i = 2
    while f"{base} ({i})".lower() in taken_l:
        i += 1
    return f"{base} ({i})"


def provisional_name(sibling_names) -> str:
    """Placeholder folder name until the place name has been looked up."""
    return unique_name_numbered("Location", sibling_names)


def unique_name_numbered(prefix: str, taken) -> str:
    taken_l = {str(n).lower() for n in taken}
    i = 1
    while f"{prefix} {i}".lower() in taken_l:
        i += 1
    return f"{prefix} {i}"

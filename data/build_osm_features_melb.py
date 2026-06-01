"""Download OpenStreetMap point/line/polygon features for Greater Melbourne via
the Overpass API and write each themed group to a static GeoJSON.

Groups:
  amenities   hospitals, shopping centres/malls, supermarkets, universities,
              libraries, community centres, places of worship  (POSITIVE proximity)
  powerlines  power=line (esp. high-voltage transmission)       (NEGATIVE proximity)
  parks       leisure=park/garden/recreation_ground, landuse=recreation (POSITIVE)
  nuisance    landuse=industrial, man_made=wastewater_plant, landuse=cemetery,
              amenity=prison, landuse=quarry, landfill                 (NEGATIVE)

Each output:  static/overlays/osm_<group>.geojson(.gz)

Overpass returns nodes/ways/relations; we convert each to a GeoJSON Point
(node, or centroid of a way's bbox) or LineString (power lines) so the front
end can render simply. Uses mirror fallback + Accept:*/* (the public servers
406 on a missing/odd Accept header) + retries.
"""
import gzip, json, os, shutil, time, urllib.parse, urllib.request

BBOX = "-38.4,144.4,-37.4,145.8"   # S,W,N,E  (Overpass order)
OUT_DIR = "static/overlays"
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Each group: list of (overpass-selector, category-label). The selector is the
# bit inside nwr[...]; we wrap it per-element-type in the query builder.
GROUPS = {
    "amenities": [
        ('["amenity"="hospital"]',          "hospital"),
        ('["shop"="mall"]',                 "shopping"),
        ('["shop"="department_store"]',     "shopping"),
        ('["shop"="supermarket"]',          "supermarket"),
        ('["amenity"="university"]',        "university"),
        ('["amenity"="library"]',           "library"),
        ('["amenity"="community_centre"]',  "community"),
        ('["amenity"="place_of_worship"]',  "worship"),
    ],
    "parks": [
        ('["leisure"="park"]',                "park"),
        ('["leisure"="garden"]',              "garden"),
        ('["leisure"="recreation_ground"]',   "recreation"),
        ('["landuse"="recreation_ground"]',   "recreation"),
        ('["leisure"="nature_reserve"]',      "reserve"),
    ],
    "nuisance": [
        ('["landuse"="industrial"]',          "industrial"),
        ('["man_made"="wastewater_plant"]',   "wastewater"),
        ('["landuse"="landfill"]',            "landfill"),
        ('["landuse"="cemetery"]',            "cemetery"),
        ('["amenity"="prison"]',              "prison"),
        ('["landuse"="quarry"]',              "quarry"),
    ],
    # power lines are lines, handled separately so we keep the geometry
    "powerlines": [
        ('["power"="line"]',  "transmission"),
        ('["power"="minor_line"]', "minor_line"),
    ],
}


def _overpass(query, attempts_per_mirror=2):
    data = urllib.parse.urlencode({"data": query}).encode()
    for mirror in MIRRORS:
        for i in range(attempts_per_mirror):
            try:
                req = urllib.request.Request(
                    mirror, data=data,
                    headers={"User-Agent": "PropertyManager/1.0 (research)",
                             "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read())
            except Exception as e:
                print(f"    [{mirror.split('/')[2]} try {i+1}] {e}", flush=True)
                time.sleep(5)
    raise RuntimeError("all Overpass mirrors failed")


def _centroid_from_bounds(b):
    return [(b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2]


def _trunc(c):
    if isinstance(c, (int, float)): return round(c, 5)
    return [_trunc(x) for x in c]


def _download_points(group, selectors):
    """Hospitals/parks/nuisance — render each element as a single Point
    (node coords, or way/relation bbox centroid)."""
    parts = []
    for sel, _label in selectors:
        parts.append(f'nwr{sel}({BBOX});')
    q = f'[out:json][timeout:120];({"".join(parts)});out center tags;'
    d = _overpass(q)
    # Map element -> category by re-checking which selector tag it carries.
    feats = []
    for el in d.get("elements", []):
        tags = el.get("tags") or {}
        # find category
        cat = None
        for sel, label in selectors:
            # sel like ["amenity"="hospital"]  → key, val
            k, v = sel.strip("[]").replace('"', "").split("=")
            if tags.get(k) == v:
                cat = label; break
        if not cat:
            continue
        if el["type"] == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            c = el.get("center")
            if not c: continue
            lon, lat = c["lon"], c["lat"]
        if lon is None or lat is None:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {
                "cat":  cat,
                "name": tags.get("name") or "",
            },
        })
    return feats


def _ring(coords):
    """Ensure a coordinate ring is closed (first == last)."""
    if len(coords) >= 3 and coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return coords


def _download_polys(group, selectors):
    """Parks / nuisance — keep AREA geometry (Polygon) so the factor engine can
    measure distance to the boundary, not a centroid. Ways become a Polygon;
    relation outer members each become a Polygon. Nodes (rare) become a tiny
    point→1-vertex skip."""
    way_parts, rel_parts = [], []
    for sel, _ in selectors:
        way_parts.append(f'way{sel}({BBOX});')
        rel_parts.append(f'relation{sel}({BBOX});')
    q = (f'[out:json][timeout:150];'
         f'({"".join(way_parts)}{"".join(rel_parts)});out geom tags;')
    d = _overpass(q)

    def cat_of(tags):
        for sel, label in selectors:
            k, v = sel.strip("[]").replace('"', "").split("=")
            if tags.get(k) == v:
                return label
        return None

    feats = []
    for el in d.get("elements", []):
        tags = el.get("tags") or {}
        cat = cat_of(tags)
        if not cat:
            continue
        name = tags.get("name") or ""
        rings = []
        if el.get("type") == "way" and el.get("geometry"):
            r = _ring([[round(p["lon"], 5), round(p["lat"], 5)] for p in el["geometry"]])
            if len(r) >= 4:
                rings.append(r)
        elif el.get("type") == "relation":
            for m in (el.get("members") or []):
                if m.get("role") == "outer" and m.get("geometry"):
                    r = _ring([[round(p["lon"], 5), round(p["lat"], 5)] for p in m["geometry"]])
                    if len(r) >= 4:
                        rings.append(r)
        for r in rings:
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [r]},
                "properties": {"cat": cat, "name": name},
            })
    return feats


def _download_mixed(group, selectors):
    """Amenities — emit a Polygon for area features (ways / relation outers)
    and a Point for node features, so distance is measured to the grounds of
    big amenities (hospitals, universities, malls) not just a label point."""
    parts = []
    for sel, _ in selectors:
        parts.append(f'nwr{sel}({BBOX});')
    q = f'[out:json][timeout:150];({"".join(parts)});out geom tags;'
    d = _overpass(q)

    def cat_of(tags):
        for sel, label in selectors:
            k, v = sel.strip("[]").replace('"', "").split("=")
            if tags.get(k) == v:
                return label
        return None

    feats = []
    for el in d.get("elements", []):
        tags = el.get("tags") or {}
        cat = cat_of(tags)
        if not cat:
            continue
        name = tags.get("name") or ""
        t = el.get("type")
        if t == "node":
            lon, lat = el.get("lon"), el.get("lat")
            if lon is None:
                continue
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                          "properties": {"cat": cat, "name": name}})
        elif t == "way" and el.get("geometry"):
            r = _ring([[round(p["lon"], 5), round(p["lat"], 5)] for p in el["geometry"]])
            geom = ({"type": "Polygon", "coordinates": [r]} if len(r) >= 4
                    else {"type": "Point", "coordinates": r[0]} if r else None)
            if geom:
                feats.append({"type": "Feature", "geometry": geom,
                              "properties": {"cat": cat, "name": name}})
        elif t == "relation":
            for m in (el.get("members") or []):
                if m.get("role") == "outer" and m.get("geometry"):
                    r = _ring([[round(p["lon"], 5), round(p["lat"], 5)] for p in m["geometry"]])
                    if len(r) >= 4:
                        feats.append({"type": "Feature",
                                      "geometry": {"type": "Polygon", "coordinates": [r]},
                                      "properties": {"cat": cat, "name": name}})
    return feats


def _download_lines(group, selectors):
    """Power lines — keep the full LineString geometry."""
    parts = []
    for sel, _ in selectors:
        parts.append(f'way{sel}({BBOX});')
    q = f'[out:json][timeout:120];({"".join(parts)});out geom tags;'
    d = _overpass(q)
    feats = []
    for el in d.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags") or {}
        coords = [[round(p["lon"], 5), round(p["lat"], 5)] for p in el["geometry"]]
        if len(coords) < 2:
            continue
        cat = "minor_line" if tags.get("power") == "minor_line" else "transmission"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "cat":     cat,
                "voltage": tags.get("voltage") or "",
                "name":    tags.get("name") or tags.get("operator") or "",
            },
        })
    return feats


def _write(name, feats):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"osm_{name}.geojson")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, separators=(",", ":"))
    raw_kb = os.path.getsize(out) / 1024
    gz = out + ".gz"
    with open(out, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, length=4 * 1024 * 1024)
    print(f"   wrote {out}: {len(feats)} feats, {raw_kb:.0f} KB -> {os.path.getsize(gz)/1024:.0f} KB gz")


def main():
    import sys
    only = sys.argv[1:] if len(sys.argv) > 1 else list(GROUPS.keys())
    for name in only:
        if name not in GROUPS:
            print(f"skip unknown {name}"); continue
        print(f"\n=== {name} ===", flush=True)
        try:
            if name == "powerlines":
                feats = _download_lines(name, GROUPS[name])
            elif name in ("parks", "nuisance"):
                feats = _download_polys(name, GROUPS[name])   # areas → Polygons
            elif name == "amenities":
                feats = _download_mixed(name, GROUPS[name])   # polygons + points
            else:
                feats = _download_points(name, GROUPS[name])
            _write(name, feats)
        except Exception as ex:
            print(f"  !! {name} failed: {ex}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()

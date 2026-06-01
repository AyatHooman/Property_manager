"""Download several DEECA Vicmap layers for Greater Melbourne and write each to
a pre-gzipped static GeoJSON the front-end can fetch directly.

Layers (all comfortably static-gzip scale — tens of thousands of features):
  zones      plan_zone               planning zones (GRZ/NRZ/RGZ/IN1Z/…)  ~22.7k
  roads      tr_road  class_code<=3  freeway/highway/arterial only        ~47k
  rivers     hy_watercourse          rivers / creeks (lines)              ~77k
  waterbody  hy_water_area_polygon   lakes / reservoirs / wide rivers     ~17k
  airports   tr_airport_area_polygon airport footprints                   small

Each output:  static/overlays/<name>.geojson(.gz)
Run with the Anaconda base python (geo_env's numpy DLL is broken but this
script needs no geo libs — only stdlib + urllib).
"""
import gzip, json, os, shutil, time, urllib.parse, urllib.request

WFS = "https://opendata.maps.vic.gov.au/geoserver/wfs"
W, S, E, N = 144.4, -38.4, 145.8, -37.4     # Greater Melbourne
OUT_DIR = "static/overlays"
DELAY = 1.0

LAYERS = {
    "zones": {
        "typename": "open-data-platform:plan_zone",
        "filter":   None,
        "page":     1000,
        "props":    {"scheme_code": "scheme", "zone_code": "zone_code",
                     "zone_description": "desc", "lga": "lga"},
    },
    "roads": {
        "typename": "open-data-platform:tr_road",
        "filter":   "class_code<=3",
        "page":     1000,
        "props":    {"ezi_road_name_label": "name", "class_code": "class",
                     "road_type": "road_type"},
    },
    "rivers": {
        "typename": "open-data-platform:hy_watercourse",
        "filter":   None,
        "page":     2000,
        "props":    {"name": "name", "feature_type_code": "ftype"},
    },
    "waterbody": {
        "typename": "open-data-platform:hy_water_area_polygon",
        "filter":   None,
        "page":     1000,
        "props":    {"name": "name", "feature_type_code": "ftype"},
    },
    "airports": {
        "typename": "open-data-platform:tr_airport_area_polygon",
        "filter":   None,
        "page":     500,
        "props":    {"name": "name", "feature_type_code": "ftype"},
    },
}


def _req(url, attempts=6):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PropertyManager/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            last = e
            wait = 10 * (i + 1)
            print(f"    [retry {i+1}/{attempts} in {wait}s] {e}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"request failed: {last}")


def _hits(typename, filt):
    cql = (f"{filt} AND " if filt else "") + f"BBOX(geom,{W},{S},{E},{N},'EPSG:4326')"
    u = WFS + "?" + urllib.parse.urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": typename, "resultType": "hits", "CQL_FILTER": cql})
    body = _req(u).decode("utf-8", "replace")
    import re
    m = re.search(r'numberMatched="(\d+)"', body)
    return int(m.group(1)) if m else -1


def _trunc(c):
    if isinstance(c, (int, float)): return round(c, 5)
    return [_trunc(x) for x in c]


def _norm(feat, propmap):
    g = feat.get("geometry")
    if g and g.get("coordinates"):
        g["coordinates"] = _trunc(g["coordinates"])
    p = feat.get("properties") or {}
    feat["properties"] = {dst: p.get(src) for src, dst in propmap.items()}
    feat.pop("bbox", None); feat.pop("id", None); feat.pop("geometry_name", None)
    return feat


def _download(name, cfg):
    typename = cfg["typename"]; filt = cfg["filter"]; page = cfg["page"]
    expected = _hits(typename, filt)
    print(f"\n=== {name}  {typename}  (expected {expected:,}) ===", flush=True)
    if expected == 0:
        print("   nothing"); return
    cql = (f"{filt} AND " if filt else "") + f"BBOX(geom,{W},{S},{E},{N},'EPSG:4326')"
    feats = []; start = 0; t0 = time.time()
    while True:
        time.sleep(DELAY)
        u = WFS + "?" + urllib.parse.urlencode({
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeName": typename, "outputFormat": "application/json",
            "srsName": "EPSG:4326", "count": str(page), "startIndex": str(start),
            "CQL_FILTER": cql})
        d = json.loads(_req(u))
        batch = d.get("features") or []
        if not batch: break
        for f in batch:
            feats.append(_norm(f, cfg["props"]))
        start += len(batch)
        print(f"   +{len(batch):4} (cum {start}/{expected})  {start/max(expected,1)*100:5.1f}%", flush=True)
        if len(batch) < page: break

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{name}.geojson")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, separators=(",", ":"))
    raw_mb = os.path.getsize(out) / 1024 / 1024
    gz = out + ".gz"
    with open(out, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, length=8 * 1024 * 1024)
    gz_mb = os.path.getsize(gz) / 1024 / 1024
    print(f"   wrote {out}: {len(feats)} feats, {raw_mb:.1f} MB -> {gz_mb:.1f} MB gz, {time.time()-t0:.0f}s")


def main():
    import sys
    only = sys.argv[1:] if len(sys.argv) > 1 else list(LAYERS.keys())
    for name in only:
        if name not in LAYERS:
            print(f"skip unknown layer {name}"); continue
        try:
            _download(name, LAYERS[name])
        except Exception as ex:
            print(f"  !! {name} failed: {ex}", flush=True)


if __name__ == "__main__":
    main()

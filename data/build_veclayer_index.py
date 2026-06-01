"""Index the HEAVY value-context layers (zones / roads / rivers / waterbody /
parks) into the local spatial DB + R-tree so the map can bbox-fetch only the
features in the current viewport instead of loading the whole 10-56 MB file.

Reads the pre-built static GeoJSON(.gz) files; writes, per layer:
    data/<name>.idx + .dat   libspatialindex R-tree
    data/veclayers.db table <name>(id INTEGER PRIMARY KEY, props TEXT, geom TEXT)

These are the layers too big to render fully at once; the light ones
(amenities, nuisance, powerlines, airports) keep loading whole.
"""
import gzip, json, os, sqlite3, time
import rtree

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_BASE, "data", "veclayers.db")
OVL = os.path.join(_BASE, "static", "overlays")

LAYERS = ["zones", "roads", "rivers", "waterbody", "parks"]
# Index name -> static file basename (most match; parks comes from OSM file).
_FILE = {"parks": "osm_parks"}


def _read(name):
    fb = _FILE.get(name, name)
    gz = os.path.join(OVL, f"{fb}.geojson.gz")
    raw = os.path.join(OVL, f"{fb}.geojson")
    if os.path.exists(gz):
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            return json.load(f)
    if os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            return json.load(f)
    return None


def _bbox_of(geom):
    minx = miny = float("inf"); maxx = maxy = float("-inf")
    def walk(c):
        nonlocal minx, miny, maxx, maxy
        if c and isinstance(c[0], (int, float)):
            x, y = c[0], c[1]
            minx = min(minx, x); maxx = max(maxx, x)
            miny = min(miny, y); maxy = max(maxy, y)
            return
        for x in c:
            walk(x)
    walk(geom["coordinates"])
    return minx, miny, maxx, maxy


def _build(con, name):
    gj = _read(name)
    if not gj:
        print(f"  [{name}] SKIP — file missing"); return
    cur = con.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {name}")
    cur.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, props TEXT, geom TEXT)")
    con.commit()
    cur.execute("PRAGMA journal_mode=OFF"); cur.execute("PRAGMA synchronous=OFF")

    base = os.path.join(_BASE, "data", name)
    for ext in (".idx", ".dat"):
        p = base + ext
        if os.path.exists(p): os.remove(p)
    p = rtree.index.Property(); p.dimension = 2
    p.dat_extension = "dat"; p.idx_extension = "idx"
    idx = rtree.index.Index(base, properties=p)

    feats = gj.get("features", [])
    print(f"\n=== {name}: {len(feats):,} features ===", flush=True)
    rows = []; n = 0; t0 = time.time()
    for feat in feats:
        g = feat.get("geometry")
        if not g or not g.get("coordinates"):
            continue
        n += 1
        try:
            bb = _bbox_of(g)
        except Exception:
            continue
        idx.insert(n, bb)
        rows.append((n, json.dumps(feat.get("properties") or {}, separators=(",", ":")),
                     json.dumps(g, separators=(",", ":"))))
        if len(rows) >= 20000:
            cur.execute("BEGIN")
            cur.executemany(f"INSERT INTO {name} (id,props,geom) VALUES (?,?,?)", rows)
            con.commit(); rows.clear()
            print(f"  {n:,} ({time.time()-t0:.0f}s)", flush=True)
    if rows:
        cur.execute("BEGIN")
        cur.executemany(f"INSERT INTO {name} (id,props,geom) VALUES (?,?,?)", rows)
        con.commit()
    idx.close()
    print(f"  {name}: DONE {n:,} in {time.time()-t0:.0f}s")


def main():
    import sys
    only = sys.argv[1:] or LAYERS
    con = sqlite3.connect(DB)
    try:
        for nm in only:
            _build(con, nm)
    finally:
        con.close()
    print(f"\nveclayers.db ready ({os.path.getsize(DB)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()

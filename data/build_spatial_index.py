"""Build a local rtree spatial index + SQLite payload store for the easements
and lots so Flask can answer bbox queries against on-disk LOCAL data without
re-downloading from the web. The browser fetches only the features in its
current viewport (tens of KB per request), instead of the full multi-hundred-
MB GeoJSON.

Reads:
    static/overlays/easements_vic.geojson.gz  (~1.3M  LineStrings, ~22 MB gz)
    static/overlays/lots_melb.geojson.gz      (~2.35M Polygons,    ~100 MB gz)

Writes (per layer, into the data/ dir):
    {name}.idx + {name}.dat   — libspatialindex on-disk R-tree (~200 MB total)
    spatial.db                — SQLite payload store, id → (pfi, status, geom)

Run once after build_easements_vic.py + build_lots_melb.py. Cheap to re-run
(drops + re-imports). Total run time ~5-10 min for both layers combined.
"""
import gzip, json, os, sqlite3, time
import rtree

DB         = "data/spatial.db"
IDX_DIR    = "data"

LAYERS = [
    {
        "name":     "easements",
        "path":     "static/overlays/easements_vic.geojson.gz",
        "expected": 1_303_740,
    },
    {
        "name":     "lots",
        "path":     "static/overlays/lots_melb.geojson.gz",
        "expected": 2_346_810,
    },
]


def _bbox_of(geom):
    """Min/max lng/lat for any GeoJSON geometry. Walks the coord tree once."""
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    def walk(coords):
        nonlocal minx, miny, maxx, maxy
        if (len(coords) >= 2 and isinstance(coords[0], (int, float))
                and isinstance(coords[1], (int, float))):
            lng, lat = coords[0], coords[1]
            if lng < minx: minx = lng
            if lng > maxx: maxx = lng
            if lat < miny: miny = lat
            if lat > maxy: maxy = lat
            return
        for c in coords:
            walk(c)

    walk(geom["coordinates"])
    return minx, miny, maxx, maxy


def _iter_features(path):
    """Stream features from a line-delimited GeoJSON.gz produced by the
    build scripts (one feature per line, sandwiched between header+footer)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip().rstrip(",")
            if not line.startswith('{"type":"Feature"'):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _build_layer(con: sqlite3.Connection, layer):
    name = layer["name"]
    path = layer["path"]
    expected = layer["expected"]
    if not os.path.exists(path):
        print(f"  [{name}] SKIP: {path} not found")
        return
    file_mb = os.path.getsize(path) / 1024 / 1024
    print(f"\n=== {name} ({file_mb:.1f} MB gzipped, ~{expected:,} features) ===")

    cur = con.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {name}")
    cur.execute(f"""CREATE TABLE {name} (
                       id     INTEGER PRIMARY KEY,
                       pfi    TEXT,
                       status TEXT,
                       geom   TEXT
                   )""")
    con.commit()
    cur.execute("PRAGMA journal_mode = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA cache_size = -200000")  # 200 MB page cache

    # Fresh rtree on disk. Delete any prior files for this layer first.
    idx_base = os.path.join(IDX_DIR, name)
    for ext in (".idx", ".dat"):
        p = idx_base + ext
        if os.path.exists(p): os.remove(p)
    p = rtree.index.Property()
    p.dimension = 2
    p.dat_extension = "dat"
    p.idx_extension = "idx"
    idx = rtree.index.Index(idx_base, properties=p)

    BATCH = 20_000
    batch_feat = []
    n = 0
    t0 = time.time()
    for feat in _iter_features(path):
        geom = feat.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        n += 1
        try:
            minx, miny, maxx, maxy = _bbox_of(geom)
        except Exception:
            continue
        props = feat.get("properties") or {}
        batch_feat.append((n,
                           props.get("pfi"),
                           props.get("status"),
                           json.dumps(geom, separators=(",", ":"))))
        # rtree inserts have to be one-at-a-time per object
        idx.insert(n, (minx, miny, maxx, maxy))

        if len(batch_feat) >= BATCH:
            cur.execute("BEGIN")
            cur.executemany(f"INSERT INTO {name} (id,pfi,status,geom) VALUES (?,?,?,?)",
                            batch_feat)
            con.commit()
            batch_feat.clear()
            elapsed = time.time() - t0
            pct = n / expected * 100 if expected else 0
            eta = elapsed / n * (expected - n) if n else 0
            print(f"  {name}: {n:>9,} ({pct:5.1f}%)  {elapsed:>5.1f}s  ETA {eta/60:.1f}min",
                  flush=True)

    if batch_feat:
        cur.execute("BEGIN")
        cur.executemany(f"INSERT INTO {name} (id,pfi,status,geom) VALUES (?,?,?,?)",
                        batch_feat)
        con.commit()

    # Flush + close the rtree so files land on disk.
    idx.close()
    print(f"  {name}: DONE — {n:,} features in {time.time()-t0:.1f}s")


def main():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    con = sqlite3.connect(DB)
    try:
        for layer in LAYERS:
            _build_layer(con, layer)
    finally:
        con.close()

    size_mb = os.path.getsize(DB) / 1024 / 1024
    print(f"\nSQLite payload: {DB}  ({size_mb:.1f} MB)")
    for layer in LAYERS:
        name = layer["name"]
        idx = os.path.join(IDX_DIR, f"{name}.idx")
        dat = os.path.join(IDX_DIR, f"{name}.dat")
        if os.path.exists(idx) and os.path.exists(dat):
            sz = (os.path.getsize(idx) + os.path.getsize(dat)) / 1024 / 1024
            print(f"  rtree[{name}]: {sz:.1f} MB ({idx}, {dat})")


if __name__ == "__main__":
    main()

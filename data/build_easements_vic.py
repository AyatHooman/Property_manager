"""Bulk-download every Vicmap easement (whole of Victoria) into a single
GeoJSON file, then leave it ready for tippecanoe → PMTiles conversion.

WFS endpoint: https://opendata.maps.vic.gov.au/geoserver/wfs
Layer:        open-data-platform:easement      (LineString geometry)
Fields:       ufi, pfi, status (A=active, R=retired), pfi_created, ufi_created

Pagination is 5000 features/request (well under the server cap of 10000).
Run time is dominated by network latency; expect 25-50 min for ~1.3M features.

After this completes, convert to PMTiles with tippecanoe (one-time WSL setup):
    wsl bash -c "tippecanoe \\
        -o static/overlays/easements.pmtiles \\
        --layer=easements \\
        --maximum-zoom=16 --minimum-zoom=10 \\
        --drop-densest-as-needed \\
        --read-parallel --force \\
        data/easements_vic.geojson"
"""
import json, os, sys, time, urllib.parse, urllib.request

WFS    = "https://opendata.maps.vic.gov.au/geoserver/wfs"
LAYER  = "open-data-platform:easement"
PAGE   = 5000
OUT    = "static/overlays/easements_vic.geojson"   # served directly by Flask
RESUME = "data/easements_vic.progress.json"        # build state (data/ is gitignored)

def _fetch_page(start):
    """One WFS GetFeature request, retried on transient errors."""
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": LAYER, "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": str(PAGE), "startIndex": str(start),
    }
    url = WFS + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "PropertyManager/1.0 (research)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"  [retry {attempt+1}/5 in {wait}s] {e}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"page start={start} failed after 5 retries: {last_err}")

def _trunc(c):
    """Round coordinates to 5 decimals (~1.1m precision) — easements are
    short cadastral lines; sub-metre precision is irrelevant on a map."""
    if isinstance(c, (int, float)): return round(c, 5)
    return [_trunc(x) for x in c]

def _normalise(feat):
    """Drop the parts of each feature we don't render — saves ~30% size."""
    g = feat.get("geometry")
    if g and g.get("coordinates"):
        g["coordinates"] = _trunc(g["coordinates"])
    p = feat.get("properties") or {}
    # Keep only what the popup actually shows.
    feat["properties"] = {
        "pfi":    p.get("pfi"),
        "status": p.get("status"),
    }
    # Drop top-level bbox and id to shrink each line further.
    feat.pop("bbox", None)
    feat.pop("id",   None)
    feat.pop("geometry_name", None)
    return feat

def main():
    # Resume support: if we crashed mid-run, pick up where we left off.
    start = 0
    if os.path.exists(RESUME):
        with open(RESUME) as f:
            start = json.load(f).get("next_start", 0)
        print(f"Resuming from feature {start}")
        out_mode = "ab"
    else:
        out_mode = "wb"

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    # Use line-delimited GeoJSON (one feature per line, sandwiched between
    # header/footer). This lets us stream-append without keeping all features
    # in RAM and lets us resume if interrupted. tippecanoe accepts this format
    # natively (it also accepts standard FeatureCollection).
    if start == 0:
        with open(OUT, "wb") as f:
            f.write(b'{"type":"FeatureCollection","features":[\n')

    total_so_far = start
    t0 = time.time()
    matched_total = None
    while True:
        t_page = time.time()
        d = _fetch_page(start)
        feats = d.get("features") or []
        if matched_total is None:
            matched_total = d.get("numberMatched")
            print(f"Server reports total: {matched_total}")
        if not feats:
            break

        # Append features as comma-separated JSON lines. The trailing comma
        # on the last feature is fixed up in the footer write below.
        with open(OUT, "ab") as f:
            for feat in feats:
                feat = _normalise(feat)
                line = json.dumps(feat, separators=(",", ":")).encode()
                f.write(line)
                f.write(b",\n")

        total_so_far += len(feats)
        with open(RESUME, "w") as f:
            json.dump({"next_start": total_so_far}, f)

        dt = time.time() - t_page
        elapsed = time.time() - t0
        if matched_total:
            pct = total_so_far / matched_total * 100
            eta = elapsed / total_so_far * (matched_total - total_so_far)
            eta_str = f"{eta/60:.1f}min"
        else:
            pct = 0
            eta_str = "?"
        print(f"  +{len(feats):5} (cum {total_so_far:7}/{matched_total or '?'} "
              f"= {pct:5.1f}%) page in {dt:4.1f}s · ETA {eta_str}", flush=True)

        if len(feats) < PAGE:
            break
        start = total_so_far

    # Close the FeatureCollection. Trailing ",\n" of the last feature line is
    # invalid JSON in strict parsers but tippecanoe handles it; we still
    # normalise here for safety by overwriting the last 2 bytes.
    with open(OUT, "r+b") as f:
        f.seek(-2, os.SEEK_END)
        tail = f.read(2)
        if tail == b",\n":
            f.seek(-2, os.SEEK_END)
            f.truncate()
        f.write(b"\n]}\n")

    size_mb = os.path.getsize(OUT) / (1024 * 1024)
    print(f"\nDone: {total_so_far} features, {size_mb:.1f} MB -> {OUT}")
    if os.path.exists(RESUME):
        os.remove(RESUME)

    # Pre-gzip so Flask can serve it with Content-Encoding: gzip (~10× smaller
    # on the wire). Browser decodes transparently.
    import gzip, shutil
    gz = OUT + ".gz"
    print(f"Gzipping -> {gz} ...", flush=True)
    with open(OUT, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=8 * 1024 * 1024)
    print(f"Gzipped: {os.path.getsize(gz)/1024/1024:.1f} MB")

if __name__ == "__main__":
    main()

"""Bulk-download Vicmap PARCEL polygons (lots) for Greater Melbourne and save
to a single GeoJSON that Leaflet.VectorGrid can render in the browser.

We use the geometry-only layer (open-data-platform:parcel_view) — just PFI +
status + the polygon — to keep the file as small as possible. Full lot /
plan / LGA detail is fetched per-parcel on click via /api/lot-detail (which
hits the richer open-data-platform:v_parcel_mp layer keyed by PFI).

Bbox covers Greater Melbourne (Werribee → Healesville → Frankston).
Statewide would be ~4M parcels / 700+ MB; Melbourne-only is ~2M / 250-300 MB.
"""
import json, os, time, urllib.parse, urllib.request

WFS    = "https://opendata.maps.vic.gov.au/geoserver/wfs"
LAYER  = "open-data-platform:parcel_view"
PAGE   = 5000
# Greater Melbourne — chosen to match the same bbox we use for crime/market
# overlays so the layers are visually consistent.
W, S, E, N = 144.4, -38.4, 145.8, -37.4
OUT    = "static/overlays/lots_melb.geojson"
RESUME = "data/lots_melb.progress.json"


def _fetch_page(start):
    """One WFS GetFeature request, retried on transient errors."""
    filt = f"BBOX(geom,{W},{S},{E},{N},'EPSG:4326')"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": LAYER, "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": str(PAGE), "startIndex": str(start),
        "CQL_FILTER": filt,
    }
    url = WFS + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "PropertyManager/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"  [retry {attempt+1}/5 in {wait}s] {e}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"page start={start} failed: {last_err}")


def _trunc(c, prec=5):
    if isinstance(c, (int, float)): return round(c, prec)
    return [_trunc(x, prec) for x in c]


def _normalise(feat):
    g = feat.get("geometry")
    if g and g.get("coordinates"):
        # 5 decimals = ~1.1m horizontal precision — well below the average
        # parcel size at the city scale, and saves ~30% of file size.
        g["coordinates"] = _trunc(g["coordinates"], 5)
    p = feat.get("properties") or {}
    feat["properties"] = {
        "pfi":    p.get("pfi"),
        "status": p.get("status"),
    }
    feat.pop("bbox", None)
    feat.pop("id", None)
    feat.pop("geometry_name", None)
    return feat


def main():
    start = 0
    if os.path.exists(RESUME):
        with open(RESUME) as f:
            start = json.load(f).get("next_start", 0)
        print(f"Resuming from feature {start}")

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
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
            print(f"Server total in bbox: {matched_total}")
        if not feats:
            break

        with open(OUT, "ab") as f:
            for feat in feats:
                feat = _normalise(feat)
                f.write(json.dumps(feat, separators=(",", ":")).encode())
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
            pct = 0; eta_str = "?"
        print(f"  +{len(feats):5} (cum {total_so_far:7}/{matched_total or '?'} "
              f"= {pct:5.1f}%) page in {dt:4.1f}s · ETA {eta_str}", flush=True)

        if len(feats) < PAGE:
            break
        start = total_so_far

    # Close the FeatureCollection.
    with open(OUT, "r+b") as f:
        f.seek(-2, os.SEEK_END)
        if f.read(2) == b",\n":
            f.seek(-2, os.SEEK_END); f.truncate()
        f.write(b"\n]}\n")

    raw_mb = os.path.getsize(OUT) / (1024 * 1024)
    print(f"\nDone: {total_so_far} parcels, {raw_mb:.1f} MB -> {OUT}")
    if os.path.exists(RESUME): os.remove(RESUME)

    # Pre-gzip so Flask can serve it with Content-Encoding: gzip (~10× smaller).
    import gzip, shutil
    gz = OUT + ".gz"
    print(f"Gzipping -> {gz} ...", flush=True)
    with open(OUT, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=8 * 1024 * 1024)
    print(f"Gzipped: {os.path.getsize(gz)/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()

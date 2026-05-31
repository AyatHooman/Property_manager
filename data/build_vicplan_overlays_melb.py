"""Download Vicmap planning-scheme OVERLAYS (the ones ChatGPT listed as
'red flags' for property value) from the DEECA OpenData WFS, for Greater
Melbourne, and write one static GeoJSON per scheme.

Schemes already on the map (heritage / flood-LSIO / bushfire-BMO) are
skipped here. The rest:

  PAO  Public Acquisition Overlay   — gov reserve for road / rail / public
  DDO  Design and Development       — height + form controls
  DPO  Development Plan             — approved plan needed before permits
  DCPO Development Contributions    — infra contribution payments
  EAO  Environmental Audit          — contaminated land
  VPO  Vegetation Protection        — tree removal restrictions
  SLO  Significant Landscape        — landscape protection
  ESO  Environmental Significance   — env controls
  SBO  Special Building (drainage)
  EMO  Erosion Management
  SMO  Salinity Management
  FO   Floodway (distinct from LSIO)

Pagination uses small pages + delays to avoid DEECA's rate limiter
(every probe in PowerShell got 400s when fired back-to-back).

Output:  static/overlays/vic_<lower>.geojson  (one per scheme)
"""
import json, os, time, urllib.parse, urllib.request

WFS    = "https://opendata.maps.vic.gov.au/geoserver/wfs"
LAYER  = "open-data-platform:plan_overlay"
PAGE   = 1000
W, S, E, N = 144.4, -38.4, 145.8, -37.4   # Greater Melbourne
OUT_DIR = "static/overlays"
DELAY  = 1.0   # seconds between requests to stay under the WFS rate limit

SCHEMES = [
    ("PAO",  "Public Acquisition"),
    ("DDO",  "Design and Development"),
    ("DPO",  "Development Plan"),
    ("DCPO", "Development Contributions"),
    ("EAO",  "Environmental Audit"),
    ("VPO",  "Vegetation Protection"),
    ("SLO",  "Significant Landscape"),
    ("ESO",  "Environmental Significance"),
    ("SBO",  "Special Building (drainage)"),
    ("EMO",  "Erosion Management"),
    ("SMO",  "Salinity Management"),
    ("FO",   "Floodway"),
]


def _req(url: str, attempts: int = 6):
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
    raise RuntimeError(f"request failed after {attempts} retries: {last}")


def _hits(scheme: str) -> int:
    cql = f"scheme_code='{scheme}' AND BBOX(geom,{W},{S},{E},{N},'EPSG:4326')"
    u = WFS + "?" + urllib.parse.urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": LAYER, "resultType": "hits", "CQL_FILTER": cql,
    })
    body = _req(u).decode("utf-8", "replace")
    import re
    m = re.search(r'numberMatched="(\d+)"', body)
    return int(m.group(1)) if m else -1


def _trunc(c):
    if isinstance(c, (int, float)): return round(c, 5)
    return [_trunc(x) for x in c]


def _normalise(feat):
    g = feat.get("geometry")
    if g and g.get("coordinates"):
        g["coordinates"] = _trunc(g["coordinates"])
    p = feat.get("properties") or {}
    # Match the UPPERCASE keys used by the existing flood/heritage/bushfire
    # static overlays so the same front-end popup template works.
    feat["properties"] = {
        "SCHEME_CODE":       p.get("scheme_code"),
        "ZONE_CODE":         p.get("zone_code"),
        "ZONE_DESCRIPTION":  p.get("zone_description"),
        "LGA":               p.get("lga"),
    }
    feat.pop("bbox", None); feat.pop("id", None); feat.pop("geometry_name", None)
    return feat


def _download_scheme(scheme: str, label: str):
    expected = _hits(scheme)
    print(f"\n=== {scheme}  {label}  (expected {expected:,} features) ===", flush=True)
    if expected == 0:
        print(f"   nothing to fetch")
        return
    out = os.path.join(OUT_DIR, f"vic_{scheme.lower()}.geojson")
    feats = []
    start = 0
    cql = f"scheme_code='{scheme}' AND BBOX(geom,{W},{S},{E},{N},'EPSG:4326')"
    t0 = time.time()
    while True:
        time.sleep(DELAY)
        u = WFS + "?" + urllib.parse.urlencode({
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeName": LAYER, "outputFormat": "application/json",
            "srsName": "EPSG:4326", "count": str(PAGE), "startIndex": str(start),
            "CQL_FILTER": cql,
        })
        body = _req(u)
        d = json.loads(body)
        batch = d.get("features") or []
        if not batch:
            break
        for f in batch:
            feats.append(_normalise(f))
        start += len(batch)
        pct = (start / max(expected, 1)) * 100
        print(f"   +{len(batch):4} (cum {start:7}/{expected})  {pct:5.1f}%", flush=True)
        if len(batch) < PAGE:
            break

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats},
                  f, separators=(",", ":"))
    size_kb = os.path.getsize(out) / 1024
    print(f"   wrote {out}  ({size_kb:.1f} KB, {len(feats)} features, {time.time()-t0:.1f}s)")


def main():
    for code, label in SCHEMES:
        try:
            _download_scheme(code, label)
        except Exception as ex:
            # One scheme failing shouldn't kill the whole batch; the others
            # are still useful even without (say) SMO which has very few
            # features in metro Melbourne.
            print(f"  !! {code} failed: {ex}", flush=True)


if __name__ == "__main__":
    main()

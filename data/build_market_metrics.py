"""Build static/overlays/market_metrics_suburb.geojson from the CoreLogic
"Mapping the Market AU v5" public ArcGIS Feature Service.

Item:    b065f247b2e847f78b679bc0ab0b8c7c  (public)
Service: https://services5.arcgis.com/3aAQWTaa8MLDwdT0/arcgis/rest/services/Mapping_the_Market_AU_v5/FeatureServer
Featured in: "Mapping the Market AU - May 2026" story map.

Each suburb polygon has:
  avm_median  — current median sale price ($)
  hvi_12      — Home Value Index growth, last 12 months (%)
  rent_cur    — current median weekly rent ($)
  rent_1y     — rent growth, last 12 months (%)

We query layer 0 (Houses) bbox-clipped to Greater Melbourne + Greater Perth,
download as GeoJSON, and emit a slim overlay file.
"""
import json, os, urllib.parse, urllib.request, time

BASE = ("https://services5.arcgis.com/3aAQWTaa8MLDwdT0/arcgis/rest/services/"
        "Mapping_the_Market_AU_v5/FeatureServer/0/query")
OUT = "static/overlays/market_metrics_suburb.geojson"

REGIONS = [
    ("Melbourne", 144.45, -38.55, 145.65, -37.40, "VIC"),
    ("Perth",     115.40, -32.70, 116.40, -31.40, "WA"),
    ("Brisbane",  152.55, -28.20, 153.55, -26.90, "QLD"),
    ("Adelaide",  138.30, -35.55, 139.10, -34.40, "SA"),
    ("Darwin",    130.60, -12.85, 131.20, -12.20, "NT"),
]

def query(W, S, E, N, offset=0, limit=2000):
    params = {
        "where":            "property_type = 'Houses' AND avm_median IS NOT NULL",
        "geometry":         json.dumps({"xmin": W, "ymin": S, "xmax": E, "ymax": N,
                                        "spatialReference": {"wkid": 4326}}),
        "geometryType":     "esriGeometryEnvelope",
        "spatialRel":       "esriSpatialRelIntersects",
        "outFields":        "suburb,state,avm_median,hvi_12,rent_cur,rent_1y",
        "returnGeometry":   "true",
        "outSR":            4326,
        "f":                "geojson",
        "resultOffset":     str(offset),
        "resultRecordCount": str(limit),
        "geometryPrecision": "5",
        "maxAllowableOffset": "0.0003",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.loads(r.read())

all_feats = []
for region, W, S, E, N, state_code in REGIONS:
    print(f"\n=== {region} bbox ({state_code}) ===")
    offset = 0
    region_feats = []
    while True:
        d = query(W, S, E, N, offset=offset)
        feats = d.get("features", [])
        if not feats: break
        region_feats.extend(feats)
        print(f"  offset={offset:5d}  +{len(feats)} (cum {len(region_feats)})")
        if not d.get("exceededTransferLimit"): break
        offset += len(feats); time.sleep(1)
    # Drop any feature whose suburb state doesn't match the metro we expect,
    # in case the bbox catches stray cross-border suburbs.
    region_feats = [f for f in region_feats
                    if (f.get('properties') or {}).get('state') == state_code]
    # CoreLogic stores hvi_3 / hvi_12 / rent_1y as fractions (0.05 = 5%).
    # Multiply by 100 so the file stores plain percentages (5 = 5%) which
    # matches how the frontend renders them.
    for f in region_feats:
        props = f.setdefault('properties', {})
        props['src'] = 'CoreLogic-MtM-v5'
        for k in ('hvi_12','rent_1y'):
            v = props.get(k)
            if isinstance(v, (int, float)):
                props[k] = round(v * 100, 2)
    print(f"  Houses (Mapping the Market) features: {len(region_feats)}")
    all_feats.extend(region_feats)

# Truncate coords to 5dp
def trunc(c):
    if isinstance(c, (int, float)): return round(c, 5)
    return [trunc(x) for x in c]
for f in all_feats:
    if f.get('geometry'):
        f['geometry']['coordinates'] = trunc(f['geometry']['coordinates'])

os.makedirs('static/overlays', exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump({"type": "FeatureCollection", "features": all_feats},
              f, separators=(',', ':'))
print(f"\n-> {OUT}: {os.path.getsize(OUT)/1024:.1f} KB, {len(all_feats)} features")

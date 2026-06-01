"""Property value-factor engine.

Given a (lat, lng) it returns the list of government / proximity factors that
affect that property's value — overlays it sits inside (bushfire, flood,
heritage, PAO, …), how close it is to value-changing features (major roads,
rail/tram/bus routes, power lines, parks, hospitals, rivers, nuisances), the
planning zone, and easement complexity.

Speed: each layer is loaded once into a shapely STRtree (lazy, cached in module
globals). Geometries are pre-scaled on the X axis by cos(mean_lat) so plain
Euclidean distance in the index space × 111320 ≈ metres (Melbourne spans ~1°,
so the error from the constant scale is well under 1 %). After warm-up a single
compute is sub-millisecond. Results are cached per rounded (lat,lng) in
data/factors.db so repeat clicks and restarts are instant.

Public:
    compute_factors(lat, lng, use_cache=True) -> dict
    warm_up()        # build every index now (call from a background thread)
"""
import gzip, json, os, math, sqlite3, threading, time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVL  = os.path.join(_BASE, "static", "overlays")
_TRN  = os.path.join(_BASE, "static", "transport")
_FACT_DB = os.path.join(_BASE, "data", "factors.db")
_SPATIAL_DB = os.path.join(_BASE, "data", "spatial.db")

_MEAN_LAT = -37.9
_KX = math.cos(math.radians(_MEAN_LAT))   # x-scale so degrees→~isotropic
_DEG_M = 111320.0                          # metres per degree latitude

_lock = threading.Lock()
_layers = {}        # name -> {"tree": STRtree, "props": {id(geom): props}}
_loaded = set()


# ── layer definitions ────────────────────────────────────────────────────────
# kind: 'poly'  → point-in-polygon (overlay you sit inside)
#       'line'  → nearest distance to a line
#       'point' → nearest distance to a point
_POLY_OVERLAYS = {
    # name (file basename) : (factor label, severity)
    "bushfire":  ("Bushfire Management Overlay (BMO)", "risk"),
    "flood":     ("Flood overlay (LSIO)",              "risk"),
    "vic_fo":    ("Floodway (FO)",                     "risk"),
    "heritage":  ("Heritage Overlay (HO)",             "risk"),
    "vic_pao":   ("Public Acquisition (PAO)",          "risk"),
    "vic_eao":   ("Environmental Audit / contamination (EAO)", "risk"),
    "vic_sbo":   ("Special Building / overland flow (SBO)",    "risk"),
    "vic_emo":   ("Erosion Management (EMO)",          "risk"),
    "vic_smo":   ("Salinity Management (SMO)",         "info"),
    "vic_ddo":   ("Design & Development control (DDO)","info"),
    "vic_dpo":   ("Development Plan required (DPO)",   "info"),
    "vic_dcpo":  ("Dev Contributions payable (DCPO)",  "info"),
    "vic_vpo":   ("Vegetation Protection (VPO)",       "info"),
    "vic_slo":   ("Significant Landscape (SLO)",       "info"),
    "vic_eso":   ("Environmental Significance (ESO)",  "info"),
}


def _read_geojson(basename):
    gz = os.path.join(_OVL, f"{basename}.geojson.gz")
    raw = os.path.join(_OVL, f"{basename}.geojson")
    traw = os.path.join(_TRN, f"{basename}.geojson")
    if os.path.exists(gz):
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            return json.load(f)
    for p in (raw, traw):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return None


def _scale_coords(c):
    if isinstance(c, (int, float)):
        return c
    if c and isinstance(c[0], (int, float)):
        return [c[0] * _KX, c[1]]          # (lng*KX, lat)
    return [_scale_coords(x) for x in c]


def _load(name, basename=None):
    """Lazy-load one layer into an STRtree of metric-scaled geometries."""
    if name in _loaded:
        return _layers.get(name)
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    basename = basename or name
    gj = _read_geojson(basename)
    if not gj:
        _loaded.add(name)
        _layers[name] = None
        return None
    geoms, props = [], {}
    for feat in gj.get("features", []):
        g = feat.get("geometry")
        if not g or not g.get("coordinates"):
            continue
        try:
            g2 = dict(g)
            g2["coordinates"] = _scale_coords(g["coordinates"])
            sg = shape(g2)
            if sg.is_empty:
                continue
        except Exception:
            continue
        geoms.append(sg)
        props[id(sg)] = feat.get("properties") or {}
    tree = STRtree(geoms) if geoms else None
    _layers[name] = {"tree": tree, "props": props} if tree else None
    _loaded.add(name)
    return _layers[name]


def _pt(lat, lng):
    from shapely.geometry import Point
    return Point(lng * _KX, lat)


def _nearest_m(name, basename, P):
    """Return (distance_metres, props) to nearest feature in a layer, or None.

    Uses a progressive bbox query (buffer rings ~0.5/2/6/16 km) instead of a
    full-tree nearest() — bounds the candidate set to features actually near
    the point, which is ~10× faster on the big line layers (roads/rivers)."""
    layer = _load(name, basename)
    if not layer or not layer["tree"]:
        return None
    tree = layer["tree"]
    for r in (0.0045, 0.018, 0.054, 0.14):   # scaled-degree radii ≈ 0.5/2/6/16 km
        try:
            cand = tree.query(P.buffer(r))
        except Exception:
            cand = []
        if cand:
            best_d = None; best_g = None
            for g in cand:
                d = P.distance(g)
                if best_d is None or d < best_d:
                    best_d = d; best_g = g
            return (best_d * _DEG_M, layer["props"].get(id(best_g)) or {})
    # Nothing within ~16 km — fall back to a single full-tree nearest.
    try:
        g = tree.nearest(P)
    except Exception:
        return None
    if g is None:
        return None
    return (P.distance(g) * _DEG_M, layer["props"].get(id(g)) or {})


def _inside(name, basename, P):
    """Return the props of the first polygon containing P, or None."""
    layer = _load(name, basename)
    if not layer or not layer["tree"]:
        return None
    for g in layer["tree"].query(P):
        try:
            if g.contains(P) or g.intersects(P):
                return layer["props"].get(id(g)) or {}
        except Exception:
            continue
    return None


_ease_idx = None        # cached rtree handle (opening it per call costs ~1.5 s)
_ease_idx_tried = False


def _easement_idx():
    global _ease_idx, _ease_idx_tried
    if _ease_idx_tried:
        return _ease_idx
    _ease_idx_tried = True
    base = os.path.join(_BASE, "data", "easements")
    if not (os.path.exists(base + ".idx") and os.path.exists(base + ".dat")):
        return None
    try:
        import rtree
        p = rtree.index.Property(); p.dimension = 2
        p.dat_extension = "dat"; p.idx_extension = "idx"
        _ease_idx = rtree.index.Index(base, properties=p)
    except Exception:
        _ease_idx = None
    return _ease_idx


def _easement_complexity(lat, lng):
    """Count easements within ~20 m of the point using the cached R-tree."""
    idx = _easement_idx()
    if idx is None:
        return None
    d = 0.0002  # ~20 m
    try:
        return idx.count((lng - d, lat - d, lng + d, lat + d))
    except Exception:
        return None


# ── main ─────────────────────────────────────────────────────────────────────

def warm_up():
    """Build every index (call once from a background thread at startup)."""
    with _lock:
        for nm in _POLY_OVERLAYS:
            _load(nm)
        for nm, base in (("roads", "roads"), ("rivers", "rivers"),
                         ("powerlines", "osm_powerlines"),
                         ("nuisance", "osm_nuisance"), ("parks", "osm_parks"),
                         ("amenities", "osm_amenities"), ("airports", "airports"),
                         ("zones", "zones"), ("trains", "trains"),
                         ("trams", "trams"), ("buses", "buses")):
            _load(nm, base)
        _easement_idx()   # open + cache the easement R-tree handle too


def _factors_db():
    os.makedirs(os.path.dirname(_FACT_DB), exist_ok=True)
    con = sqlite3.connect(_FACT_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS factors (
        key TEXT PRIMARY KEY, json TEXT, ts TEXT)""")
    return con


def compute_factors(lat, lng, use_cache=True):
    lat = float(lat); lng = float(lng)
    key = f"{lat:.5f},{lng:.5f}"
    if use_cache:
        try:
            con = _factors_db()
            row = con.execute("SELECT json FROM factors WHERE key=?", (key,)).fetchone()
            con.close()
            if row:
                return json.loads(row[0])
        except Exception:
            pass

    with _lock:
        P = _pt(lat, lng)
        risks, positives, info = [], [], []

        # 1. Overlays you sit inside
        for nm, (label, sev) in _POLY_OVERLAYS.items():
            pr = _inside(nm, nm, P)
            if pr is not None:
                code = pr.get("ZONE_CODE") or pr.get("zone_code") or ""
                detail = f" — {code}" if code else ""
                item = {"label": label + detail, "detail": pr.get("ZONE_DESCRIPTION")
                        or pr.get("zone_description") or ""}
                (risks if sev == "risk" else info).append(item)

        # 2. Planning zone (informational — development potential)
        zp = _inside("zones", "zones", P)
        if zp:
            zc = zp.get("zone_code") or ""
            info.insert(0, {"label": f"Zone: {zc}", "detail": zp.get("desc") or zp.get("zone_description") or ""})

        # 3. Nearest major road (class<=3). On it = bad; nearby arterial or
        #    freeway = traffic/noise (flagged out to a wider radius for
        #    freeways, which carry noise much further).
        nr = _nearest_m("roads", "roads", P)
        if nr:
            dist, pr = nr
            cls = pr.get("class"); nm = pr.get("name") or "major road"
            clslabel = {1: "freeway", 2: "highway/arterial", 3: "sub-arterial"}.get(cls, "major road")
            if dist < 40:
                risks.append({"label": f"Fronts/abuts a {clslabel}", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls == 1 and dist < 500:
                risks.append({"label": "Close to a freeway (noise)", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls <= 2 and dist < 150:
                risks.append({"label": "Close to a main road (traffic/noise)", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls == 3 and dist < 120:
                risks.append({"label": "Close to a sub-arterial road", "detail": f"{nm} · ~{dist:.0f} m"})
            else:
                info.append({"label": f"Nearest {clslabel}", "detail": f"{nm} · ~{dist:.0f} m"})

        # 4. Rail / tram / bus route proximity (on-street route = noise/traffic)
        for nm, base, near_m, lab in (("trains", "trains", 60, "rail line"),
                                      ("trams", "trams", 25, "tram route"),
                                      ("buses", "buses", 20, "bus route")):
            res = _nearest_m(nm, base, P)
            if res:
                dist, _ = res
                if dist < near_m:
                    risks.append({"label": f"On/adjacent to a {lab}", "detail": f"~{dist:.0f} m (traffic/noise)"})
                elif nm == "trains" and dist < 1200:
                    positives.append({"label": "Walk to train line", "detail": f"~{dist:.0f} m"})

        # 5. Power lines
        res = _nearest_m("powerlines", "osm_powerlines", P)
        if res:
            dist, pr = res
            if dist < 120:
                risks.append({"label": "Near high-voltage power line", "detail": f"~{dist:.0f} m" + (f" · {pr.get('voltage')} V" if pr.get("voltage") else "")})

        # 6. Nuisance sites (industrial / landfill / cemetery / etc.)
        res = _nearest_m("nuisance", "osm_nuisance", P)
        if res:
            dist, pr = res
            cat = pr.get("cat") or "nuisance"
            if dist < 300:
                risks.append({"label": f"Near {cat} site", "detail": f"{pr.get('name') or cat} · ~{dist:.0f} m"})

        # 7. Park / reserve — distance is now to the park BOUNDARY (polygons),
        #    so "faces / adjacent" is accurate. Facing a reserve can reduce
        #    value (privacy, anti-social use, parking, after-dark activity).
        res = _nearest_m("parks", "osm_parks", P)
        if res:
            dist, pr = res
            if dist < 45:
                risks.append({"label": "Faces / adjacent to park or reserve", "detail": f"{pr.get('name') or 'reserve'} · ~{dist:.0f} m"})
            elif dist < 700:
                positives.append({"label": "Walk to park", "detail": f"{pr.get('name') or 'park'} · ~{dist:.0f} m"})

        # 8. Rivers / creeks
        res = _nearest_m("rivers", "rivers", P)
        if res:
            dist, pr = res
            if dist < 40:
                risks.append({"label": "Abuts a watercourse (flood/erosion)", "detail": f"{pr.get('name') or 'creek'} · ~{dist:.0f} m"})

        # 9. Amenities — hospital traffic (neg if very close) else positive
        res = _nearest_m("amenities", "osm_amenities", P)
        if res:
            dist, pr = res
            cat = pr.get("cat"); nm = pr.get("name") or cat or "amenity"
            if cat == "hospital" and dist < 200:
                risks.append({"label": "Adjacent to hospital (traffic)", "detail": f"{nm} · ~{dist:.0f} m"})
            elif dist < 1500:
                positives.append({"label": f"Near {cat or 'amenity'}", "detail": f"{nm} · ~{dist:.0f} m"})

        # 10. Airport (aircraft noise)
        res = _nearest_m("airports", "airports", P)
        if res:
            dist, pr = res
            if dist < 3000:
                risks.append({"label": "Near airport (aircraft noise)", "detail": f"{pr.get('name') or 'airport'} · ~{dist/1000:.1f} km"})

        # 11. Easement complexity
        ne = _easement_complexity(lat, lng)
        if ne is not None and ne > 0:
            if ne >= 3:
                risks.append({"label": "Complex easements on/near title", "detail": f"{ne} easement segments within ~20 m"})
            else:
                info.append({"label": "Easement on/near title", "detail": f"{ne} segment(s) within ~20 m"})

    result = {
        "lat": lat, "lng": lng,
        "risk_count": len(risks),
        "risks": risks, "positives": positives, "info": info,
    }
    try:
        con = _factors_db()
        con.execute("INSERT OR REPLACE INTO factors (key, json, ts) VALUES (?,?,?)",
                    (key, json.dumps(result, separators=(",", ":")),
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        con.commit(); con.close()
    except Exception:
        pass
    return result

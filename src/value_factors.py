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


def _nearest_amenity_by_cat(P, cats=("hospital", "supermarket", "shopping",
                                     "university", "library",
                                     "school", "college", "kindergarten",
                                     "childcare")):
    """Return {cat: (distance_m, name)} for the nearest amenity of each
    requested category. The amenities layer is mixed categories, so we scan
    candidates in growing buffer rings and keep the closest per category —
    much more useful than the single global-nearest (which is usually a
    church). 'worship' / 'community' are deliberately not value drivers."""
    layer = _load("amenities", "osm_amenities")
    if not layer or not layer["tree"]:
        return {}
    tree = layer["tree"]
    want = set(cats)
    best = {}   # cat -> (dist_m, name)
    seen = set()
    for r in (0.009, 0.018, 0.03):   # ~1 / 2 / 3.3 km scaled-degree rings
        try:
            cand = tree.query(P.buffer(r))
        except Exception:
            cand = []
        for g in cand:
            gid = id(g)
            if gid in seen:
                continue
            seen.add(gid)
            pr = layer["props"].get(gid) or {}
            cat = pr.get("cat")
            if cat not in want:
                continue
            d = P.distance(g) * _DEG_M
            cur = best.get(cat)
            if cur is None or d < cur[0]:
                best[cat] = (d, pr.get("name") or cat)
        if want.issubset(best.keys()):
            break
    return best


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


_rt_handles = {}        # "easements"/"lots" -> cached rtree.Index (degree space)
_rt_tried = set()


def _rt_idx(name):
    if name in _rt_tried:
        return _rt_handles.get(name)
    _rt_tried.add(name)
    base = os.path.join(_BASE, "data", name)
    if not (os.path.exists(base + ".idx") and os.path.exists(base + ".dat")):
        return None
    try:
        import rtree
        p = rtree.index.Property(); p.dimension = 2
        p.dat_extension = "dat"; p.idx_extension = "idx"
        _rt_handles[name] = rtree.index.Index(base, properties=p)
    except Exception:
        _rt_handles[name] = None
    return _rt_handles.get(name)


def _spatial_geoms(table, ids):
    """Load geometry dicts for the given ids from spatial.db (read-only)."""
    if not ids or not os.path.exists(_SPATIAL_DB):
        return []
    out = []
    try:
        uri = f"file:{os.path.abspath(_SPATIAL_DB)}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        cur = con.cursor()
        for cs in range(0, len(ids), 900):
            chunk = ids[cs:cs + 900]
            ph = ",".join("?" * len(chunk))
            for (geom,) in cur.execute(f"SELECT geom FROM {table} WHERE id IN ({ph})", chunk):
                try:
                    out.append(json.loads(geom))
                except Exception:
                    pass
        con.close()
    except Exception:
        return out
    return out


def _shape_local_m(geomdict, lat0, lng0):
    """Build a shapely geom in LOCAL METRES relative to (lat0,lng0) — so
    lengths/areas/angles are true metres for the easement-vs-lot analysis."""
    from shapely.geometry import shape
    K = _DEG_M
    def tf(c):
        if c and isinstance(c[0], (int, float)):
            return [(c[0] - lng0) * K * _KX, (c[1] - lat0) * K]
        return [tf(x) for x in c]
    g2 = dict(geomdict)
    g2["coordinates"] = tf(geomdict["coordinates"])
    return shape(g2)


def _easement_analysis(lat, lng):
    """Classify easement risk for the lot containing (lat,lng).

    Returns dict: {present, n_segments, lot_found, sides, long_side, crosses}
    where `sides` = how many lot boundaries carry an easement and `long_side`
    = an easement runs along the lot's long boundary (both are the cases that
    most reduce usable/buildable area)."""
    from shapely.geometry import Point, LineString
    res = {"present": False, "n_segments": 0, "lot_found": False,
           "sides": 0, "long_side": False, "crosses": False}
    eidx = _rt_idx("easements")
    if eidx is None:
        return res
    d = 0.00018  # ~18 m bbox for the simple count fallback
    try:
        near_ids = list(eidx.intersection((lng - d, lat - d, lng + d, lat + d)))
    except Exception:
        near_ids = []
    res["n_segments"] = len(near_ids)
    res["present"] = len(near_ids) > 0

    # Find the lot polygon containing the point.
    lidx = _rt_idx("lots")
    if lidx is None:
        return res
    try:
        lot_cands = list(lidx.intersection((lng, lat, lng, lat)))
    except Exception:
        lot_cands = []
    lot_geom = None
    P0 = Point(lng, lat)
    from shapely.geometry import shape as _shape
    for gd in _spatial_geoms("lots", lot_cands):
        try:
            g = _shape(gd)
            if g.contains(P0):
                lot_geom = gd
                break
        except Exception:
            continue
    if lot_geom is None:
        return res
    res["lot_found"] = True

    lot_m = _shape_local_m(lot_geom, lat, lng)
    if lot_m.is_empty:
        return res
    # Easements intersecting the lot's bbox (a touch wider).
    minx, miny, maxx, maxy = None, None, None, None
    try:
        b = lot_m.bounds  # metres
    except Exception:
        return res
    # widen the easement search to the lot bbox in degrees
    lb = _shape(lot_geom).bounds
    pad = 0.00005
    try:
        e_ids = list(eidx.intersection((lb[0]-pad, lb[1]-pad, lb[2]+pad, lb[3]+pad)))
    except Exception:
        e_ids = []
    ease_m = []
    for gd in _spatial_geoms("easements", e_ids):
        try:
            em = _shape_local_m(gd, lat, lng)
            if not em.is_empty:
                ease_m.append(em)
        except Exception:
            continue
    if not ease_m:
        return res

    # Lot oriented bounding box → 4 edges; classify long vs short dimension.
    try:
        mrr = lot_m.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)[:4]
    except Exception:
        return res
    if len(coords) < 4:
        return res
    edges = [LineString([coords[i], coords[(i + 1) % 4]]) for i in range(4)]
    lengths = [e.length for e in edges]
    long_dim = max(lengths)
    long_edges = {i for i, L in enumerate(lengths) if L >= long_dim - 0.5}

    lot_buf = lot_m.buffer(2.0)
    crosses = False
    sides_hit = set()
    for em in ease_m:
        # Does it run along a boundary, or cross the interior?
        for i, e in enumerate(edges):
            try:
                ov = em.intersection(e.buffer(4.0)).length
            except Exception:
                ov = 0
            if ov > max(3.0, 0.25 * lengths[i]):
                sides_hit.add(i)
        # interior crossing: a long piece inside the lot but not near any edge
        try:
            inside = em.intersection(lot_buf)
            near_edge = any(em.distance(e) < 4.0 for e in edges)
            if inside.length > 6.0 and not near_edge:
                crosses = True
        except Exception:
            pass

    res["sides"] = len(sides_hit)
    res["long_side"] = any(i in long_edges for i in sides_hit)
    res["crosses"] = crosses
    return res


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
        _rt_idx("easements")   # open + cache the easement + lot R-tree handles
        _rt_idx("lots")


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
            # Per-class noise radius: freeway 500 m, highway 350 m,
            # arterial 200 m (DEECA class 1 / 2 / 3 respectively).
            if dist < 40:
                risks.append({"label": f"Fronts/abuts a {clslabel}", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls == 1 and dist < 500:
                risks.append({"label": "Close to a freeway (noise)", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls == 2 and dist < 350:
                risks.append({"label": "Close to a highway (traffic/noise)", "detail": f"{nm} · ~{dist:.0f} m"})
            elif cls == 3 and dist < 200:
                risks.append({"label": "Close to an arterial road (traffic/noise)", "detail": f"{nm} · ~{dist:.0f} m"})
            else:
                info.append({"label": f"Nearest {clslabel}", "detail": f"{nm} · ~{dist:.0f} m"})

        # 4. Rail line proximity = train NOISE (one risk band; we only have the
        #    line geometry, not stations, so distance is a noise measure).
        res = _nearest_m("trains", "trains", P)
        if res:
            dist, _ = res
            if dist < 500:
                risks.append({"label": "Near rail line (train noise)", "detail": f"~{dist:.0f} m to track"})
        # Tram / bus routes: only a flag when the property is literally ON the
        # route street (the bus/tram passes the frontage). A route a block away
        # on another street is not a concern; and where the route runs on a
        # major road, the road-proximity factor above already covers it.
        for nm, base, near_m, lab in (("trams", "trams", 30, "tram route"),
                                      ("buses", "buses", 25, "bus route")):
            res = _nearest_m(nm, base, P)
            if res:
                dist, _ = res
                if dist < near_m:
                    risks.append({"label": f"On a {lab}", "detail": f"~{dist:.0f} m (traffic/noise)"})

        # 5. Power lines
        res = _nearest_m("powerlines", "osm_powerlines", P)
        if res:
            dist, pr = res
            if dist < 120:
                risks.append({"label": "Near high-voltage power line", "detail": f"~{dist:.0f} m" + (f" · {pr.get('voltage')} V" if pr.get("voltage") else "")})

        # 6. Nuisance sites (industrial / landfill / cemetery / fuel / etc.)
        res = _nearest_m("nuisance", "osm_nuisance", P)
        if res:
            dist, pr = res
            cat = pr.get("cat") or "nuisance"
            NLAB = {"industrial": "industrial land", "wastewater": "wastewater plant",
                    "landfill": "landfill", "cemetery": "cemetery", "prison": "prison",
                    "quarry": "quarry", "fuel": "petrol station"}
            # Fuel stations are small — only flag when genuinely close.
            thresh = 150 if cat == "fuel" else 300
            if dist < thresh:
                risks.append({"label": f"Near {NLAB.get(cat, cat)}", "detail": f"{pr.get('name') or NLAB.get(cat, cat)} · ~{dist:.0f} m"})

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

        # 9. Amenities — report the nearest of each USEFUL category (not just
        #    the single closest POI, which was usually a church). Hospital is
        #    special: very close = traffic risk, otherwise a convenience plus.
        amen = _nearest_amenity_by_cat(P)
        h = amen.get("hospital")
        if h and h[0] < 200:
            risks.append({"label": "Adjacent to hospital (traffic)", "detail": f"{h[1]} · ~{h[0]:.0f} m"})
        elif h and h[0] < 2000:
            positives.append({"label": "Near hospital", "detail": f"{h[1]} · ~{h[0]:.0f} m"})
        # Schools / preschools / colleges close by = pick-up traffic, parking
        # congestion, noise — a value RISK when very close.
        for cat, label, maxm in (("school",       "school",            250),
                                 ("college",      "college/secondary", 250),
                                 ("kindergarten", "kinder / preschool", 170),
                                 ("childcare",    "childcare centre",   150)):
            v = amen.get(cat)
            if v and v[0] < maxm:
                risks.append({"label": f"Close to a {label} (traffic/parking/noise)",
                              "detail": f"{v[1]} · ~{v[0]:.0f} m"})
        for cat, label, maxm in (("supermarket", "supermarket", 1500),
                                 ("shopping",    "shopping centre", 2000),
                                 ("university",  "university", 2500),
                                 ("library",     "library", 1500)):
            v = amen.get(cat)
            if v and v[0] < maxm:
                positives.append({"label": f"Near {label}", "detail": f"{v[1]} · ~{v[0]:.0f} m"})

        # 10. Airport (aircraft noise)
        res = _nearest_m("airports", "airports", P)
        if res:
            dist, pr = res
            if dist < 3000:
                risks.append({"label": "Near airport (aircraft noise)", "detail": f"{pr.get('name') or 'airport'} · ~{dist/1000:.1f} km"})

        # 11. Easement analysis — risky when on 2+ boundaries or the long side
        #     (those eat the most usable / buildable area).
        ea = _easement_analysis(lat, lng)
        if ea.get("present"):
            if ea.get("sides", 0) >= 2:
                risks.append({"label": "Easements on multiple boundaries",
                              "detail": f"{ea['sides']} sides of the lot" +
                                        (" (incl. the long side)" if ea.get("long_side") else "")})
            elif ea.get("long_side"):
                risks.append({"label": "Easement along the long boundary",
                              "detail": "runs down the long side of the lot"})
            elif ea.get("crosses"):
                risks.append({"label": "Easement crosses the lot interior",
                              "detail": "may block building over part of the land"})
            elif ea.get("sides", 0) == 1:
                info.append({"label": "Easement on one boundary",
                             "detail": "typically low impact (services on a side)"})
            else:
                info.append({"label": "Easement on/near title",
                             "detail": f"{ea.get('n_segments', 0)} segment(s) nearby"})

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

"""
Flask web interface for Property Manager.
Run with: python -m src.web
"""
import os
import re
import json
import time
import glob
import shutil
import hashlib
import sqlite3
import datetime
import threading
import subprocess
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from src import scraper

# Cache for /api/nearby-sales results. Repeat searches return instantly instead
# of re-scraping Domain (50–85s fresh because of anti-bot delays). Persists to
# disk so cache survives server restarts — sold prices don't change once the
# sale closes, so caching forever is safe (just won't include sales added after
# the cache was written).
CACHE_TTL_SEC = 60 * 60 * 24 * 365 * 10  # 10 years ≈ forever
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "sales_cache")
_sales_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_key_hash(key: tuple) -> str:
    return hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()[:16]


def _cache_load() -> None:
    """Populate _sales_cache from disk on startup."""
    if not os.path.isdir(_CACHE_DIR):
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return
    loaded = 0
    for fname in os.listdir(_CACHE_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(_CACHE_DIR, fname), "r", encoding="utf-8") as f:
                entry = json.load(f)
            key = tuple(entry.get("key", []))
            # Re-tuple inner sequences (json doesn't distinguish tuple/list)
            key = key[:8] + (tuple(key[8]),) if len(key) >= 9 else key
            _sales_cache[key] = {"ts": entry["ts"], "ref": entry.get("ref"), "items": entry.get("items", [])}
            loaded += 1
        except Exception as e:
            print(f"[cache] skip {fname}: {e}", flush=True)
    print(f"[cache] loaded {loaded} entries from {_CACHE_DIR}", flush=True)


def _cache_save_one(key: tuple, ref, items) -> None:
    """Persist one cache entry to disk."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, f"{_cache_key_hash(key)}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": list(key), "ts": time.time(), "ref": ref, "items": items}, f, default=str)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[cache] save failed: {e}", flush=True)


_cache_load()

# ── Scenarios DB (server-side, shared across all devices) ─────────────────────

_SCEN_DB = os.path.join(os.path.dirname(__file__), "..", "data", "scenarios.db")


def _scen_db():
    """Return a connection to the scenarios SQLite DB (auto-creates table)."""
    os.makedirs(os.path.dirname(_SCEN_DB), exist_ok=True)
    conn = sqlite3.connect(_SCEN_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scenarios (
            name TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)


@app.after_request
def _no_cache(resp):
    """Disable browser caching so template edits show up immediately."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── Optional shared-token gate (set AUTH_TOKEN env var to enable) ─────────────

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()


@app.before_request
def _check_auth():
    """If AUTH_TOKEN is set, require ?token=… on every request (or a cookie)."""
    if not AUTH_TOKEN:
        return None  # auth disabled
    # Allow the gate page itself + static assets through
    if request.path.startswith("/static/") or request.path == "/_gate":
        return None
    supplied = (
        request.args.get("token")
        or request.headers.get("X-Auth-Token")
        or request.cookies.get("pm_token")
    )
    if supplied == AUTH_TOKEN:
        # Refresh cookie so the user doesn't need ?token=… on every link.
        # Preserve the rest of the query string on the redirect, otherwise any
        # API call like /api/easements?token=…&w=…&s=… loses its real params.
        if request.cookies.get("pm_token") != AUTH_TOKEN:
            other = {k: v for k, v in request.args.items() if k != "token"}
            from urllib.parse import urlencode
            target = request.path + (("?" + urlencode(other)) if other else "")
            resp = app.make_response(("", 302, {"Location": target}))
            resp.set_cookie("pm_token", AUTH_TOKEN, max_age=60 * 60 * 24 * 30,
                            httponly=True, samesite="Lax")
            return resp
        return None
    # Show a tiny gate page
    return Response(
        "<html><body style='font-family:sans-serif;max-width:400px;margin:80px auto;'>"
        "<h2>🔒 Property Manager</h2>"
        "<form method='get' action='/_gate'>"
        "<p>Access token:</p>"
        "<input name='token' type='password' style='width:100%;padding:8px;font-size:16px;'>"
        "<button type='submit' style='margin-top:10px;padding:8px 16px;'>Unlock</button>"
        "</form></body></html>",
        status=401, mimetype="text/html"
    )


@app.route("/_gate")
def _gate():
    """Token-submission landing — sets cookie then redirects to /."""
    token = request.args.get("token", "")
    if token == AUTH_TOKEN:
        resp = app.make_response(("", 302, {"Location": "/"}))
        resp.set_cookie("pm_token", token, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="Lax")
        return resp
    return Response("Bad token", status=401)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = Response(render_template("index.html"))
    # Always serve fresh HTML — never let the browser cache the page shell
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# Serve the huge Vicmap easements / lots GeoJSONs pre-gzipped. Browser decodes
# transparently via Content-Encoding: gzip. Flask's default static route
# would just send the raw .geojson, missing the ~10× smaller .gz right next
# to it. The same helper does easements + lots.
def _serve_gzipped_geojson(name: str, build_script: str):
    base = os.path.join(os.path.dirname(__file__), "..", "static", "overlays")
    gz   = os.path.join(base, f"{name}.geojson.gz")
    raw  = os.path.join(base, f"{name}.geojson")
    if os.path.exists(gz):
        with open(gz, "rb") as f:
            data = f.read()
        resp = Response(data, mimetype="application/geo+json")
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    if os.path.exists(raw):
        return Response(open(raw, "rb").read(), mimetype="application/geo+json",
                        headers={"Cache-Control": "public, max-age=86400"})
    return jsonify({"error": f"File {name}.geojson not built. Run {build_script}."}), 404


@app.route("/static/overlays/easements_vic.geojson")
def static_easements():
    return _serve_gzipped_geojson("easements_vic", "data/build_easements_vic.py")


@app.route("/static/overlays/lots_melb.geojson")
def static_lots():
    return _serve_gzipped_geojson("lots_melb", "data/build_lots_melb.py")


# VicPlan planning-scheme overlay files (PAO, DDO, EAO, VPO, SLO, ESO, DPO,
# SBO, EMO, SMO, DCPO, FO). Pre-gzipped by data/build_vicplan_overlays_melb.py.
@app.route("/static/overlays/vic_<scheme>.geojson")
def static_vicplan_overlay(scheme):
    if not scheme.isalpha() or len(scheme) > 10:
        return jsonify({"error": "bad scheme"}), 400
    return _serve_gzipped_geojson(f"vic_{scheme.lower()}",
                                  "data/build_vicplan_overlays_melb.py")


# Generic overlay-file route. Serves ANY static/overlays/<name>.geojson,
# transparently preferring the pre-gzipped sibling (.geojson.gz) when present.
# This covers the DEECA value layers (zones/roads/rivers/waterbody/airports),
# the OSM feature layers (osm_*), AND the legacy plain files (flood, heritage,
# crime_postcode, …) that used to be served by Flask's default static handler.
@app.route("/static/overlays/<name>.geojson")
def static_overlay_file(name):
    # Filename guard — only simple names, no path traversal.
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
        return jsonify({"error": "bad name"}), 400
    base = os.path.join(os.path.dirname(__file__), "..", "static", "overlays")
    gz  = os.path.join(base, f"{name}.geojson.gz")
    raw = os.path.join(base, f"{name}.geojson")
    if os.path.exists(gz):
        with open(gz, "rb") as f:
            data = f.read()
        resp = Response(data, mimetype="application/geo+json")
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    if os.path.exists(raw):
        return Response(open(raw, "rb").read(), mimetype="application/geo+json",
                        headers={"Cache-Control": "public, max-age=86400"})
    return jsonify({"error": f"{name}.geojson not found"}), 404


# Property value factors — overlays/proximity risks for a single lat/lng.
# Computed by src/value_factors.py against the LOCAL layer files; cached per
# rounded coordinate in data/factors.db. Used by the "⚠ Risk factors" button
# in both the for-sale and sold-property popups.
@app.route("/api/property-factors")
def api_property_factors():
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat,lng required"}), 400
    try:
        from src import value_factors
        return jsonify(value_factors.compute_factors(lat, lng))
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


# Per-parcel rich detail (lot number, plan, LGA) fetched on click from the
# v_parcel_mp WFS layer keyed by PFI. The bulk static file only carries the
# PFI to keep it small; this endpoint fills in the rest on demand.
@app.route("/api/lot-detail")
def api_lot_detail():
    pfi = request.args.get("pfi", "").strip()
    if not pfi:
        return jsonify({"error": "pfi required"}), 400
    import httpx
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "open-data-platform:v_parcel_mp",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": "1",
        # parv_pfi is the field on v_parcel_mp that matches parcel_view.pfi
        # (the PFI we carry in the bulk static file). parcel_pfi is a separate
        # sub-PFI used when one lot has multiple parts.
        "CQL_FILTER": f"parv_pfi='{pfi}'",
    }
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get("https://opendata.maps.vic.gov.au/geoserver/wfs", params=params)
            r.raise_for_status()
            j = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    feats = j.get("features") or []
    if not feats:
        return jsonify({"pfi": pfi}), 200
    p = feats[0].get("properties") or {}
    return jsonify({
        "pfi":          pfi,
        "lot_number":   p.get("parcel_lot_number"),
        "plan_number":  p.get("parcel_plan_number"),
        "spi":          p.get("parcel_spi"),
        "lga_code":     p.get("parcel_lga_code"),
    })


# ── API: address autocomplete via Nominatim (OpenStreetMap) ───────────────────

@app.route("/api/suggest")
def api_suggest():
    """Return address suggestions using OpenStreetMap Nominatim — no API key needed."""
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])
    try:
        import httpx
        url = (
            f"https://nominatim.openstreetmap.org/search"
            f"?q={q}&countrycodes=au&format=json&addressdetails=1&limit=8"
        )
        headers = {"User-Agent": "PropertyManager/1.0 (research tool)"}
        with httpx.Client(headers=headers, timeout=10) as client:
            resp = client.get(url)
            data = resp.json()
        results = []
        for item in data:
            addr = item.get("address", {})
            suburb = (
                addr.get("suburb")
                or addr.get("town")
                or addr.get("city_district")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("quarter")
                or addr.get("neighbourhood")
                or addr.get("city")
                or ""
            )
            # Last resort: pull suburb from display_name before state
            if not suburb:
                display = item.get("display_name", "")
                parts = [p.strip() for p in display.split(",")]
                state_full_local = addr.get("state", "")
                for p in reversed(parts):
                    if p and p not in (state_full_local, "Australia", addr.get("postcode",""), addr.get("country","")):
                        suburb = p
                        break
            state_full = addr.get("state", "")
            state_abbr = _state_abbr(state_full)
            postcode = addr.get("postcode", "")
            results.append({
                "label": item.get("display_name", "").split(", Australia")[0],
                "lat": float(item.get("lat", 0)),
                "lng": float(item.get("lon", 0)),
                "suburb": suburb,
                "state": state_abbr,
                "postcode": postcode,
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: bbox queries against the LOCAL spatial index ────────────────────────
#
# Easements + lots are downloaded ONCE to disk (data/build_easements_vic.py,
# data/build_lots_melb.py) and indexed via data/build_spatial_index.py into:
#   - data/{easements,lots}.idx + .dat  → libspatialindex on-disk R-tree
#   - data/spatial.db                   → SQLite payload store (id, pfi, geom)
#
# Every browser request hits these LOCAL files — nothing leaves the machine
# after the initial bulk download. The wire payload per request is tens of KB
# (just the features in the current viewport), not the full 200 / 780 MB file.

_SPATIAL_DB = os.path.join(os.path.dirname(__file__), "..", "data", "spatial.db")
_RTREE_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_RTREE_CACHE = {}   # layer name -> rtree.index.Index   (lazy, opened on demand)


def _rtree_for(layer: str):
    """Lazy-open the libspatialindex R-tree for a layer. Cached process-wide
    so we don't pay open cost per request. Returns None (uncached) while the
    index is mid-build so the next request retries once the build finishes."""
    idx = _RTREE_CACHE.get(layer)
    if idx is not None:
        return idx
    base = os.path.join(_RTREE_DIR, layer)
    if not (os.path.exists(base + ".idx") and os.path.exists(base + ".dat")):
        return None
    # An empty / unfinished .idx file is the build script's mid-run state —
    # libspatialindex throws InvalidPageException trying to open it. Skip
    # rather than crash; the user gets a 503 + "index building" hint.
    if os.path.getsize(base + ".idx") == 0:
        return None
    try:
        import rtree
    except ImportError:
        return None
    p = rtree.index.Property()
    p.dimension = 2
    p.dat_extension = "dat"
    p.idx_extension = "idx"
    try:
        idx = rtree.index.Index(base, properties=p)
    except Exception:
        # Concurrent writes from build_spatial_index.py can also leave the
        # index in transient invalid states; surface the error to the client
        # instead of erroring out the whole request.
        return None
    _RTREE_CACHE[layer] = idx
    return idx


def _spatial_conn():
    """Read-only SQLite connection. The DB is built once and never written
    to from the web process."""
    uri = f"file:{os.path.abspath(_SPATIAL_DB)}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _bbox_query(layer: str, w: float, s: float, e: float, n: float, cap: int = 10000):
    """Return up to `cap` features from `layer` whose bbox intersects (w,s,e,n)
    as a GeoJSON FeatureCollection (dict)."""
    if layer not in ("easements", "lots"):
        return None
    if not os.path.exists(_SPATIAL_DB):
        return None
    idx = _rtree_for(layer)
    if idx is None:
        return None

    # 1. Spatial filter — R-tree returns the candidate feature IDs in O(log N).
    ids = list(idx.intersection((w, s, e, n)))
    if not ids:
        return {"type": "FeatureCollection", "features": []}
    if len(ids) > cap:
        ids = ids[:cap]

    # 2. Payload lookup — SQLite primary-key fetch is O(1) per id.
    # We chunk because SQLite has a 999-parameter limit by default.
    feats = []
    con = _spatial_conn()
    try:
        cur = con.cursor()
        for chunk_start in range(0, len(ids), 900):
            chunk = ids[chunk_start:chunk_start + 900]
            placeholders = ",".join("?" * len(chunk))
            sql = f"SELECT pfi, status, geom FROM {layer} WHERE id IN ({placeholders})"
            for pfi, status, geom in cur.execute(sql, chunk):
                try:
                    g = json.loads(geom)
                except Exception:
                    continue
                feats.append({
                    "type": "Feature",
                    "geometry": g,
                    "properties": {"pfi": pfi, "status": status},
                })
    finally:
        con.close()
    return {"type": "FeatureCollection", "features": feats}


@app.route("/api/easements")
def api_easements():
    """Easements intersecting the lat/lng bbox (LOCAL DB, no network)."""
    try:
        w = float(request.args.get("w")); s = float(request.args.get("s"))
        e = float(request.args.get("e")); n = float(request.args.get("n"))
    except (TypeError, ValueError):
        return jsonify({"error": "w,s,e,n (WGS84) required"}), 400
    if not (w < e and s < n):
        return jsonify({"error": "invalid bbox"}), 400
    # Refuse implausibly large bboxes — the front-end is already gated on
    # zoom ≥ 13, but a defensive cap stops a single request from pulling
    # half the state into memory if zoom-gating ever breaks.
    if (e - w) > 0.5 or (n - s) > 0.5:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "warning": "bbox too large — zoom in further"}), 200
    fc = _bbox_query("easements", w, s, e, n)
    if fc is None:
        return jsonify({"error": "spatial DB missing. Run data/build_spatial_index.py."}), 503
    return jsonify(fc)


@app.route("/api/lots")
def api_lots():
    """Lot / parcel polygons intersecting the lat/lng bbox (LOCAL DB)."""
    try:
        w = float(request.args.get("w")); s = float(request.args.get("s"))
        e = float(request.args.get("e")); n = float(request.args.get("n"))
    except (TypeError, ValueError):
        return jsonify({"error": "w,s,e,n (WGS84) required"}), 400
    if not (w < e and s < n):
        return jsonify({"error": "invalid bbox"}), 400
    if (e - w) > 0.5 or (n - s) > 0.5:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "warning": "bbox too large — zoom in further"}), 200
    fc = _bbox_query("lots", w, s, e, n)
    if fc is None:
        return jsonify({"error": "spatial DB missing. Run data/build_spatial_index.py."}), 503
    return jsonify(fc)


# Heavy value-context layers (zones/roads/rivers/waterbody/parks) indexed into
# data/veclayers.db + data/<name>.idx by data/build_veclayer_index.py. Same
# bbox-fetch pattern as easements/lots so the map only transfers what's in view.
_VECLAYERS_DB = os.path.join(os.path.dirname(__file__), "..", "data", "veclayers.db")
_VEC_BBOX_LAYERS = {"zones", "roads", "rivers", "waterbody", "parks"}


def _veclayer_bbox_query(layer, w, s, e, n, cap=12000):
    if layer not in _VEC_BBOX_LAYERS or not os.path.exists(_VECLAYERS_DB):
        return None
    idx = _rtree_for(layer)
    if idx is None:
        return None
    ids = list(idx.intersection((w, s, e, n)))
    if not ids:
        return {"type": "FeatureCollection", "features": []}
    if len(ids) > cap:
        ids = ids[:cap]
    feats = []
    uri = f"file:{os.path.abspath(_VECLAYERS_DB)}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        cur = con.cursor()
        for cs in range(0, len(ids), 900):
            chunk = ids[cs:cs + 900]
            ph = ",".join("?" * len(chunk))
            for props, geom in cur.execute(
                    f"SELECT props, geom FROM {layer} WHERE id IN ({ph})", chunk):
                try:
                    feats.append({"type": "Feature",
                                  "geometry": json.loads(geom),
                                  "properties": json.loads(props)})
                except Exception:
                    continue
    finally:
        con.close()
    return {"type": "FeatureCollection", "features": feats}


@app.route("/api/veclayer/<name>")
def api_veclayer(name):
    if name not in _VEC_BBOX_LAYERS:
        return jsonify({"error": "unknown layer"}), 404
    try:
        w = float(request.args.get("w")); s = float(request.args.get("s"))
        e = float(request.args.get("e")); n = float(request.args.get("n"))
    except (TypeError, ValueError):
        return jsonify({"error": "w,s,e,n required"}), 400
    if not (w < e and s < n):
        return jsonify({"error": "invalid bbox"}), 400
    if (e - w) > 0.4 or (n - s) > 0.4:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "warning": "bbox too large — zoom in"}), 200
    fc = _veclayer_bbox_query(name, w, s, e, n)
    if fc is None:
        return jsonify({"error": "veclayers.db missing. Run data/build_veclayer_index.py."}), 503
    return jsonify(fc)


# ── API: fetch reference-property specs from a Domain URL ─────────────

@app.route("/api/listing-info")
def api_listing_info():
    """Scrape a Domain listing URL and return beds/baths/cars/land/address."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        info = scraper.get_listing_features(url)
        if not info:
            return jsonify({"error": "Could not parse listing"}), 404
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: debug — inspect raw __NEXT_DATA__ keys from a sold-listings page ──

@app.route("/api/debug-scrape")
def api_debug_scrape():
    """Debug: fetch a Domain sold-listings page and return the JSON key structure."""
    suburb  = request.args.get("suburb", "Mernda").strip()
    state   = request.args.get("state", "VIC").strip().upper()
    postcode = request.args.get("postcode", "").strip()
    try:
        from src.scraper import _slug, _fetch_page, _extract_next_data, _extract_json_listings
        slug = _slug(suburb, state, postcode)
        url  = f"https://www.domain.com.au/sold-listings/{slug}/?page=1"
        html = _fetch_page(url)
        nd   = _extract_next_data(html)
        # Return key structure without giant data blobs
        def _keys(obj, depth=0):
            if depth > 4 or not isinstance(obj, dict):
                return type(obj).__name__
            return {k: _keys(v, depth+1) for k, v in list(obj.items())[:20]}
        raw = _extract_json_listings(html)
        props_keys = list(nd.get("props", {}).get("pageProps", {}).keys()) if nd else []
        cp_keys    = list(nd.get("props", {}).get("pageProps", {}).get("componentProps", {}).keys()) if nd else []
        return jsonify({
            "url": url,
            "html_len": len(html),
            "html_preview": html[:500],
            "next_data_found": bool(nd),
            "props_pageProps_keys": props_keys,
            "componentProps_keys": cp_keys,
            "raw_listings_count": len(raw),
            "first_listing_keys": list(raw[0].keys()) if raw else [],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── API: lazy-load pool/storey for one comp ─────────────────────────

@app.route("/api/listing-extras")
def api_listing_extras():
    """Return {pool, storeys} for a single comp listing URL.

    Optional lat/lng query params let the server fall back to an OSM
    Overpass query when the listing description doesn't mention a pool.
    """
    url = request.args.get("url", "").strip()
    try:
        lat = float(request.args.get("lat") or 0) or None
        lng = float(request.args.get("lng") or 0) or None
    except ValueError:
        lat = lng = None
    if not url and not (lat and lng):
        return jsonify({"error": "url or lat+lng required"}), 400
    try:
        return jsonify(scraper.get_listing_pool_storeys(url, lat, lng))
    except Exception as e:
        return jsonify({"error": str(e), "pool": None, "storeys": None}), 500


# ── API: similar houses currently for sale (official Domain API) ───────────────

@app.route("/api/similar-listings")
def api_similar_listings():
    """Count active for-sale listings via the official Domain API
    (500 req/day free tier; zero ban risk vs scraping).
    Filters by suburb/state/type + bedroom range around the ref property.
    """
    suburb   = request.args.get("suburb", "").strip()
    state    = request.args.get("state",  "").strip().upper()
    try:
        min_beds = max(int(request.args.get("min_beds", 0)) - 1, 1)
        max_beds = int(request.args.get("max_beds", 0)) + 1 if int(request.args.get("max_beds", 0)) else None
    except ValueError:
        min_beds, max_beds = 1, None
    types_raw = request.args.getlist("types")
    if not suburb or not state:
        return jsonify({"error": "suburb and state required"}), 400
    try:
        from src.api_client import DomainAPIClient
        client = DomainAPIClient()
        listings = client.search_listings(
            suburb=suburb, state=state,
            listing_type="Sale",
            min_beds=min_beds, max_beds=max_beds,
            property_types=types_raw or None,
            page_size=50,
        )
        items = []
        for l in listings:
            items.append({
                "address": getattr(l, 'address', ''),
                "price":   getattr(l, 'price_display', '') or '',
                "beds":    getattr(l, 'bedrooms', None),
                "baths":   getattr(l, 'bathrooms', None),
                "cars":    getattr(l, 'carspaces', None),
                "url":     getattr(l, 'url', ''),
            })
        return jsonify({"count": len(items), "items": items, "suburb": suburb})
    except Exception as e:
        return jsonify({"error": str(e), "count": 0, "items": []}), 200


# ── API: "For Sale" listings — scrape Domain filter URL + cache locally ──────
#
# scrape:   POST /api/for-sale/refresh?url=<full domain filter url>
# load:     GET  /api/for-sale            — return cached listings as GeoJSON
#
# The scrape uses src/scraper_domain_sale.py (same undetected-chromedriver
# infrastructure as the sold-results scraper). Results are upserted into
# data/for_sale.db so the next page load is instant.

_FORSALE_DB = os.path.join(os.path.dirname(__file__), "..", "data", "for_sale.db")


def _forsale_to_geojson(items):
    feats = []
    for d in items:
        if d.get("lat") is None or d.get("lng") is None:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lng"], d["lat"]]},
            "properties": {k: v for k, v in d.items() if k not in ("lat", "lng")},
        })
    return {"type": "FeatureCollection", "features": feats}


@app.route("/api/for-sale", methods=["GET"])
def api_for_sale_load():
    """Return the currently-saved For Sale listings as a GeoJSON FC.
    Each load fills risk_count for up to 60 not-yet-computed listings (cheap —
    factors are cached per coordinate) so markers progressively colour without
    blocking the response for long."""
    from src import scraper_domain_sale as sds
    con = sds.init_db(_FORSALE_DB)
    try:
        try:
            sds.fill_risk_counts(con, limit=60)
        except Exception:
            pass
        items = sds.load_listings(con)
    finally:
        con.close()
    return jsonify(_forsale_to_geojson(items))


@app.route("/api/for-sale/favourite", methods=["POST"])
def api_for_sale_favourite():
    """Mark/unmark a for-sale listing as a favourite (persisted in for_sale.db)."""
    try:
        lid = int(request.args.get("listing_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "listing_id required"}), 400
    fav = request.args.get("fav", "1") not in ("0", "false", "")
    from src import scraper_domain_sale as sds
    con = sds.init_db(_FORSALE_DB)
    try:
        ok = sds.set_favourite(con, lid, fav)
    finally:
        con.close()
    return jsonify({"listing_id": lid, "favourite": fav, "updated": ok})


@app.route("/api/for-sale/refresh", methods=["POST", "GET"])
def api_for_sale_refresh():
    """Scrape the supplied Domain filter URL, upsert results, return the new
    GeoJSON. Accepts ?url= via either GET or POST so the frontend can use a
    plain fetch() without payload juggling."""
    url = (request.args.get("url") or
           (request.json or {}).get("url") if request.is_json else
           request.args.get("url") or "").strip()
    if not url or "domain.com.au" not in url:
        return jsonify({"error": "url must be a domain.com.au filter URL"}), 400
    max_pages = int(request.args.get("max_pages", 20) or 20)
    from src import scraper_domain_sale as sds
    scrape_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        listings = sds.scrape_filter_url(url, max_pages=max_pages)
    except Exception as ex:
        return jsonify({"error": f"scrape failed: {ex}"}), 502
    con = sds.init_db(_FORSALE_DB)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        ins, upd = sds.save_listings(con, listings)
        # Listings not seen this scrape went sold/under-offer/withdrawn. Two
        # stages: just-disappeared → flag off-market + show once as "updated";
        # already-off-market → remove. Guarded so a partial scrape can't wipe
        # the map.
        newly_off, hidden = sds.process_absent(con, scrape_start, now_iso) if len(listings) >= 30 else (0, 0)
        # Compute value-risk counts for every listing (cached per coordinate;
        # the scrape already took 30-90 s so this one-time pass is unnoticed).
        try:
            sds.fill_risk_counts(con)
        except Exception:
            pass
        items = sds.load_listings(con)
    finally:
        con.close()
    fc = _forsale_to_geojson(items)
    fc["meta"] = {
        "scraped":  len(listings),
        "inserted": ins,
        "updated":  upd,
        "off_market": newly_off,
        "removed":  hidden,
        "total_in_db": len(items),
    }
    return jsonify(fc)


# ── API: nearby sold properties (SSE streaming) ────────────────────────────────

@app.route("/api/nearby-sales")
def api_nearby_sales():
    """Stream nearby sold properties as SSE events.
    Accepts lat/lng/suburb/state/postcode directly from the address autocomplete.
    """
    lat = float(request.args.get("lat", 0) or 0)
    lng = float(request.args.get("lng", 0) or 0)
    suburb = request.args.get("suburb", "").strip()
    state = request.args.get("state", "").strip().upper()
    postcode = request.args.get("postcode", "").strip()
    address_label = request.args.get("address", "").strip()
    radius = float(request.args.get("radius", 3))
    # If suburb came through blank, try to extract it from the address label
    # (Nominatim sometimes omits it for estate/locality addresses)
    if not suburb and address_label:
        # address label format: "10 Callaway Crescent, Mernda, Melbourne, VIC 3754"
        # suburb is typically the first token after the street
        parts = [p.strip() for p in address_label.split(",")]
        for p in parts[1:]:  # skip house+street
            clean = p.strip()
            if clean and not any(c.isdigit() for c in clean) and clean not in ("Australia",):
                suburb = clean
                break
    print(f"[search] suburb={suburb!r} state={state!r} postcode={postcode!r} lat={lat} lng={lng}", flush=True)
    months = int(request.args.get("months", 6))
    pages = int(request.args.get("pages", 3))
    prop_types = request.args.getlist("types")
    force_refresh = request.args.get("force", "").strip() in ("1", "true", "yes")

    if not lat or not lng or not suburb or not state:
        return jsonify({"error": "lat, lng, suburb and state are required"}), 400

    cache_key = (
        round(lat, 5), round(lng, 5), round(radius, 2),
        months, pages, suburb.lower(), state, postcode,
        tuple(sorted(prop_types)),
    )

    def _results_to_items(results):
        out = []
        for r in results:
            out.append({
                "address": r.address,
                "price": r.price_display or (f"${r.price:,.0f}" if r.price else "Undisclosed"),
                "price_num": r.price,
                "sold_date": r.sold_date or "",
                "type": r.property_type or "",
                "beds": r.bedrooms,
                "baths": r.bathrooms,
                "cars": r.carspaces,
                "land_area": r.land_area,
                "distance_km": r.distance_km,
                "url": r.url or "",
                "lat": r.lat,
                "lng": r.lng,
                "pool": r.pool,
                "storeys": r.storeys,
                "building_m2": getattr(r, 'building_m2', None),
                "slope_pct":   getattr(r, 'slope_pct',   None),
            })
        return out

    def _attach_factor_tiers(items):
        """Fill risk_count / medium_count / tier for each sold comp from the
        value-factor engine (cached per coordinate + version-aware, so cheap
        and never stale). Lets the sold popup show the count up front and the
        adjustment apply a risk discount."""
        try:
            from src import value_factors
        except Exception:
            return items
        for it in items:
            la, lo = it.get("lat"), it.get("lng")
            if la is None or lo is None:
                # Without coordinates we cannot compute factors — default to
                # 0/0 so the popup button shows a definitive label rather than
                # the indeterminate "⚠ Risk factors" placeholder.
                it.setdefault("risk_count", 0)
                it.setdefault("medium_count", 0)
                continue
            try:
                fc = value_factors.compute_factors(la, lo)
                it["risk_count"] = fc.get("risk_count", 0)
                it["medium_count"] = fc.get("medium_count", 0)
                it["tier"] = fc.get("tier")
            except Exception:
                it.setdefault("risk_count", 0)
                it.setdefault("medium_count", 0)
        return items

    def _ref_factors():
        try:
            from src import value_factors
            return value_factors.compute_factors(lat, lng)
        except Exception:
            return None

    def generate():
        yield f"data: {json.dumps({'status': 'searching', 'address': address_label, 'lat': lat, 'lng': lng})}\n\n"

        # ── Cache hit → return as-is (only the explicit Refresh button bypasses) ──
        hit = _sales_cache.get(cache_key)
        if hit and not force_refresh:
            age_min = int((time.time() - hit['ts']) / 60)
            print(f"[cache] HIT for {cache_key[:3]} (age {age_min}m, {len(hit['items'])} items)", flush=True)
            if hit.get('ref'):
                yield f"data: {json.dumps({'status': 'ref_specs', 'ref': hit['ref']})}\n\n"
            _attach_factor_tiers(hit['items'])
            yield f"data: {json.dumps({'status': 'done', 'results': hit['items'], 'ref_address': address_label, 'lat': lat, 'lng': lng, 'cached': True, 'cache_age_min': age_min, 'ref_factors': _ref_factors()})}\n\n"
            return
        if force_refresh:
            print(f"[cache] FORCE refresh for {cache_key[:3]} -- bypassing cache", flush=True)

        # ── Cache miss → run the scraper ─────────────────────────────────
        ref = None
        try:
            ref = scraper.get_property_profile(address_label, suburb, state, postcode)
            if ref:
                yield f"data: {json.dumps({'status': 'ref_specs', 'ref': ref})}\n\n"
        except Exception:
            pass  # non-fatal — user can still adjust manually

        try:
            results = scraper.get_nearby_sales(
                lat, lng,
                radius_km=radius,
                months=months,
                pages=pages,
                suburb=suburb,
                state=state,
                postcode=postcode,
            )
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        # Filter by property type if requested (exact-aware: "House" must NOT match "Townhouse")
        if prop_types:
            results = [r for r in results if _type_matches(r.property_type or "", prop_types)]

        items = _results_to_items(results)

        with _cache_lock:
            _sales_cache[cache_key] = {'ts': time.time(), 'ref': ref, 'items': items}
        _cache_save_one(cache_key, ref, items)
        print(f"[cache] STORE for {cache_key[:3]} ({len(items)} items) -> disk", flush=True)
        _attach_factor_tiers(items)
        yield f"data: {json.dumps({'status': 'done', 'results': items, 'ref_address': address_label, 'lat': lat, 'lng': lng, 'ref_factors': _ref_factors()})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _type_matches(prop_type: str, selected: list) -> bool:
    """Match a Domain property_type against the user's selected chip labels.

    Domain returns values like 'House', 'Townhouse', 'ApartmentUnitFlat',
    'Villa', 'VacantLand', 'Rural'. We need 'House' to exclude 'Townhouse'.
    """
    pt = (prop_type or "").lower().strip()
    if not pt:
        return False
    for sel in selected:
        s = sel.lower().strip()
        if s == "house":
            # exact match on the word 'house' only — not 'townhouse'
            if pt == "house" or pt.startswith("house ") or pt.startswith("semidetached"):
                return True
        elif s == "apartment":
            if any(k in pt for k in ("apartment", "unit", "flat", "studio")):
                return True
        elif s == "townhouse":
            if "townhouse" in pt or "terrace" in pt:
                return True
        elif s == "villa":
            if "villa" in pt:
                return True
        elif s == "land":
            if "land" in pt or "block" in pt:
                return True
        elif s == "rural":
            if "rural" in pt or "acreage" in pt or "farm" in pt:
                return True
    return False


def _state_abbr(state_full: str) -> str:
    """Convert full Australian state name to abbreviation."""
    mapping = {
        "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
        "western australia": "WA", "south australia": "SA", "tasmania": "TAS",
        "australian capital territory": "ACT", "northern territory": "NT",
    }
    return mapping.get(state_full.lower(), state_full.upper()[:3])


# ── Entry point ────────────────────────────────────────────────────────────────


@app.route("/api/scenarios", methods=["GET"])
def scen_list():
    with _scen_db() as conn:
        rows = conn.execute("SELECT name, updated_at FROM scenarios ORDER BY updated_at DESC").fetchall()
    return jsonify([{"name": r[0], "updated_at": r[1]} for r in rows])


@app.route("/api/scenarios", methods=["POST"])
def scen_save():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    data = body.get("data")
    if not name or data is None:
        return jsonify({"error": "name and data required"}), 400
    with _scen_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scenarios (name, data, updated_at) VALUES (?,?,?)",
            (name, json.dumps(data), time.time()),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/scenarios/<path:name>", methods=["GET"])
def scen_get(name):
    with _scen_db() as conn:
        row = conn.execute("SELECT data FROM scenarios WHERE name=?", (name,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"name": name, "data": json.loads(row[0])})


@app.route("/api/scenarios/<path:name>", methods=["DELETE"])
def scen_delete(name):
    with _scen_db() as conn:
        conn.execute("DELETE FROM scenarios WHERE name=?", (name,))
        conn.commit()
    return jsonify({"ok": True})


# ── AI Chat queue (processed server-side by the Claude Code CLI) ─────────────
#
# Mechanism: the front-end POSTs a new request to ai_requests_data.json with
# status="pending". This server then runs the Claude Code CLI in headless print
# mode (`claude -p`) against the repo, captures its reply, and writes
# status="processed" + response back into the same file. The front-end polls
# /api/ai-requests for the response.
#
# Auth: the CLI uses the same subscription login stored in ~/.claude — no API
# key and no extra billing beyond the existing Claude subscription.
#
# NOTE: this replaces the old VS Code extension that pasted a prompt into
# Copilot Chat. That extension must stay uninstalled/disabled, otherwise both
# it and this server would answer the same request (double processing).

_AI_REQ_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "main", "ai_requests_data.json")
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ai_req_lock = threading.Lock()

# How long to let a single request run, and what permission posture Claude uses.
#   bypassPermissions = full autonomy: edits files AND runs terminal commands
#       (restart server, install deps, etc.) with NO approval prompts. This is
#       the default so the chat box is fully hands-off from the browser.
#   acceptEdits = auto-accept file edits only; shell commands are denied.
#   default     = prompt for everything (unusable headless — will just deny).
# Override with the AI_CHAT_PERMISSION env var if you want a safer posture.
# SECURITY: bypass means anything typed in the web chat box can run arbitrary
# commands on this machine. Safe here because the server is localhost-only and
# token-gated, but do not expose it more widely without reconsidering this.
_AI_TIMEOUT_SEC = int(os.environ.get("AI_CHAT_TIMEOUT", "900"))
_AI_PERMISSION = os.environ.get("AI_CHAT_PERMISSION", "bypassPermissions")
_AI_MAX_HISTORY = 6

_AI_SYSTEM_PROMPT = (
    "You are the in-app AI assistant for the Property Manager web app (a Flask + "
    "Leaflet property-analysis tool). Answer the user's request directly. If the "
    "request asks for a code or UI change, implement it fully in this repository. "
    "Format your reply as clean Markdown (use '- ' bullets, '## ' headings, "
    "**bold**, and `code` / fenced code blocks where helpful) because the app "
    "renders your reply as Markdown in a chat bubble."
)


def _resolve_claude_cli():
    """Find the Claude Code CLI executable. Order: explicit env override, then a
    `claude` on PATH, then the binary bundled inside the installed VS Code
    extension (latest version by mtime)."""
    override = os.environ.get("CLAUDE_CLI")
    if override and os.path.exists(override):
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    pattern = os.path.join(
        home, ".vscode", "extensions",
        "anthropic.claude-code-*", "resources", "native-binary", "claude.exe",
    )
    matches = glob.glob(pattern)
    if matches:
        return max(matches, key=os.path.getmtime)
    return None


def _ai_req_read():
    try:
        with open(_AI_REQ_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _ai_req_write(arr):
    os.makedirs(os.path.dirname(_AI_REQ_PATH), exist_ok=True)
    tmp = _AI_REQ_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _AI_REQ_PATH)


def _ai_history_prefix(arr, exclude_id):
    """Build a short conversation-history preamble from recent processed items so
    the assistant has context (mirrors the old extension's behaviour)."""
    done = [i for i in arr
            if i.get("status") == "processed" and i.get("response") and i.get("id") != exclude_id]
    done = done[-_AI_MAX_HISTORY:]
    if not done:
        return ""
    lines = []
    for idx, it in enumerate(done, 1):
        resp = (it.get("response") or "").replace("\n", " ")
        if len(resp) > 300:
            resp = resp[:300] + "..."
        lines.append(f"[{idx}] User: {(it.get('text') or '').strip()}\n    Assistant: {resp}")
    return ("CONVERSATION HISTORY (last %d exchanges — use for context):\n%s\n\n---\n\n"
            % (len(done), "\n".join(lines)))


def _ai_mark(item_id, **fields):
    with _ai_req_lock:
        arr = _ai_req_read()
        for it in arr:
            if it.get("id") == item_id:
                it.update(fields)
                break
        _ai_req_write(arr)


def _process_ai_request(item):
    """Run the Claude Code CLI for one pending request and write the reply back."""
    item_id = item.get("id")
    exe = _resolve_claude_cli()
    if not exe:
        _ai_mark(item_id, status="processed", processor="error",
                 model="error", processedAt=_iso_now(),
                 response="**AI unavailable** — could not locate the Claude Code CLI. "
                          "Set the `CLAUDE_CLI` environment variable to its full path.")
        return

    with _ai_req_lock:
        arr = _ai_req_read()
    prompt = _ai_history_prefix(arr, item_id) + (item.get("text") or "")

    cmd = [
        exe, "-p", prompt,
        "--permission-mode", _AI_PERMISSION,
        "--append-system-prompt", _AI_SYSTEM_PROMPT,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_AI_TIMEOUT_SEC,
        )
        resp = (proc.stdout or "").strip()
        if not resp:
            err = (proc.stderr or "").strip()
            resp = ("**No response from Claude Code.**\n\n```\n" + err + "\n```") if err \
                else "**No response from Claude Code** (empty output)."
    except subprocess.TimeoutExpired:
        resp = f"**Timed out** after {_AI_TIMEOUT_SEC}s. Try a smaller request."
    except Exception as ex:  # pragma: no cover - defensive
        resp = f"**Error running Claude Code:** {ex}"

    _ai_mark(item_id, status="processed", processor="claude-code-cli",
             model="Claude Code (subscription)", processedAt=_iso_now(), response=resp)


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.route("/api/ai-requests", methods=["GET"])
def ai_requests_get():
    with _ai_req_lock:
        return jsonify(_ai_req_read())


@app.route("/api/ai-requests", methods=["POST"])
def ai_requests_put():
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "body must be a JSON array"}), 400
    with _ai_req_lock:
        _ai_req_write(body)
    return jsonify({"ok": True, "count": len(body)})


@app.route("/api/ai-requests/add", methods=["POST"])
def ai_requests_add():
    item = request.get_json(silent=True)
    if not isinstance(item, dict) or not item.get("text"):
        return jsonify({"error": "expected {text:...}"}), 400
    now_ms = int(time.time() * 1000)
    item.setdefault("id", f"req-{now_ms}")
    item.setdefault("status", "pending")
    item.setdefault("createdAt", now_ms)
    with _ai_req_lock:
        arr = _ai_req_read()
        arr.append(item)
        _ai_req_write(arr)
    # Process server-side via the Claude Code CLI (no VS Code / Copilot involved).
    threading.Thread(target=_process_ai_request, args=(item,), daemon=True).start()
    return jsonify({"ok": True, "item": item})


def _warm_factors():
    """Build the value-factor spatial indexes in the background so the first
    property click is instant rather than paying a ~20-40 s one-time load."""
    try:
        from src import value_factors
        t0 = time.time()
        value_factors.warm_up()
        print(f"[factors] value-factor indexes warm in {time.time()-t0:.1f}s", flush=True)
    except Exception as ex:
        print(f"[factors] warm-up failed: {ex}", flush=True)


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "0.0.0.0")           # 0.0.0.0 = LAN + tunnel reachable
    port = int(os.environ.get("PORT", "5000"))
    threading.Thread(target=_warm_factors, daemon=True).start()
    print(f"Property Manager serving on http://{host}:{port}")
    app.run(host=host, debug=False, port=port, threaded=True, use_reloader=False)
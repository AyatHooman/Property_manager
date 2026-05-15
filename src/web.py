"""
Flask web interface for Property Manager.
Run with: python -m src.web
"""
import os
import json
import threading
import httpx
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from src import scraper

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
        # Refresh cookie so the user doesn't need ?token=… on every link
        if request.cookies.get("pm_token") != AUTH_TOKEN:
            resp = app.make_response(("", 302, {"Location": request.path}))
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


# ── API: school zone polygons (proxy to Vic DET ArcGIS) ───────────────────────

# Vic DET ArcGIS FeatureServer — school zones (public, no auth needed)
_SCHOOL_ZONE_URLS = {
    "primary":   "https://services.maps.vic.gov.au/arcgis/rest/services/SCHOOLS/Vicmap_Schools/FeatureServer/1/query",
    "secondary": "https://services.maps.vic.gov.au/arcgis/rest/services/SCHOOLS/Vicmap_Schools/FeatureServer/2/query",
}

@app.route("/api/school-zones")
def api_school_zones():
    """Return GeoJSON polygon(s) for the school zone containing a lat/lng point.
    Query params: lat, lng, type (primary|secondary)
    """
    try:
        lat  = float(request.args.get("lat", 0))
        lng  = float(request.args.get("lng", 0))
        kind = request.args.get("type", "primary").lower()
    except (TypeError, ValueError):
        return jsonify({"error": "invalid params"}), 400

    url = _SCHOOL_ZONE_URLS.get(kind)
    if not url:
        return jsonify({"error": "type must be primary or secondary"}), 400

    params = {
        "geometry":     f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel":   "esriSpatialRelIntersects",
        "inSR":         "4326",
        "outSR":        "4326",
        "outFields":    "SCHOOL_NAME,SCHOOL_TYPE,ZONE_TYPE",
        "returnGeometry": "true",
        "f":            "geojson",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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

    if not lat or not lng or not suburb or not state:
        return jsonify({"error": "lat, lng, suburb and state are required"}), 400

    def generate():
        yield f"data: {json.dumps({'status': 'searching', 'address': address_label, 'lat': lat, 'lng': lng})}\n\n"

        # Try to look up the reference property's specs from Domain's
        # property-profile page so the UI can auto-fill beds/baths/cars/land.
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

        items = []
        for r in results:
            items.append({
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
            })
        yield f"data: {json.dumps({'status': 'done', 'results': items, 'ref_address': address_label, 'lat': lat, 'lng': lng})}\n\n"

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

if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "0.0.0.0")           # 0.0.0.0 = LAN + tunnel reachable
    port = int(os.environ.get("PORT", "5000"))
    print(f"Property Manager serving on http://{host}:{port}")
    app.run(host=host, debug=False, port=port, threaded=True, use_reloader=False)
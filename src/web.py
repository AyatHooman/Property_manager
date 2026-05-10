"""
Flask web interface for Property Manager.
Run with: python -m src.web
"""
import os
import json
import threading
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from src import scraper

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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
                or ""
            )
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
    months = int(request.args.get("months", 6))
    pages = int(request.args.get("pages", 3))
    prop_types = request.args.getlist("types")

    if not lat or not lng or not suburb or not state:
        return jsonify({"error": "lat, lng, suburb and state are required"}), 400

    def generate():
        yield f"data: {json.dumps({'status': 'searching', 'address': address_label, 'lat': lat, 'lng': lng})}\n\n"

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

        # Filter by property type if requested
        if prop_types:
            results = [r for r in results if any(
                pt.lower() in (r.property_type or "").lower() for pt in prop_types
            )]

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
                "distance_km": r.distance_km,
                "url": r.url or "",
            })
        yield f"data: {json.dumps({'status': 'done', 'results': items, 'ref_address': address_label, 'lat': lat, 'lng': lng})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    app.run(debug=False, port=5000, threaded=True, use_reloader=False)
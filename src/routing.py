"""
Routing + nearest-train-station helpers for the map.

Street routing (walk / bike / drive) goes through OpenRouteService (ORS) when an
API key is configured, falling back to the public OSRM demo server for *driving*
when no key is present (the OSRM demo only serves the car profile reliably).

Public-transport routing (mode "transit") is OPTIONAL and only works when an
OpenTripPlanner (OTP) server is reachable. Point env OTP_URL at it, e.g.
    set OTP_URL=http://localhost:8080
OTP needs Java + a Melbourne OSM extract + the PTV GTFS feed, then a one-time
graph build. Until OTP_URL is set, transit requests return {"error": "no_otp"}.

Configuration (put these in .secrets.bat next to the launch scripts):
    set ORS_API_KEY=your-free-key-from-openrouteservice.org
    set OTP_URL=http://localhost:8080      (optional, for public transport)
"""
import os
import json
import math
import threading
import urllib.request

_HERE   = os.path.dirname(__file__)
_TRAINS = os.path.join(_HERE, "..", "static", "transport", "trains.geojson")

ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()
OTP_URL     = os.environ.get("OTP_URL", "").strip().rstrip("/")

# Our short mode keys → ORS profile names.
_ORS_PROFILE = {
    "walk":  "foot-walking",
    "bike":  "cycling-regular",
    "drive": "driving-car",
}
# Our short mode keys → OSRM demo profile names (only "driving" is reliable).
_OSRM_PROFILE = {"drive": "driving", "walk": "foot", "bike": "bike"}

_STATIONS = None                 # lazy cache: list of {name, lat, lng}
_TRIP_CACHE: dict = {}           # (rlat, rlng) -> station_trip result
_TRIP_LOCK = threading.Lock()


# ── train stations ───────────────────────────────────────────────────────────
def _load_stations():
    global _STATIONS
    if _STATIONS is not None:
        return _STATIONS
    out = []
    try:
        with open(_TRAINS, "r", encoding="utf-8") as f:
            fc = json.load(f)
        for ft in fc.get("features", []):
            p = ft.get("properties") or {}
            g = ft.get("geometry") or {}
            if p.get("kind") != "station" or g.get("type") != "Point":
                continue
            lng, lat = g["coordinates"][:2]
            out.append({"name": p.get("name") or "Station", "lat": lat, "lng": lng})
    except Exception as ex:
        print(f"[routing] could not load stations: {ex}", flush=True)
    _STATIONS = out
    print(f"[routing] {len(out)} train stations loaded", flush=True)
    return out


def _haversine_km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def nearest_station(lat, lng):
    best, best_km = None, None
    for s in _load_stations():
        km = _haversine_km(lat, lng, s["lat"], s["lng"])
        if best_km is None or km < best_km:
            best, best_km = s, km
    if not best:
        return None
    return {**best, "straight_km": round(best_km, 2)}


# ── HTTP helper ───────────────────────────────────────────────────────────────
def _http_json(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── street routing engines ────────────────────────────────────────────────────
def _route_ors(frm, to, mode):
    profile = _ORS_PROFILE[mode]
    url = f"https://api.openrouteservice.org/v2/directions/{profile}/geojson"
    body = json.dumps({"coordinates": [[frm[1], frm[0]], [to[1], to[0]]]}).encode()
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/geo+json",
    }
    fc = _http_json(url, data=body, headers=headers)
    feat = (fc.get("features") or [None])[0]
    if not feat:
        raise RuntimeError("no_route")
    summ = (feat.get("properties") or {}).get("summary") or {}
    coords = feat["geometry"]["coordinates"]   # [lng, lat]
    return {
        "distance_m": summ.get("distance"),
        "duration_s": summ.get("duration"),
        "geometry": [[c[1], c[0]] for c in coords],
        "mode": mode, "engine": "ors",
    }


def _route_osrm(frm, to, mode):
    prof = _OSRM_PROFILE.get(mode, "driving")
    url = (f"https://router.project-osrm.org/route/v1/{prof}/"
           f"{frm[1]},{frm[0]};{to[1]},{to[0]}?overview=full&geometries=geojson")
    d = _http_json(url)
    routes = d.get("routes") or []
    if not routes:
        raise RuntimeError("no_route")
    r0 = routes[0]
    coords = r0["geometry"]["coordinates"]
    return {
        "distance_m": r0.get("distance"),
        "duration_s": r0.get("duration"),
        "geometry": [[c[1], c[0]] for c in coords],
        "mode": mode, "engine": "osrm",
    }


# ── OTP public-transport (optional) ───────────────────────────────────────────
def _decode_polyline(s):
    """Decode a Google-encoded polyline (OTP legGeometry) → [[lat, lng], ...]."""
    out, lat, lng, i = [], 0, 0, 0
    while i < len(s):
        for is_lng in (False, True):
            shift, result = 0, 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += d
            else:
                lat += d
        out.append([lat / 1e5, lng / 1e5])
    return out


def _route_otp(frm, to):
    if not OTP_URL:
        raise RuntimeError("no_otp")
    query = {
        "query": (
            "query($fl:Float!,$fo:Float!,$tl:Float!,$to:Float!){"
            "plan(from:{lat:$fl,lon:$fo},to:{lat:$tl,lon:$to},"
            "transportModes:[{mode:TRANSIT},{mode:WALK}],numItineraries:1){"
            "itineraries{duration walkDistance "
            "legs{mode duration distance route{shortName} legGeometry{points}}}}}"
        ),
        "variables": {"fl": frm[0], "fo": frm[1], "tl": to[0], "to": to[1]},
    }
    url = f"{OTP_URL}/otp/routers/default/index/graphql"
    d = _http_json(url, data=json.dumps(query).encode(),
                   headers={"Content-Type": "application/json"})
    its = (((d.get("data") or {}).get("plan") or {}).get("itineraries")) or []
    if not its:
        raise RuntimeError("no_transit_route")
    it = its[0]
    geom, legs = [], []
    for leg in it.get("legs", []):
        geom.extend(_decode_polyline((leg.get("legGeometry") or {}).get("points") or ""))
        legs.append({
            "mode": leg.get("mode"),
            "route": (leg.get("route") or {}).get("shortName"),
            "duration_s": leg.get("duration"),
        })
    return {
        "distance_m": None,
        "duration_s": it.get("duration"),
        "geometry": geom,
        "legs": legs,
        "mode": "transit", "engine": "otp",
    }


# ── public API ────────────────────────────────────────────────────────────────
def route(frm, to, mode):
    """frm/to are (lat, lng). mode in walk|bike|drive|transit. Returns dict or raises."""
    if mode == "transit":
        return _route_otp(frm, to)
    if ORS_API_KEY:
        return _route_ors(frm, to, mode)
    if mode == "drive":          # only mode the keyless OSRM demo serves reliably
        return _route_osrm(frm, to, mode)
    raise RuntimeError("no_key")


def station_trip(lat, lng):
    """Nearest train station + walk/drive/bike summary, cached per ~11 m cell."""
    key = (round(lat, 4), round(lng, 4))
    with _TRIP_LOCK:
        if key in _TRIP_CACHE:
            return _TRIP_CACHE[key]
    st = nearest_station(lat, lng)
    if not st:
        return {"error": "no_station"}
    out = {"station": st, "modes": {}, "has_key": bool(ORS_API_KEY),
           "has_otp": bool(OTP_URL)}
    for mode in ("walk", "drive", "bike"):
        try:
            r = route((lat, lng), (st["lat"], st["lng"]), mode)
            out["modes"][mode] = {"distance_m": r["distance_m"],
                                  "duration_s": r["duration_s"]}
        except Exception as ex:
            out["modes"][mode] = {"error": str(ex)}
    with _TRIP_LOCK:
        _TRIP_CACHE[key] = out
    return out

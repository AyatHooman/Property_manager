"""
Pool + storey detection for AU residential properties.

Two complementary sources because neither is complete on its own:

1. OpenStreetMap Overpass API
   - Reliable for swimming pools when present (60m radius around the
     property point catches most backyard pools tagged as
     ``leisure=swimming_pool``).
   - **Unreliable for storey count**: AU residential buildings almost
     never carry the ``building:levels`` tag. So we ignore OSM for
     storeys.

2. Domain listing-page HTML text
   - Property descriptions almost always state "single storey",
     "two storey", "double storey" or similar — easy to regex.
   - Often mention "swimming pool", "in-ground pool", "plunge pool"
     in the description as well — used as a fallback when OSM
     doesn't show a tagged pool (private back-yard pools are
     sometimes missing from OSM).
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

import httpx

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_HTTP_HEADERS = {"User-Agent": "PropertyManager/1.0 (research)"}


# ── OSM building:levels ─────────────────────────────────────────────────────

_ELEV_URL = "https://api.open-meteo.com/v1/elevation"


def terrain_slope_pct(lat: float, lng: float) -> Optional[float]:
    """Estimate the steepest land slope (%) within ~30 m of the point.

    Samples the elevation API at the centre + 4 cardinal neighbours and
    returns the largest gradient as a percentage (rise / run × 100).
    Zero ban risk — Open-Meteo is a free elevation API for non-commercial use.

    Resolution caveat: the underlying DEM is ~30 m. Small flat lots in
    hilly suburbs may register some slope from a neighbouring hillside;
    big sloping blocks may average out to less than the visual steepness.
    """
    if not lat or not lng:
        return None
    import math
    d_lat = 0.00027  # ~30 m
    d_lng = 0.00034 / max(math.cos(math.radians(lat)), 0.1)
    pts = [
        (lat,           lng),
        (lat + d_lat,   lng),
        (lat - d_lat,   lng),
        (lat,           lng + d_lng),
        (lat,           lng - d_lng),
    ]
    lats = ",".join(f"{p[0]:.6f}" for p in pts)
    lngs = ",".join(f"{p[1]:.6f}" for p in pts)
    try:
        with httpx.Client(headers=_HTTP_HEADERS, timeout=10) as client:
            r = client.get(_ELEV_URL, params={"latitude": lats, "longitude": lngs})
            r.raise_for_status()
            elevs = r.json().get("elevation", [])
        if not elevs or len(elevs) != 5:
            return None
        centre, *nbrs = elevs
        max_diff = max(abs(centre - n) for n in nbrs)
        return round((max_diff / 30.0) * 100, 1)
    except Exception:
        return None


def osm_building_area(lat: float, lng: float, radius_m: int = 25) -> Optional[int]:
    """Return building footprint area in m^2 for the nearest building (zero ban risk).

    Uses Overpass turbo `out geom` so we receive the polygon coordinates
    inline. Calculates planar area via the Shoelace formula on lat/lng
    converted to local metres (good enough for residential lots; ~1m
    precision at this latitude). Returns None on network error or no
    building found.
    """
    if not lat or not lng:
        return None
    query = (
        f"[out:json][timeout:10];"
        f"way[\"building\"](around:{radius_m},{lat},{lng});"
        f"out geom;"
    )
    try:
        with httpx.Client(headers=_HTTP_HEADERS, timeout=12) as client:
            r = client.post(_OVERPASS_URL, data={"data": query})
            r.raise_for_status()
            elements = r.json().get("elements", [])
        import math
        # Pick the largest building (often the house, ignoring sheds)
        best = 0.0
        for e in elements:
            geom = e.get("geometry") or []
            if len(geom) < 4:  # need closed ring
                continue
            # Convert lat/lng to local metres centered on the point
            lat0 = sum(p["lat"] for p in geom) / len(geom)
            cos = math.cos(math.radians(lat0))
            xy = [((p["lon"] - lng) * 111320.0 * cos,
                   (p["lat"] - lat)  * 111320.0) for p in geom]
            # Shoelace
            s = 0.0
            for i in range(len(xy) - 1):
                s += xy[i][0] * xy[i+1][1] - xy[i+1][0] * xy[i][1]
            area = abs(s) / 2
            if area > best: best = area
        return int(best) if best > 0 else None
    except Exception:
        return None


def osm_building_levels(lat: float, lng: float, radius_m: int = 25) -> Optional[int]:
    """Return building:levels for the nearest tagged building (zero ban risk).

    AU residential rarely has this tag — but when it does it's authoritative.
    Returns ``None`` on network error OR no tagged building found nearby.
    """
    if not lat or not lng:
        return None
    query = (
        f"[out:json][timeout:10];"
        f"way[\"building\"][\"building:levels\"](around:{radius_m},{lat},{lng});"
        f"out tags;"
    )
    try:
        with httpx.Client(headers=_HTTP_HEADERS, timeout=12) as client:
            r = client.post(_OVERPASS_URL, data={"data": query})
            r.raise_for_status()
            elements = r.json().get("elements", [])
        for e in elements:
            lvl = e.get("tags", {}).get("building:levels")
            if lvl:
                try:
                    return int(float(lvl))
                except ValueError:
                    continue
        return None
    except Exception:
        return None


# ── OSM pool ────────────────────────────────────────────────────────────────

def osm_has_pool(lat: float, lng: float, radius_m: int = 60) -> Optional[bool]:
    """Return True if OSM has a swimming pool within ``radius_m`` of the point.

    Returns ``None`` on network error so the caller can fall back to a
    different signal (HTML keyword scan).  Returns ``False`` when the
    Overpass query succeeded but no pool was found — the caller should
    treat that as "OSM doesn't know of one" rather than absolute proof
    of absence.
    """
    if not lat or not lng:
        return None
    query = (
        f"[out:json][timeout:10];"
        f"("
        f"way[\"leisure\"=\"swimming_pool\"](around:{radius_m},{lat},{lng});"
        f"node[\"leisure\"=\"swimming_pool\"](around:{radius_m},{lat},{lng});"
        f");out ids;"
    )
    try:
        with httpx.Client(headers=_HTTP_HEADERS, timeout=12) as client:
            r = client.post(_OVERPASS_URL, data={"data": query})
            r.raise_for_status()
            elements = r.json().get("elements", [])
        return len(elements) > 0
    except Exception:
        return None


# ── HTML / description keyword parsing ──────────────────────────────────────

# Single regex for storey detection.  Order matters — match the more
# specific phrases first.  The capture group identifies which side won.
_STOREY_RE = re.compile(
    r"\b("
    r"(?:double|two|two[-\s]?storey|two[-\s]?storeys|two[-\s]?storeyed|"
    r"two[-\s]?level|two[-\s]?levels|"
    r"two[-\s]?story|two[-\s]?stories|"
    r"upstairs|second[-\s]?storey|second[-\s]?floor|"
    r"split[-\s]?level|tri[-\s]?level|three[-\s]?storey|three[-\s]?storeys"
    r")"
    r"|"
    r"(?:single|one|single[-\s]?storey|single[-\s]?storeys|"
    r"single[-\s]?level|single[-\s]?levels|"
    r"single[-\s]?story|one[-\s]?storey|one[-\s]?story|one[-\s]?level|"
    r"ground[-\s]?floor[-\s]?living)"
    r")\b",
    re.IGNORECASE,
)

_DOUBLE_KEYWORDS = re.compile(
    r"\b(double[-\s]?storey|double[-\s]?storeys|two[-\s]?storey|two[-\s]?storeys|"
    r"two[-\s]?storeyed|two[-\s]?level|two[-\s]?levels|two[-\s]?story|two[-\s]?stories|"
    r"second[-\s]?storey|second[-\s]?floor|upstairs|split[-\s]?level|tri[-\s]?level|"
    r"three[-\s]?storey|three[-\s]?storeys)\b",
    re.IGNORECASE,
)

_SINGLE_KEYWORDS = re.compile(
    r"\b(single[-\s]?storey|single[-\s]?storeys|one[-\s]?storey|one[-\s]?story|"
    r"single[-\s]?level|one[-\s]?level|single[-\s]?story|ground[-\s]?floor[-\s]?living)\b",
    re.IGNORECASE,
)

_POOL_KEYWORDS = re.compile(
    r"\b(swimming[-\s]?pool|in[-\s]?ground[-\s]?pool|plunge[-\s]?pool|"
    r"lap[-\s]?pool|salt[-\s]?water[-\s]?pool|solar[-\s]?heated[-\s]?pool|"
    r"sparkling[-\s]?pool|heated[-\s]?pool|magnesium[-\s]?pool|fibreglass[-\s]?pool)\b",
    re.IGNORECASE,
)

# Generic "pool" keyword — only used as a *positive* signal because the
# word "pool" rarely appears in residential listings without referring
# to an actual swimming pool ("car pool" is far too rare to worry about).
_POOL_GENERIC = re.compile(r"\bpool\b", re.IGNORECASE)


def storeys_from_text(text: str) -> Optional[int]:
    """Return 1, 2 or None based on keyword presence in free text."""
    if not text:
        return None
    has_double = bool(_DOUBLE_KEYWORDS.search(text))
    has_single = bool(_SINGLE_KEYWORDS.search(text))
    if has_double and not has_single:
        return 2
    if has_single and not has_double:
        return 1
    if has_double and has_single:
        # "single storey extension to a two storey home" — trust the higher
        return 2
    return None


def pool_from_text(text: str) -> Optional[bool]:
    """Return True if pool keywords appear in text. Never returns False —
    absence of evidence isn't evidence of absence in marketing copy."""
    if not text:
        return None
    if _POOL_KEYWORDS.search(text):
        return True
    if _POOL_GENERIC.search(text):
        return True
    return None


# ── Strip HTML tags so the text scan only hits real prose ──────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    if not html:
        return ""
    no_tags = _TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", no_tags).strip()


def parse_pool_storeys_from_html(html: str) -> Tuple[Optional[bool], Optional[int]]:
    """Extract pool + storeys from a Domain listing page.

    Strategy:
      1. Pull the seller's free-text description out of the embedded
         ``__NEXT_DATA__`` JSON.  Domain stores it under several keys
         (``componentProps.description``, ``rootGraphQuery.listingByIdV2.description``,
         ``layoutProps.description``, etc.) sometimes as a string and
         sometimes as a list of paragraphs.
      2. Run keyword scans on that description.  Falling back to a
         tag-stripped scan of the whole page would generate false
         positives from things like school names ("Pool Heights Primary")
         or marketing chrome.
    """
    text = extract_description(html)
    if not text:
        # Fallback: whole-page text-only scan.  Less precise but better
        # than nothing for unusual page layouts.
        text = strip_html(html)
    return pool_from_text(text), storeys_from_text(text)


def extract_description(html: str) -> str:
    """Pull the seller's description out of Domain's __NEXT_DATA__ JSON."""
    if not html:
        return ""
    import json as _json
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    try:
        data = _json.loads(m.group(1))
    except Exception:
        return ""
    chunks: list = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "description" and v:
                    if isinstance(v, str) and len(v) > 80:
                        chunks.append(v)
                    elif isinstance(v, list):
                        for s in v:
                            if isinstance(s, str) and len(s) > 80:
                                chunks.append(s)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    # De-dup while preserving order
    seen: set = set()
    uniq = []
    for c in chunks:
        key = c[:120]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return "  ".join(uniq)

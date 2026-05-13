"""
Domain.com.au scraper — uses Selenium + Chrome to bypass bot protection,
then extracts listing data from __NEXT_DATA__ JSON embedded in the page.
"""
import json
import re
import time
import math
import random
from datetime import datetime, timedelta
from typing import Optional, List, Any, Tuple
from src.models import PropertyListing, SaleResult

def _get_driver():
    """Return an undetected Chrome driver that bypasses Akamai/Cloudflare bot detection."""
    import os
    import undetected_chromedriver as uc

    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _chrome_bin = os.path.join(_base, "chrome-portable", "chrome-win64", "chrome.exe")
    _driver_bin = os.path.join(_base, "chrome-portable", "chromedriver-win64", "chromedriver.exe")

    options = uc.ChromeOptions()
    # Do NOT pass --headless here; use uc's headless=True param instead
    # (the manual flag is fingerprinted by Akamai; uc patches it differently)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")

    kwargs = dict(options=options, headless=True, use_subprocess=False)
    if os.path.exists(_chrome_bin):
        kwargs["browser_executable_path"] = _chrome_bin
    if os.path.exists(_driver_bin):
        kwargs["driver_executable_path"] = _driver_bin

    driver = uc.Chrome(**kwargs)
    driver.set_page_load_timeout(40)
    return driver


def _slug(suburb: str, state: str, postcode: str = "") -> str:
    """Build domain.com.au suburb slug e.g. richmond-vic-3121"""
    parts = suburb.lower().replace(" ", "-") + "-" + state.lower()
    if postcode:
        parts += "-" + postcode
    return parts


def _fetch_page(url: str) -> str:
    """Fetch a page using Selenium Chrome to bypass bot protection."""
    html, _ = _fetch_page_with_url(url)
    return html


def _fetch_page_with_driver(driver, url: str) -> str:
    """Fetch a page using an existing driver instance (no open/close overhead).

    Returns empty string on crash so the caller can skip the page gracefully.
    Raises RuntimeError if Domain returns an Akamai Access Denied page.
    """
    try:
        driver.get(url)
        time.sleep(random.uniform(4, 7))  # randomised delay to avoid bot detection
        html = driver.page_source
        if len(html) < 2000 and "Access Denied" in html and "edgesuite.net" in html:
            raise RuntimeError(
                "Domain.com.au has blocked this IP address (Akamai bot protection). "
                "Please wait a few minutes and try again, or use a VPN/proxy."
            )
        return html
    except RuntimeError:
        raise
    except Exception:
        return ""


def _fetch_page_with_url(url: str) -> Tuple[str, str]:
    """Fetch a page and return (page_source, final_url) after any redirects."""
    driver = _get_driver()
    try:
        driver.get(url)
        time.sleep(random.uniform(4, 7))
        html = driver.page_source
        if len(html) < 2000 and "Access Denied" in html and "edgesuite.net" in html:
            raise RuntimeError(
                "Domain.com.au has blocked this IP address (Akamai bot protection). "
                "Please wait a few minutes and try again, or use a VPN/proxy."
            )
        return html, driver.current_url
    finally:
        driver.quit()



def _extract_next_data(html: str) -> dict:
    """Extract the __NEXT_DATA__ JSON blob embedded in the page."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return {}


def _extract_json_listings(html: str) -> list:
    """Extract listing data from domain.com.au __NEXT_DATA__ componentProps."""
    data = _extract_next_data(html)
    try:
        props = data["props"]["pageProps"]
        # domain.com.au stores listings under componentProps.listingsMap
        cp = props.get("componentProps", props)
        listings_map = cp.get("listingsMap", {})
        if listings_map:
            return list(listings_map.values())
        # fallback keys
        for key in ["listings", "results", "searchResults"]:
            if key in cp and cp[key]:
                val = cp[key]
                return list(val.values()) if isinstance(val, dict) else val
    except (KeyError, TypeError):
        pass
    return []


def search_listings(
    suburb: str,
    state: str,
    postcode: str = "",
    listing_type: str = "Sale",
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_beds: Optional[int] = None,
    max_beds: Optional[int] = None,
    page: int = 1,
) -> List[PropertyListing]:
    """Scrape property listings from domain.com.au."""
    mode = "sale" if listing_type.lower() == "sale" else "rent"
    slug = _slug(suburb, state, postcode)
    url = f"https://www.domain.com.au/{mode}/{slug}/?page={page}"

    if min_price:
        url += f"&price={min_price}-"
        if max_price:
            url += str(max_price)
    elif max_price:
        url += f"&price=0-{max_price}"

    if min_beds:
        url += f"&bedrooms={min_beds}-any"

    html = _fetch_page(url)
    raw_listings = _extract_json_listings(html)

    results = []
    for item in raw_listings:
        listing = _parse_listing(item, listing_type)
        if listing:
            results.append(listing)

    return results


def _parse_listing(item: Any, listing_type: str = "Sale") -> Optional[PropertyListing]:
    """Parse a raw listing dict from domain.com.au listingsMap into a PropertyListing."""
    try:
        listing_id = item.get("id", 0)
        lm = item.get("listingModel", {})

        address = lm.get("address", {})
        features = lm.get("features", {})

        street = address.get("street", "")
        suburb_val = address.get("suburb", "")
        state_val = address.get("state", "")
        postcode_val = address.get("postcode", "")
        full_address = f"{street}, {suburb_val} {state_val} {postcode_val}".strip(", ")

        url = lm.get("url", "")
        if url and not url.startswith("http"):
            url = "https://www.domain.com.au" + url

        return PropertyListing(
            id=int(listing_id) if listing_id else 0,
            listing_type=listing_type,
            status="Live",
            price=lm.get("price") or lm.get("displaySearchPriceRange") or "Contact Agent",
            address=full_address,
            suburb=suburb_val,
            state=state_val,
            postcode=postcode_val,
            bedrooms=_safe_int(features.get("beds")),
            bathrooms=_safe_int(features.get("baths")),
            carspaces=_safe_int(features.get("parking")),
            property_type=features.get("propertyTypeFormatted") or features.get("propertyType", ""),
            land_area=_safe_float(features.get("landSize")),
            description="",
            url=url,
            agent_name=None,
            agency_name=lm.get("branding", {}).get("agencyName") if lm.get("branding") else None,
            listed_date="",
        )
    except Exception:
        return None


def get_sales_results(suburb: str, state: str, postcode: str) -> List[SaleResult]:
    """Scrape recent sold properties from domain.com.au."""
    slug = _slug(suburb, state, postcode)
    url = f"https://www.domain.com.au/sold-listings/{slug}/"

    html = _fetch_page(url)
    raw_listings = _extract_json_listings(html)

    results = []
    for item in raw_listings:
        result = _parse_sale(item)
        if result:
            results.append(result)
    return results


def _parse_sale(item: Any) -> Optional[SaleResult]:
    try:
        lm = item.get("listingModel", {})
        address = lm.get("address", {})
        features = lm.get("features", {})
        street = address.get("street", "")
        suburb_val = address.get("suburb", "")
        state_val = address.get("state", "")
        postcode_val = address.get("postcode", "")
        full_address = f"{street}, {suburb_val} {state_val} {postcode_val}".strip(", ")
        price_raw = lm.get("price", "")
        return SaleResult(
            property_id=item.get("id"),
            address=full_address,
            suburb=suburb_val,
            state=state_val,
            postcode=postcode_val,
            price=_extract_price_number(str(price_raw)),
            price_display=str(price_raw),
            sold_date=lm.get("soldDate", ""),
            property_type=features.get("propertyTypeFormatted") or features.get("propertyType", ""),
            bedrooms=_safe_int(features.get("beds")),
            bathrooms=_safe_int(features.get("baths")),
            carspaces=_safe_int(features.get("parking")),
            land_area=_safe_float(features.get("landSize") or features.get("landArea")),
            url="https://www.domain.com.au" + lm.get("url", "") if lm.get("url") else "",
        )
    except Exception:
        return None


def get_listing_location(
    listing_id: int, listing_url: Optional[str] = None
) -> Optional[Tuple[float, float, str, str, str, str]]:
    """Return (lat, lng, address, suburb, state, postcode) for a listing.

    Pass listing_url directly (the full domain.com.au URL) to skip the slug
    lookup step, e.g. https://www.domain.com.au/18-765-malvern-road-toorak-vic-3142-2020795806
    """
    # Step 1: get the SEO slug URL
    slug_url = listing_url or _get_listing_slug_url(listing_id)
    if not slug_url:
        return None

    # Step 2: fetch the detail page and extract lat/lng + address from componentProps.map
    html = _fetch_page(slug_url)
    data = _extract_next_data(html)
    try:
        cp = data["props"]["pageProps"]["componentProps"]
        # Verify we landed on the right listing (skip check if url was user-provided)
        if not listing_url:
            cp_id = str(cp.get("id", "") or cp.get("listingId", ""))
            if cp_id and cp_id != str(listing_id):
                return None
        map_data = cp.get("map", {})
        lat = map_data.get("latitude")
        lng = map_data.get("longitude")
        if lat is None or lng is None:
            return None
        addr = cp.get("address", "")
        suburb_val = cp.get("suburb", "")
        state_val = cp.get("stateAbbreviation", "")
        postcode_val = cp.get("postcode", "")
        return float(lat), float(lng), addr, suburb_val, state_val.upper(), postcode_val
    except (KeyError, TypeError):
        return None


def get_listing_features(url: str) -> Optional[dict]:
    """Fetch a Domain listing detail page and extract beds/baths/cars/land/address.

    Returns dict with keys: address, suburb, state, postcode, beds, baths, cars,
    land_area, property_type, lat, lng, pool, storeys.  Missing fields are None.
    """
    if not url or "domain.com.au" not in url:
        return None
    html = _fetch_page(url)
    return _parse_features_from_html(html)


def get_listing_pool_storeys(url: str, lat: Optional[float] = None,
                             lng: Optional[float] = None) -> dict:
    """Cheap-as-possible pool/storey lookup for one comp listing.

    Used by the lazy-loading API endpoint.  Strategy:
      1. Scrape the listing page text (storeys + pool both come from
         description text).
      2. If pool was not found in text, try OSM Overpass at the
         lat/lng (caller must pass it for this to work).
    """
    out = {"pool": None, "storeys": None}
    if url and "domain.com.au" in url:
        try:
            html = _fetch_page(url)
            from src.features import parse_pool_storeys_from_html
            pool, storeys = parse_pool_storeys_from_html(html)
            if pool is not None:
                out["pool"] = pool
            if storeys is not None:
                out["storeys"] = storeys
        except Exception:
            pass
    if out["pool"] is None and lat and lng:
        try:
            from src.features import osm_has_pool
            osm_pool = osm_has_pool(lat, lng)
            if osm_pool is not None:
                out["pool"] = osm_pool
        except Exception:
            pass
    return out


def _parse_features_from_html(html: str) -> Optional[dict]:
    """Shared feature parser for Domain listing/profile detail pages.

    Domain's __NEXT_DATA__ JSON varies between page types (live listing,
    sold listing, property-profile). Rather than hard-code paths, we
    recursively walk the JSON looking for the first dict that contains
    bed/bath/parking/landSize keys.  Pool + storey are extracted from
    raw page text by `src.features` — they are seldom in the JSON.
    """
    data = _extract_next_data(html)
    if not data:
        return None

    # Recursively find candidate feature dicts and address dicts.
    # Collect features from ALL dicts that look feature-ish, since beds/baths
    # may live in a different dict than parking/landSize on profile pages.
    feats: dict = {}
    addr: dict = {}

    # Map of canonical feature name -> set of regex patterns (case-insensitive)
    # matching keys Domain uses across its various JSON shapes.
    _key_patterns = {
        "beds":     [r"^beds?$", r"^bedrooms?$"],
        "baths":    [r"^baths?$", r"^bathrooms?$"],
        "cars":     [r"^parking$", r"^carspaces?$", r"^parkingspaces?$"],
        "land":     [r"^landsize$", r"^landarea(\(.*\))?$"],
        "ptype":    [r"^propertytype(formatted)?$"],
    }
    import re as _re
    _key_re = {k: [_re.compile(p, _re.I) for p in pats] for k, pats in _key_patterns.items()}

    def _match_canonical(key: str):
        for canon, regs in _key_re.items():
            for r in regs:
                if r.match(key):
                    return canon
        return None

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                canon = _match_canonical(k) if isinstance(k, str) else None
                if canon and v not in (None, "", 0) and not feats.get(canon):
                    feats[canon] = v
            keys = set(obj.keys())
            if not addr and ({"suburb", "postcode"} & keys) and ("street" in keys or "displayableAddress" in keys or "address" in keys):
                addr.update(obj)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(data)

    def _g(d, *keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", 0):
                return v
        return None

    beds  = feats.get("beds")
    baths = feats.get("baths")
    cars  = feats.get("cars")
    land  = feats.get("land")

    if beds is None and baths is None and cars is None and land is None:
        return None

    def _as_str(v):
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for k in ("name", "displayName", "value"):
                if isinstance(v.get(k), str):
                    return v[k]
        if v is None:
            return ""
        return str(v)

    suburb_s   = _as_str(addr.get("suburb"))
    state_s    = _as_str(addr.get("state"))
    postcode_s = _as_str(addr.get("postcode"))

    addr_raw = _g(addr, "displayableAddress", "address", "street")
    address_str = _as_str(addr_raw) if addr_raw else ""
    if address_str and suburb_s and suburb_s not in address_str:
        address_str = f"{address_str}, {suburb_s} {state_s} {postcode_s}".strip(", ")

    # Pool / storey from page text — Domain descriptions almost always say.
    from src.features import parse_pool_storeys_from_html
    pool, storeys = parse_pool_storeys_from_html(html)

    return {
        "address":  address_str,
        "suburb":   suburb_s,
        "state":    state_s.upper(),
        "postcode": postcode_s,
        "beds":     _safe_int(beds),
        "baths":    _safe_int(baths),
        "cars":     _safe_int(cars),
        "land_area": _safe_float(land),
        "property_type": _as_str(feats.get("ptype", "")),
        "lat": addr.get("lat") or addr.get("latitude"),
        "lng": addr.get("lng") or addr.get("longitude"),
        "pool":    pool,
        "storeys": storeys,
    }


def _build_property_profile_slug(address: str, suburb: str, state: str, postcode: str) -> Optional[str]:
    """Build a Domain property-profile slug from a Nominatim-style address.

    address example from Nominatim: "10, Callaway Crescent, Mernda Villages Estate, Mernda, Melbourne, Victoria, 3754"
    target slug:                    "10-callaway-crescent-mernda-vic-3754"
    """
    if not address or not suburb or not state:
        return None
    # First two comma-parts are street number + street name (sometimes the
    # number is missing, then first part is the street).
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return None
    street_bits = []
    # Take number + street name (skip later parts which are estate/suburb/etc.)
    for p in parts[:2]:
        if p.lower() == suburb.lower():
            break
        street_bits.append(p)
    if not street_bits:
        return None
    street = " ".join(street_bits)
    slug_parts = [street, suburb, state]
    if postcode:
        slug_parts.append(postcode)
    raw = " ".join(slug_parts).lower()
    # keep alnum and spaces, collapse to hyphens
    cleaned = re.sub(r"[^a-z0-9 ]+", "", raw)
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-")
    return cleaned


def get_property_profile(address: str, suburb: str, state: str, postcode: str) -> Optional[dict]:
    """Look up a property by address on Domain's property-profile pages.

    Tries several Domain URL formats since the slug structure varies.
    Returns the same dict shape as get_listing_features, or None.
    """
    slug = _build_property_profile_slug(address, suburb, state, postcode)
    if not slug:
        return None
    # Domain has a few possible URLs for an address — try them in order.
    candidate_urls = [
        f"https://www.domain.com.au/property-profile/{slug}",
        f"https://www.domain.com.au/{slug}",          # sometimes addresses live at root slug
        f"https://www.domain.com.au/address/{slug}",
    ]
    driver = _get_driver()
    try:
        for url in candidate_urls:
            try:
                html = _fetch_page_with_driver(driver, url)
            except Exception:
                continue
            if not html or len(html) < 1000:
                continue
            info = _parse_features_from_html(html)
            if info and (info.get("beds") or info.get("baths") or info.get("land_area")):
                info["_source_url"] = url
                # OSM pool fallback if the listing text didn't mention one
                if info.get("pool") is None:
                    try:
                        from src.features import osm_has_pool
                        plat = info.get("lat")
                        plng = info.get("lng")
                        if plat and plng:
                            osm_p = osm_has_pool(float(plat), float(plng))
                            if osm_p is not None:
                                info["pool"] = osm_p
                    except Exception:
                        pass
                return info
    finally:
        driver.quit()
    return None


def _get_listing_slug_url(listing_id: int) -> Optional[str]:
    """Return the SEO slug URL for a listing_id.

    Domain redirects /listing/<id> → SEO slug URL for current listings.
    We follow that redirect via Selenium and capture the final URL.
    """
    canonical = f"https://www.domain.com.au/listing/{listing_id}"
    html, final_url = _fetch_page_with_url(canonical)
    # If redirected to SEO slug (contains listing_id and doesn't start with /listing/)
    path = final_url.split("domain.com.au")[-1] if "domain.com.au" in final_url else final_url
    if str(listing_id) in final_url and not path.startswith("/listing/"):
        return final_url
    # Fallback: read listingUrl from embedded JSON
    try:
        data = _extract_next_data(html)
        cp = data["props"]["pageProps"].get("componentProps", {})
        url = cp.get("listingUrl", "")
        if url:
            return url
    except (KeyError, TypeError):
        pass
    return None


def _bounding_box(lat: float, lng: float, radius_km: float) -> Tuple[float, float, float, float]:
    """Return (min_lat, min_lng, max_lat, max_lng) for a radius around a point."""
    # 1 degree latitude ≈ 111.32 km
    delta_lat = radius_km / 111.32
    # 1 degree longitude ≈ 111.32 * cos(lat) km
    delta_lng = radius_km / (111.32 * math.cos(math.radians(lat)))
    return lat - delta_lat, lng - delta_lng, lat + delta_lat, lng + delta_lng


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in km between two lat/lng points."""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_nearby_sales(
    lat: float,
    lng: float,
    radius_km: float = 5.0,
    months: int = 6,
    pages: int = 3,
    suburb: str = "",
    state: str = "",
    postcode: str = "",
) -> List[SaleResult]:
    """Scrape sold properties within radius_km of (lat, lng) sold within last `months` months.

    If suburb/state/postcode are provided they are used directly; otherwise
    the suburb must be passed from the CLI.  Domain's sold-listings pages are
    fetched by suburb slug — the bounding-box lat/lng filter is applied
    client-side to drop results outside the radius.
    """
    cutoff = datetime.now() - timedelta(days=30 * months)

    all_results: List[SaleResult] = []

    slug = _slug(suburb, state, postcode) if suburb and state else ""
    if not slug:
        return []

    driver = _get_driver()
    try:
        for page in range(1, pages + 1):
            url = f"https://www.domain.com.au/sold-listings/{slug}/?page={page}"
            html = _fetch_page_with_driver(driver, url)
            raw = _extract_json_listings(html)
            if not raw:
                break

            for item in raw:
                result = _parse_sale_nearby(item, lat, lng)
                if result is None:
                    continue
                # Drop properties outside radius
                if result.distance_km is not None and result.distance_km > radius_km:
                    continue
                # Filter by sold date
                if result.sold_date:
                    try:
                        sold_dt = datetime.strptime(result.sold_date[:10], "%Y-%m-%d")
                        if sold_dt < cutoff:
                            continue
                    except ValueError:
                        pass
                all_results.append(result)
    finally:
        driver.quit()

    # Sort by distance
    all_results.sort(key=lambda r: r.distance_km if r.distance_km is not None else 999)
    return all_results


def _parse_sale_nearby(item: Any, ref_lat: float, ref_lng: float) -> Optional[SaleResult]:
    """Parse a sold listing and attach distance from reference point."""
    try:
        listing_id = item.get("id", 0)
        lm = item.get("listingModel", {})
        address = lm.get("address", {})

        item_lat = address.get("lat")
        item_lng = address.get("lng")
        distance = None
        if item_lat and item_lng:
            distance = _haversine_km(ref_lat, ref_lng, item_lat, item_lng)

        street = address.get("street", "")
        suburb_val = address.get("suburb", "")
        state_val = address.get("state", "")
        postcode_val = address.get("postcode", "")
        full_address = f"{street}, {suburb_val} {state_val} {postcode_val}".strip(", ")

        features = lm.get("features", {})
        price_raw = lm.get("price", "")
        # Try to extract numeric price from string like "Sold $1.29m" or "$1,290,000"
        price_num = _extract_price_number(str(price_raw))

        # Sold date is in tags.tagText e.g. "Sold at auction 17 Apr 2026"
        sold_date_str = ""
        tags = lm.get("tags", {})
        tag_text = tags.get("tagText", "") if isinstance(tags, dict) else ""
        if tag_text:
            date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', tag_text)
            if date_match:
                try:
                    sold_date_str = datetime.strptime(date_match.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass

        r = SaleResult(
            property_id=int(listing_id) if listing_id else None,
            address=full_address,
            suburb=suburb_val,
            state=state_val,
            postcode=postcode_val,
            price=price_num,
            price_display=str(price_raw),
            sold_date=sold_date_str or lm.get("soldDate") or "",
            property_type=features.get("propertyTypeFormatted") or features.get("propertyType", ""),
            bedrooms=_safe_int(features.get("beds")),
            bathrooms=_safe_int(features.get("baths")),
            carspaces=_safe_int(features.get("parking")),
            land_area=_safe_float(features.get("landSize") or features.get("landArea")),
            distance_km=round(distance, 2) if distance is not None else None,
            url="https://www.domain.com.au" + lm.get("url", "") if lm.get("url") else "",
            lat=_safe_float(item_lat),
            lng=_safe_float(item_lng),
        )
        return r
    except Exception:
        return None


def _extract_price_number(price_str: str) -> Optional[int]:
    """Extract numeric value from price strings like '$1.29m', '$1,290,000', 'Sold $950,000'."""
    s = price_str.lower().replace(",", "")
    m = re.search(r'\$?([\d.]+)\s*m', s)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    m = re.search(r'\$?([\d]{4,})', s)
    if m:
        return int(m.group(1))
    return None


def suggest_suburbs(query: str) -> List[dict]:
    """Use domain.com.au autocomplete via Selenium."""
    import httpx
    url = f"https://suggest.domain.com.au/suggestions?query={query}&suggestionTypes=SuburbOrPostcode"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"}
        with httpx.Client(headers=headers, timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("suggestions", [])
    except Exception:
        return []


def _safe_int(val) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None

"""
Domain.com.au scraper — uses Selenium + Chrome to bypass bot protection,
then extracts listing data from __NEXT_DATA__ JSON embedded in the page.
"""
import json
import re
import time
from typing import Optional, List, Any
from src.models import PropertyListing, SaleResult

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def _get_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def _slug(suburb: str, state: str, postcode: str = "") -> str:
    """Build domain.com.au suburb slug e.g. richmond-vic-3121"""
    parts = suburb.lower().replace(" ", "-") + "-" + state.lower()
    if postcode:
        parts += "-" + postcode
    return parts


def _fetch_page(url: str) -> str:
    """Fetch a page using Selenium Chrome to bypass bot protection."""
    driver = _get_driver()
    try:
        driver.get(url)
        time.sleep(3)  # wait for JS to render
        return driver.page_source
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
        if "listing" in item:
            item = item["listing"]
        prop = item.get("propertyDetails", item)
        address_parts = prop.get("displayableAddress") or item.get("address", "")
        if isinstance(address_parts, dict):
            address_parts = address_parts.get("street", "")
        price_raw = item.get("priceDetails", {}).get("price") or item.get("price")
        return SaleResult(
            property_id=item.get("id"),
            address=str(address_parts),
            suburb=str(prop.get("suburb") or item.get("suburb", "")),
            state=str(prop.get("state") or item.get("state", "")),
            postcode=str(prop.get("postcode") or item.get("postcode", "")),
            price=_safe_int(price_raw),
            sold_date=item.get("soldDate") or item.get("dateSold", ""),
            property_type=str(prop.get("propertyType") or item.get("propertyType", "")),
            bedrooms=_safe_int(prop.get("bedrooms")),
            bathrooms=_safe_int(prop.get("bathrooms")),
            land_area=_safe_float(prop.get("landArea")),
        )
    except Exception:
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

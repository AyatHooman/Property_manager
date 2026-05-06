"""
Domain API wrapper — listings, property details, sales results, suburb data.
"""
import httpx
from typing import Optional, Union, List, Dict, Any
from src.auth import DomainAuth
from src.models import PropertyListing, SaleResult, SuburbProfile

BASE_URL = "https://api.domain.com.au/v1"


class DomainAPIClient:
    def __init__(self):
        self.auth = DomainAuth()

    def _get(self, path: str, params: dict = None) -> Any:
        url = f"{BASE_URL}{path}"
        response = httpx.get(url, headers=self.auth.headers, params=params or {})
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict) -> Any:
        url = f"{BASE_URL}{path}"
        response = httpx.post(url, headers=self.auth.headers, json=body)
        response.raise_for_status()
        return response.json()

    # ── Listings ──────────────────────────────────────────────────────────────

    def search_listings(
        self,
        suburb: str,
        state: str,
        listing_type: str = "Sale",
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        min_beds: Optional[int] = None,
        max_beds: Optional[int] = None,
        property_types: Optional[List[str]] = None,
        page_size: int = 20,
    ) -> List[PropertyListing]:
        """Search for residential property listings."""
        body = {
            "listingType": listing_type,
            "locations": [
                {
                    "state": state,
                    "suburb": suburb,
                    "includeSurroundingSuburbs": False,
                }
            ],
            "pageSize": page_size,
        }

        if min_price or max_price:
            body["priceRange"] = {}
            if min_price:
                body["priceRange"]["minimum"] = min_price
            if max_price:
                body["priceRange"]["maximum"] = max_price

        if min_beds or max_beds:
            body["bedroomsRange"] = {}
            if min_beds:
                body["bedroomsRange"]["minimum"] = min_beds
            if max_beds:
                body["bedroomsRange"]["maximum"] = max_beds

        if property_types:
            body["propertyTypes"] = property_types

        data = self._post("/listings/residential/_search", body)
        return [self._parse_listing(item) for item in data]

    def get_listing(self, listing_id: int) -> PropertyListing:
        """Get a single listing by ID."""
        data = self._get(f"/listings/{listing_id}")
        return self._parse_listing({"listing": data})

    def _parse_listing(self, item: dict) -> PropertyListing:
        listing = item.get("listing", item)
        prop_details = listing.get("propertyDetails", {})
        price_details = listing.get("priceDetails", {})
        advertiser = listing.get("advertiser", {})
        contacts = advertiser.get("contacts", [])
        agent_name = contacts[0].get("name") if contacts else None

        address_parts = prop_details.get("displayableAddress", "")
        suburb = prop_details.get("suburb", "")
        state = prop_details.get("state", "")
        postcode = prop_details.get("postcode", "")

        return PropertyListing(
            id=listing.get("id", 0),
            listing_type=listing.get("listingType", ""),
            status=listing.get("status", ""),
            price=price_details.get("displayPrice"),
            address=address_parts,
            suburb=suburb,
            state=state,
            postcode=postcode,
            bedrooms=prop_details.get("bedrooms"),
            bathrooms=prop_details.get("bathrooms"),
            carspaces=prop_details.get("carspaces"),
            property_type=prop_details.get("propertyType", ""),
            land_area=prop_details.get("landArea"),
            description=listing.get("description"),
            url=listing.get("seoUrl"),
            agent_name=agent_name,
            agency_name=advertiser.get("name"),
            listed_date=listing.get("dateAvailable"),
        )

    # ── Sales Results ─────────────────────────────────────────────────────────

    def get_sales_results(self, suburb: str, state: str, postcode: str) -> List[SaleResult]:
        """Get recent auction/sales results for a suburb."""
        params = {"suburb": suburb, "state": state, "postcode": postcode}
        data = self._get("/salesResults", params)
        results = data.get("results", []) if isinstance(data, dict) else data
        return [self._parse_sale(r) for r in results]

    def _parse_sale(self, item: dict) -> SaleResult:
        prop = item.get("property", {})
        return SaleResult(
            property_id=item.get("id"),
            address=prop.get("address", ""),
            suburb=prop.get("suburb", ""),
            state=prop.get("state", ""),
            postcode=prop.get("postcode", ""),
            price=item.get("price"),
            sold_date=item.get("reportedDate"),
            property_type=prop.get("propertyType", ""),
            bedrooms=prop.get("bedrooms"),
            bathrooms=prop.get("bathrooms"),
            land_area=prop.get("landArea"),
        )

    # ── Suburb Profile ────────────────────────────────────────────────────────

    def get_suburb_performance(
        self,
        suburb: str,
        state: str,
        postcode: str,
        property_category: str = "house",
        bedrooms: int = 4,
    ) -> SuburbProfile:
        """Get median prices and performance stats for a suburb."""
        params = {
            "state": state,
            "suburb": suburb,
            "postcode": postcode,
            "propertyCategory": property_category,
            "bedroomsCount": bedrooms,
        }
        data = self._get("/suburbPerformanceStatistics", params)
        series = data.get("series", {}).get("seriesInfo", [{}])
        latest = series[-1] if series else {}

        return SuburbProfile(
            suburb=suburb,
            state=state,
            postcode=postcode,
            median_sale_price=latest.get("medianSoldPrice"),
            median_rent_price=latest.get("medianRentListingPrice"),
            auction_clearance_rate=latest.get("auctionNumberAuctioned"),
            days_on_market=latest.get("daysOnMarket"),
            properties_sold=latest.get("numberSold"),
        )

    # ── Suggestions / Autocomplete ────────────────────────────────────────────

    def suggest_suburbs(self, query: str) -> List[dict]:
        """Autocomplete suburb names."""
        data = self._get("/typeAhead", params={"terms": query, "searchTypes": "SuburbOrPostcode"})
        return data if isinstance(data, list) else []

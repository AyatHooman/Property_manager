"""
Data models for Domain API responses.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PropertyListing:
    id: int
    listing_type: str          # "Sale" or "Rent"
    status: str
    price: Optional[str]
    address: str
    suburb: str
    state: str
    postcode: str
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    carspaces: Optional[int]
    property_type: str
    land_area: Optional[float]
    description: Optional[str]
    url: Optional[str]
    agent_name: Optional[str]
    agency_name: Optional[str]
    listed_date: Optional[str]


@dataclass
class SaleResult:
    property_id: Optional[int]
    address: str
    suburb: str
    state: str
    postcode: str
    price: Optional[int]
    price_display: Optional[str]
    sold_date: Optional[str]
    property_type: str
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    land_area: Optional[float]
    carspaces: Optional[int] = None
    distance_km: Optional[float] = None
    url: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    pool: Optional[bool] = None       # None = unknown, True/False = known
    storeys: Optional[int] = None     # None = unknown, 1 or 2


@dataclass
class SuburbProfile:
    suburb: str
    state: str
    postcode: str
    median_sale_price: Optional[int]
    median_rent_price: Optional[int]
    auction_clearance_rate: Optional[float]
    days_on_market: Optional[int]
    properties_sold: Optional[int]

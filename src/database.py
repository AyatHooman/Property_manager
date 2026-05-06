"""
SQLite cache layer — avoids re-fetching data and respects rate limits.
"""
import sqlite3
import json
import time
import os
from pathlib import Path
from typing import Optional, Any, List

DB_PATH = Path(__file__).parent.parent / "data" / "property_cache.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings_cache (
                cache_key   TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                cached_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_listings (
                id          INTEGER PRIMARY KEY,
                listing_id  INTEGER UNIQUE NOT NULL,
                address     TEXT,
                suburb      TEXT,
                state       TEXT,
                postcode    TEXT,
                price       TEXT,
                bedrooms    INTEGER,
                bathrooms   INTEGER,
                property_type TEXT,
                url         TEXT,
                saved_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query       TEXT NOT NULL,
                searched_at REAL NOT NULL
            );
        """)


# ── Cache helpers ──────────────────────────────────────────────────────────────

def get_cache(key: str, max_age_seconds: int = 3600) -> Optional[Any]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT data, cached_at FROM listings_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    if row and (time.time() - row["cached_at"]) < max_age_seconds:
        return json.loads(row["data"])
    return None


def set_cache(key: str, data: Any):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO listings_cache (cache_key, data, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(data, default=str), time.time()),
        )


# ── Saved Listings ─────────────────────────────────────────────────────────────

def save_listing(listing) -> bool:
    """Save a listing for later reference. Returns False if already saved."""
    try:
        with _conn() as conn:
            conn.execute(
                """INSERT INTO saved_listings
                   (listing_id, address, suburb, state, postcode, price, bedrooms,
                    bathrooms, property_type, url, saved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing.id,
                    listing.address,
                    listing.suburb,
                    listing.state,
                    listing.postcode,
                    listing.price,
                    listing.bedrooms,
                    listing.bathrooms,
                    listing.property_type,
                    listing.url,
                    time.time(),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_saved_listings() -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_listings ORDER BY saved_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_saved_listing(listing_id: int) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM saved_listings WHERE listing_id = ?", (listing_id,)
        )
    return cursor.rowcount > 0


# ── Search history ─────────────────────────────────────────────────────────────

def log_search(query: str):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO search_history (query, searched_at) VALUES (?, ?)",
            (query, time.time()),
        )


def get_search_history(limit: int = 10) -> List[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT query FROM search_history ORDER BY searched_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r["query"] for r in rows]

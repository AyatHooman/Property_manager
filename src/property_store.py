"""Unified per-property store.

ONE physical property = ONE row, no matter how many times or in how many forms
we have seen it. Previously the same house could be stored twice:

  * once in data/for_sale.db  (current "on the market" listings), and
  * once (often many times) inside data/sales_cache/*.json (sold comps, keyed
    by *search query* — so a single sold house appeared in every overlapping
    search).

Domain gives a *different* listing id to the for-sale ad and to the later sold
ad of the same house, so the listing id cannot be the identity. The stable
identity is the **address** (street + suburb + state + postcode), which both
scrapers build from the same Domain `address` object, so they match after
normalisation.

This module keeps a single table `properties` in data/properties.db keyed by a
normalised address. Each row carries:

  * identity + location + latest specs
  * a "for sale" block  (listing id / price / status / first & last seen …)
  * a "sold"    block  (sold price / sold date / url …)
  * a JSON `history` list recording every distinct event we have observed.

So a house that was on the market and later sold (or vice-versa) is a single
row that holds BOTH blocks.

Public API:
    init_db(path) -> sqlite3.Connection
    record_for_sale(con, listings) -> (inserted, updated)
    record_sold(con, items)        -> (inserted, updated)
    get(con, address, suburb, state, postcode) -> dict | None
    load_all(con) -> list[dict]
"""
import os, re, json, time, sqlite3
from typing import List, Dict, Any, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "properties.db")

# Street-type abbreviations expanded so "12 Smith St" and "12 Smith Street"
# collapse to the same key. Kept small + common — over-expanding risks
# collisions, so only unambiguous suffixes are listed.
_ABBREV = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "cres": "crescent", "cr": "crescent", "ct": "court", "dr": "drive",
    "drv": "drive", "pl": "place", "pde": "parade", "hwy": "highway",
    "ln": "lane", "tce": "terrace", "blvd": "boulevard", "cct": "circuit",
    "cl": "close", "gr": "grove", "grn": "green", "wy": "way",
}


def _norm_key(address: str, suburb: str = "", state: str = "",
              postcode: str = "") -> str:
    """Build a stable identity key from an address.

    Both scrapers produce ``"<street>, <suburb> <state> <postcode>"``. We fold
    case, expand street-type abbreviations, drop punctuation and collapse
    whitespace so trivial formatting differences don't split one house into two.
    """
    base = address or ""
    # Make sure suburb/state/postcode are present even if the caller passed a
    # bare street (sold detail pages sometimes do).
    for extra in (suburb, state, postcode):
        if extra and extra.lower() not in base.lower():
            base = f"{base} {extra}"
    base = base.lower()
    base = base.replace("&", " and ")
    base = re.sub(r"[^a-z0-9\s]", " ", base)           # drop commas / slashes …
    toks = [_ABBREV.get(t, t) for t in base.split()]
    return " ".join(toks).strip()


def _money(x) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).replace(",", "")
    m = re.search(r"([\d.]+)\s*([mMkK]?)", s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    suf = m.group(2).lower()
    if suf == "m":
        v *= 1_000_000
    elif suf == "k":
        v *= 1_000
    return int(v)


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            prop_key    TEXT PRIMARY KEY,
            address     TEXT,
            suburb      TEXT,
            state       TEXT,
            postcode    TEXT,
            lat         REAL,
            lng         REAL,
            -- latest known specs (whichever source touched the row last) --
            beds        INTEGER,
            baths       INTEGER,
            carspaces   INTEGER,
            land_m2     REAL,
            prop_type   TEXT,
            -- for-sale block --
            fs_listing_id INTEGER,
            fs_price_text TEXT,
            fs_price_low  INTEGER,
            fs_price_high INTEGER,
            fs_sale_type  TEXT,
            fs_agency     TEXT,
            fs_url        TEXT,
            fs_images     TEXT,          -- JSON array
            fs_status     TEXT,          -- 'active' / 'offmarket' / NULL
            fs_first_seen TEXT,
            fs_last_seen  TEXT,
            -- sold block --
            sold_listing_id INTEGER,
            sold_price      INTEGER,
            sold_price_text TEXT,
            sold_date       TEXT,
            sold_url        TEXT,
            sold_seen       TEXT,
            -- roll-up flags --
            on_market   INTEGER NOT NULL DEFAULT 0,
            was_sold    INTEGER NOT NULL DEFAULT 0,
            history     TEXT,            -- JSON array of {kind, ts, ...}
            created_at  TEXT,
            updated_at  TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS prop_geo ON properties(lat, lng)")
    con.commit()
    return con


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fetch_row(con, key) -> Optional[dict]:
    cur = con.execute("SELECT * FROM properties WHERE prop_key=?", (key,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _load_history(row: Optional[dict]) -> list:
    if not row:
        return []
    try:
        return json.loads(row.get("history") or "[]")
    except Exception:
        return []


def _append_event(history: list, event: dict) -> list:
    """Append an event unless an equivalent one (same kind + key field) exists,
    so re-scraping the same data doesn't grow the log forever."""
    kind = event.get("kind")
    sig = event.get("date") or event.get("price_text") or event.get("status")
    for e in history:
        if e.get("kind") == kind and (
                e.get("date") == event.get("date")
                and e.get("price_text") == event.get("price_text")
                and e.get("status") == event.get("status")):
            return history
    history.append(event)
    return history


def _coalesce(new, old):
    """Prefer a non-empty new value, else keep the old one."""
    return new if new not in (None, "", []) else old


# ── For-sale ingestion ────────────────────────────────────────────────────────

def record_for_sale(con: sqlite3.Connection,
                    listings: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Merge a batch of for-sale listing dicts (as produced by
    scraper_domain_sale._parse_listing) into the unified store."""
    ins = upd = 0
    now = _now()
    for L in listings or []:
        addr = L.get("address") or ""
        key = _norm_key(addr, L.get("suburb", ""), L.get("state", ""),
                        str(L.get("postcode") or ""))
        if not key:
            continue
        row = _fetch_row(con, key)
        hist = _append_event(_load_history(row), {
            "kind": "for_sale", "ts": now,
            "price_text": L.get("price_text") or "",
            "status": "active",
        })
        vals = {
            "address": addr, "suburb": L.get("suburb"), "state": L.get("state"),
            "postcode": str(L.get("postcode") or ""),
            "lat": L.get("lat"), "lng": L.get("lng"),
            "beds": L.get("beds"), "baths": L.get("baths"),
            "carspaces": L.get("carspaces"), "land_m2": L.get("land_m2"),
            "prop_type": L.get("prop_type"),
            "fs_listing_id": L.get("listing_id"),
            "fs_price_text": L.get("price_text"),
            "fs_price_low": L.get("price_low"), "fs_price_high": L.get("price_high"),
            "fs_sale_type": L.get("sale_type"), "fs_agency": L.get("agency"),
            "fs_url": L.get("url"),
            "fs_images": json.dumps(L.get("image_urls") or [], separators=(",", ":")),
            "fs_status": "active",
            "fs_last_seen": now,
            "on_market": 1,
            "history": json.dumps(hist, ensure_ascii=False),
            "updated_at": now,
        }
        if row:
            # keep specs if the new scrape lacks them
            for spec in ("beds", "baths", "carspaces", "land_m2", "prop_type",
                         "lat", "lng"):
                vals[spec] = _coalesce(vals[spec], row.get(spec))
            sets = ", ".join(f"{k}=?" for k in vals)
            con.execute(f"UPDATE properties SET {sets} WHERE prop_key=?",
                        (*vals.values(), key))
            upd += 1
        else:
            vals["fs_first_seen"] = now
            vals["was_sold"] = 0
            vals["created_at"] = now
            vals["prop_key"] = key
            cols = ", ".join(vals)
            qs = ", ".join("?" for _ in vals)
            con.execute(f"INSERT INTO properties ({cols}) VALUES ({qs})",
                        tuple(vals.values()))
            ins += 1
    con.commit()
    return ins, upd


# ── Sold ingestion ────────────────────────────────────────────────────────────

def record_sold(con: sqlite3.Connection,
                items: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Merge a batch of sold comps (as produced by web.py's
    `_results_to_items`) into the unified store. A house already present as a
    for-sale listing is updated in place (its sold block filled, on_market
    cleared) rather than duplicated."""
    ins = upd = 0
    now = _now()
    for it in items or []:
        addr = it.get("address") or ""
        key = _norm_key(addr, it.get("suburb", ""), it.get("state", ""),
                        str(it.get("postcode") or ""))
        if not key:
            continue
        sold_price = _money(it.get("price_num") if it.get("price_num") is not None
                            else it.get("price"))
        row = _fetch_row(con, key)
        hist = _append_event(_load_history(row), {
            "kind": "sold", "ts": now,
            "date": it.get("sold_date") or "",
            "price": sold_price,
            "price_text": it.get("price") or "",
        })
        vals = {
            "address": addr, "suburb": it.get("suburb"), "state": it.get("state"),
            "postcode": str(it.get("postcode") or ""),
            "lat": it.get("lat"), "lng": it.get("lng"),
            "beds": it.get("beds"), "baths": it.get("baths"),
            "carspaces": it.get("cars"), "land_m2": it.get("land_area"),
            "prop_type": it.get("type"),
            "sold_listing_id": it.get("property_id"),
            "sold_price": sold_price,
            "sold_price_text": it.get("price"),
            "sold_date": it.get("sold_date"),
            "sold_url": it.get("url"),
            "sold_seen": now,
            "was_sold": 1,
            # If we now know it sold, it is no longer on the market.
            "on_market": 0,
            "fs_status": "sold" if (row and row.get("fs_status")) else None,
            "history": json.dumps(hist, ensure_ascii=False),
            "updated_at": now,
        }
        if row:
            for spec in ("beds", "baths", "carspaces", "land_m2", "prop_type",
                         "lat", "lng"):
                vals[spec] = _coalesce(vals[spec], row.get(spec))
            # Don't clobber the for-sale status to NULL if the row had one;
            # mark it 'sold' so the popup can say "was for sale, now sold".
            if not row.get("fs_status"):
                vals.pop("fs_status")
            sets = ", ".join(f"{k}=?" for k in vals)
            con.execute(f"UPDATE properties SET {sets} WHERE prop_key=?",
                        (*vals.values(), key))
            upd += 1
        else:
            vals.pop("fs_status", None)
            vals["created_at"] = now
            vals["prop_key"] = key
            cols = ", ".join(vals)
            qs = ", ".join("?" for _ in vals)
            con.execute(f"INSERT INTO properties ({cols}) VALUES ({qs})",
                        tuple(vals.values()))
            ins += 1
    con.commit()
    return ins, upd


# ── Reads ─────────────────────────────────────────────────────────────────────

def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    for jf in ("fs_images", "history"):
        try:
            d[jf] = json.loads(d.get(jf) or "[]")
        except Exception:
            d[jf] = []
    return d


def get(con: sqlite3.Connection, address: str, suburb: str = "",
        state: str = "", postcode: str = "") -> Optional[dict]:
    """Fetch one unified property record by address."""
    key = _norm_key(address, suburb, state, str(postcode or ""))
    row = _fetch_row(con, key)
    return _row_to_dict(row) if row else None


def load_all(con: sqlite3.Connection) -> List[dict]:
    cur = con.execute("SELECT * FROM properties")
    cols = [d[0] for d in cur.description]
    return [_row_to_dict(dict(zip(cols, r))) for r in cur.fetchall()]

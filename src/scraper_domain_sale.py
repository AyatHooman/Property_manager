"""Domain "For Sale" listing scraper, driven by a user-supplied filter URL.

Reuses the undetected_chromedriver session from src/scraper.py. Paginates the
filter URL, extracts each listing's id / address / lat-lng / price / features
/ images from the embedded __NEXT_DATA__ JSON, and writes them to a local
SQLite DB so the map can render points without re-scraping.

Public API:
    scrape_filter_url(filter_url, max_pages=20)   -> list[dict]
    init_db(db_path)                              -> sqlite3.Connection
    save_listings(con, listings)                  -> (inserted, updated)
    load_listings(con)                            -> list[dict]
"""
import json, os, re, sqlite3, time, random
from typing import List, Dict, Any, Optional, Tuple

from src import scraper as _base

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "for_sale.db")


# ── DB layer ─────────────────────────────────────────────────────────────────

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id  INTEGER PRIMARY KEY,
            address     TEXT,
            suburb      TEXT,
            state       TEXT,
            postcode    TEXT,
            lat         REAL,
            lng         REAL,
            price_text  TEXT,
            price_low   INTEGER,
            price_high  INTEGER,
            beds        INTEGER,
            baths       INTEGER,
            carspaces   INTEGER,
            land_m2     REAL,
            prop_type   TEXT,
            sale_type   TEXT,
            agency      TEXT,
            url         TEXT,
            image_urls  TEXT,           -- JSON array
            risk_count  INTEGER,         -- value-risk count (NULL = not computed yet)
            first_seen  TEXT,
            last_seen   TEXT,
            removed     INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migrate older DBs that predate newer columns.
    cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    if "risk_count" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN risk_count INTEGER")
    if "favourite" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN favourite INTEGER NOT NULL DEFAULT 0")
    if "rc_version" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN rc_version INTEGER")
    if "last_changed" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN last_changed TEXT")
    # Spatial filter goes through these
    con.execute("CREATE INDEX IF NOT EXISTS listings_geo  ON listings(lat, lng)")
    con.execute("CREATE INDEX IF NOT EXISTS listings_seen ON listings(last_seen)")
    con.commit()
    return con


def hide_stale(con: sqlite3.Connection, scrape_start_iso: str) -> int:
    """Mark listings not seen in the latest scrape as removed (hidden from the
    map). Sold / under-offer / withdrawn listings drop out of Domain's sale
    results, so anything whose last_seen predates this scrape is no longer on
    the market. Favourites are kept so the user doesn't lose saved ones.
    Returns how many were hidden."""
    cur = con.execute(
        "UPDATE listings SET removed=1 WHERE last_seen < ? AND favourite=0 AND removed=0",
        (scrape_start_iso,))
    con.commit()
    return cur.rowcount


def set_favourite(con: sqlite3.Connection, listing_id: int, fav: bool) -> bool:
    """Mark/unmark a listing as a favourite. Returns True if a row changed."""
    cur = con.execute("UPDATE listings SET favourite=? WHERE listing_id=?",
                       (1 if fav else 0, int(listing_id)))
    con.commit()
    return cur.rowcount > 0


def fill_risk_counts(con: sqlite3.Connection, limit: int = 10000) -> int:
    """Compute + persist risk_count for listings whose count is missing OR was
    computed under an older factor-rule version. Cached per coordinate, so it's
    cheap after the first pass. Returns how many were (re)filled."""
    try:
        from src import value_factors
        ver = value_factors.FACTOR_VERSION
    except Exception:
        return 0
    rows = con.execute(
        "SELECT listing_id, lat, lng FROM listings "
        "WHERE removed=0 AND lat IS NOT NULL "
        "AND (risk_count IS NULL OR rc_version IS NULL OR rc_version != ?) LIMIT ?",
        (ver, limit)).fetchall()
    n = 0
    for lid, lat, lng in rows:
        try:
            rc = value_factors.compute_factors(lat, lng).get("risk_count", 0)
        except Exception:
            continue
        con.execute("UPDATE listings SET risk_count=?, rc_version=? WHERE listing_id=?",
                    (rc, ver, lid))
        n += 1
    if n:
        con.commit()
    return n


def save_listings(con: sqlite3.Connection, listings: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Insert new listings; UPSERT existing ones with the fresher info.
    Returns (inserted_count, updated_count)."""
    if not listings:
        return 0, 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = con.cursor()
    # Pull the fields we compare for "did it change?" so we can stamp
    # last_changed only on a real change (price, status, specs).
    prev = {}
    for row in cur.execute(
            "SELECT listing_id, price_text, price_low, price_high, sale_type, "
            "beds, baths, carspaces, land_m2, prop_type FROM listings"):
        prev[row[0]] = row[1:]
    inserted = updated = 0
    for L in listings:
        lid = L.get("listing_id")
        if not lid:
            continue
        params = (
            lid, L.get("address"), L.get("suburb"), L.get("state"), L.get("postcode"),
            L.get("lat"), L.get("lng"),
            L.get("price_text"), L.get("price_low"), L.get("price_high"),
            L.get("beds"), L.get("baths"), L.get("carspaces"), L.get("land_m2"),
            L.get("prop_type"), L.get("sale_type"), L.get("agency"), L.get("url"),
            json.dumps(L.get("image_urls") or [], separators=(",", ":")),
            now,            # last_seen
        )
        if lid in prev:
            # Did anything meaningful change vs the stored row?
            new_cmp = (L.get("price_text"), L.get("price_low"), L.get("price_high"),
                       L.get("sale_type"), L.get("beds"), L.get("baths"),
                       L.get("carspaces"), L.get("land_m2"), L.get("prop_type"))
            changed = (tuple(prev[lid]) != new_cmp)
            if changed:
                cur.execute("""UPDATE listings SET
                    address=?, suburb=?, state=?, postcode=?, lat=?, lng=?,
                    price_text=?, price_low=?, price_high=?,
                    beds=?, baths=?, carspaces=?, land_m2=?,
                    prop_type=?, sale_type=?, agency=?, url=?,
                    image_urls=?, last_seen=?, removed=0, last_changed=?
                    WHERE listing_id=?""",
                    params[1:] + (now, lid))
            else:
                # Seen again but unchanged — refresh last_seen only.
                cur.execute("UPDATE listings SET last_seen=?, removed=0 WHERE listing_id=?",
                            (now, lid))
            updated += 1
        else:
            cur.execute("""INSERT INTO listings (
                listing_id, address, suburb, state, postcode, lat, lng,
                price_text, price_low, price_high,
                beds, baths, carspaces, land_m2,
                prop_type, sale_type, agency, url,
                image_urls, last_seen, first_seen, last_changed
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                params + (now, now))
            inserted += 1
    con.commit()
    return inserted, updated


def load_listings(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    # The latest scrape stamped all listings it saw with the same last_seen.
    # Relative to that timestamp: NEW = first appeared this scrape; UPDATED =
    # existed before but a field changed this scrape. Anything highlighted in a
    # previous refresh but unchanged now is neither.
    row = con.execute("SELECT MAX(last_seen) FROM listings WHERE removed=0").fetchone()
    latest_ts = row[0] if row else None
    cur = con.execute("""
        SELECT listing_id, address, suburb, state, postcode, lat, lng,
               price_text, price_low, price_high,
               beds, baths, carspaces, land_m2,
               prop_type, sale_type, agency, url, image_urls,
               risk_count, favourite, first_seen, last_seen, last_changed
        FROM listings WHERE removed=0 AND lat IS NOT NULL AND lng IS NOT NULL
    """)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur:
        d = dict(zip(cols, row))
        try:
            d["image_urls"] = json.loads(d.get("image_urls") or "[]")
        except Exception:
            d["image_urls"] = []
        fs, lc = d.get("first_seen"), d.get("last_changed")
        d["is_new"] = 1 if (latest_ts and fs == latest_ts) else 0
        d["is_updated"] = 1 if (latest_ts and not d["is_new"] and lc == latest_ts) else 0
        out.append(d)
    return out


# ── Scraper ──────────────────────────────────────────────────────────────────

def _money(s: str) -> Optional[int]:
    """Parse '$1,250,000' / '1.25M' / '1.2m' / '$850k' into an int."""
    if not s: return None
    s = s.replace(",", "").strip()
    m = re.search(r"\$?\s*([\d.]+)\s*([mMkK]?)", s)
    if not m: return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    suf = m.group(2).lower()
    if suf == "m": val *= 1_000_000
    elif suf == "k": val *= 1_000
    return int(val)


def _parse_price_range(text: str) -> Tuple[Optional[int], Optional[int]]:
    """'$1.2m - $1.3m' / '$850,000+ Guide' → (low, high)."""
    if not text: return (None, None)
    nums = re.findall(r"\$?\s*([\d.,]+)\s*[mMkK]?", text)
    if not nums: return (None, None)
    parsed = []
    for n in nums:
        m = re.search(r"([\d.,]+)\s*([mMkK]?)", n)
        if not m: continue
        try:
            v = float(m.group(1).replace(",", ""))
            suf = m.group(2).lower()
            if suf == "m": v *= 1_000_000
            elif suf == "k": v *= 1_000
            parsed.append(int(v))
        except ValueError:
            continue
    if not parsed: return (None, None)
    if len(parsed) == 1: return (parsed[0], parsed[0])
    return (min(parsed), max(parsed))


def _safe_int(x):
    try: return int(x)
    except (TypeError, ValueError): return None


def _safe_float(x):
    try: return float(x)
    except (TypeError, ValueError): return None


# Matches Domain's sold / under-offer markers in either the human text
# ("Under Offer", "Sold") or the CSS class name ("is-under-offer", "is-sold").
_UNAVAILABLE_RE = re.compile(
    r"\bsold\b|is-sold|under[\s-]?offer|under[\s-]?contract|under[\s-]?application",
    re.IGNORECASE)


def _is_unavailable(item: dict, lm: dict) -> bool:
    """True if the listing is sold / under offer / under contract — we don't
    want those on the 'on the market' map even though Domain's sale search
    sometimes returns them."""
    bits = []
    lt = item.get("listingType") or item.get("listingModel", {}).get("listingType")
    if lt:
        bits.append(str(lt))
    bits.append(str(lm.get("status") or ""))
    tags = lm.get("tags")
    if isinstance(tags, dict):
        bits += [str(tags.get("tagText") or ""), str(tags.get("tagClassName") or "")]
    elif isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                bits += [str(t.get("tagText") or ""), str(t.get("tagClassName") or "")]
            else:
                bits.append(str(t))
    # 'soldListing' listingType, 'is-under-offer' className, 'Under Offer' text…
    return any(_UNAVAILABLE_RE.search(b) for b in bits if b)


def _parse_listing(item: dict) -> Optional[Dict[str, Any]]:
    """Pull every useful field out of one __NEXT_DATA__ listing dict."""
    try:
        lid = item.get("id")
        if not lid: return None
        lm = item.get("listingModel") or {}
        if _is_unavailable(item, lm):
            return None        # skip sold / under-offer / under-contract
        addr = lm.get("address") or {}
        feats = lm.get("features") or {}

        # lat/lng may live under address or top-level — Domain has changed it
        # historically. Probe both spots.
        lat = (addr.get("lat") or addr.get("latitude")
               or item.get("lat") or (item.get("geoLocation") or {}).get("lat"))
        lng = (addr.get("lng") or addr.get("longitude")
               or item.get("lng") or (item.get("geoLocation") or {}).get("lng"))
        lat = _safe_float(lat); lng = _safe_float(lng)

        street = addr.get("street") or ""
        suburb = addr.get("suburb") or ""
        state = addr.get("state") or ""
        postcode = addr.get("postcode") or ""
        full_addr = ", ".join(p for p in (street, f"{suburb} {state} {postcode}".strip()) if p)

        url = lm.get("url") or ""
        if url and not url.startswith("http"):
            url = "https://www.domain.com.au" + url

        price_text = lm.get("price") or lm.get("displaySearchPriceRange") or ""
        plow, phigh = _parse_price_range(price_text)

        # Image URLs: lm.media is a list of dicts each with imageUrl/largeImageUrl
        images = []
        for m in (lm.get("media") or []):
            if not isinstance(m, dict): continue
            u = m.get("imageUrl") or m.get("url") or m.get("smallImageUrl")
            if u: images.append(u)
            if len(images) >= 6: break

        # Sale type heuristic: text near "Auction" or "Private Sale" — Domain
        # exposes auctionSchedule / inspectionDetails on premium listings.
        sale_type = ""
        if lm.get("auction") or lm.get("auctionSchedule"):
            sale_type = "Auction"
        elif "auction" in price_text.lower():
            sale_type = "Auction"
        else:
            sale_type = "Private Sale"

        return {
            "listing_id": int(lid),
            "address":    full_addr,
            "suburb":     suburb, "state": state, "postcode": postcode,
            "lat":        lat, "lng": lng,
            "price_text": price_text,
            "price_low":  plow, "price_high": phigh,
            "beds":       _safe_int(feats.get("beds")),
            "baths":      _safe_int(feats.get("baths")),
            "carspaces":  _safe_int(feats.get("parking")),
            "land_m2":    _safe_float(feats.get("landSize")),
            "prop_type":  feats.get("propertyTypeFormatted") or feats.get("propertyType") or "",
            "sale_type":  sale_type,
            "agency":     (lm.get("branding") or {}).get("agencyName"),
            "url":        url,
            "image_urls": images,
        }
    except Exception:
        return None


def scrape_filter_url(filter_url: str, max_pages: int = 20) -> List[Dict[str, Any]]:
    """Paginate the given Domain filter URL until we hit an empty page, a
    repeat-only page, or max_pages. Returns parsed listing dicts."""
    driver = _base._get_driver()
    try:
        seen_ids = set()
        out = []
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in filter_url else "?"
            page_url = filter_url + sep + f"page={page}" if page > 1 else filter_url
            try:
                html = _base._fetch_page_with_driver(driver, page_url)
            except Exception as ex:
                print(f"  page {page} fetch failed: {ex}", flush=True)
                break
            raw = _base._extract_json_listings(html)
            if not raw:
                break
            new_items = []
            for it in raw:
                lid = it.get("id")
                if not lid or lid in seen_ids:
                    continue
                parsed = _parse_listing(it)
                if parsed and parsed.get("lat") and parsed.get("lng"):
                    seen_ids.add(lid)
                    new_items.append(parsed)
            print(f"  page {page}: +{len(new_items)} new (cum {len(out) + len(new_items)})", flush=True)
            out.extend(new_items)
            if not new_items:
                break
            # be polite — random 2-4s between pages
            time.sleep(2 + random.random() * 2)
        return out
    finally:
        try: _base._quit_driver(driver)
        except Exception: pass

"""Merge the four REIWA "May 2026" Perth house-market CSV exports into one clean
file, then patch the Perth (WA) suburbs in market_metrics_suburb.geojson with
those numbers.

Source exports (data captured from REIWA suburb quick-stats, May 2026):
  * perth_first_50_suburbs_...           ranks   1- 50  (cols use *_pct, not *_pct_yoy)
  * perth_second_50_suburbs_...          ranks  51-100
  * perth_remaining_suburbs_..._partial  ranks 101-357  (mostly N/A — fallback only)
  * perth_suburbs_196_to_357_...         ranks 196-357  (the complete version of the tail)

All growth figures are REIWA year-on-year (last-year) growth. There is no
3-month growth column, so the hvi_3 ("Price · 3mo") field is dropped from every
feature — the map no longer offers a 3-month growth layer.

Outputs:
  data/perth_house_market_reiwa_may2026.csv   — merged, deduplicated, clean
  static/overlays/market_metrics_suburb.geojson — WA features patched in place
"""
import csv, json, os, re, unicodedata

SRC_DIR = "data/perth_raw"
# Priority order: more complete / more specific exports first. When the same
# suburb appears in several files we keep the first row that carries a real
# (non-N/A) median price.
SRC_FILES = [
    "perth_first_50_suburbs_house_market_reiwa_may2026.csv",
    "perth_second_50_suburbs_house_market_reiwa_may2026.csv",
    "perth_suburbs_196_to_357_house_market_reiwa_may2026.csv",
    "perth_remaining_suburbs_house_market_reiwa_may2026_partial.csv",
]
MERGED_CSV = "data/perth_house_market_reiwa_may2026.csv"
GEOJSON    = "static/overlays/market_metrics_suburb.geojson"

FIELDS = ["suburb", "postcode", "median_house_price_aud",
          "median_house_rent_per_week_aud", "sales_price_growth_pct_yoy",
          "rental_price_growth_pct_yoy", "source_url"]


def norm(s):
    """Normalise a suburb name for matching (ASCII, lowercase, no punctuation)."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if ord(c) < 128).lower()
    s = re.sub(r"\([^)]*\)", "", s)              # strip "(WA)" disambiguators
    s = re.sub(r"[\.,'\"\-\&\/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def num(v):
    """Parse a numeric cell; '' / 'N/A' / non-numeric -> None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "" or s.upper() in ("N/A", "NA", "-"):
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def get(row, *names):
    """First non-empty value among the given column names (handles *_pct vs *_pct_yoy)."""
    for n in names:
        if n in row and str(row[n]).strip() != "":
            return row[n]
    return None


# ── 1. Read + merge ─────────────────────────────────────────────────────────
merged = {}  # norm(suburb) -> clean row dict
for fname in SRC_FILES:
    path = os.path.join(SRC_DIR, fname)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            suburb = (get(row, "suburb") or "").strip()
            if not suburb:
                continue
            key = norm(suburb)
            clean = {
                "suburb": suburb,
                "postcode": (get(row, "postcode") or "").strip(),
                "median_house_price_aud": num(get(row, "median_house_price_aud")),
                "median_house_rent_per_week_aud": num(get(row, "median_house_rent_per_week_aud")),
                "sales_price_growth_pct_yoy": num(get(row, "sales_price_growth_pct_yoy", "sales_price_growth_pct")),
                "rental_price_growth_pct_yoy": num(get(row, "rental_price_growth_pct_yoy", "rental_price_growth_pct")),
                "source_url": (get(row, "source_url") or "").strip(),
            }
            prev = merged.get(key)
            # Keep the existing row unless it lacks a price and the new one has one.
            if prev is None or (prev["median_house_price_aud"] is None
                                and clean["median_house_price_aud"] is not None):
                merged[key] = clean

rows = sorted(merged.values(), key=lambda r: r["suburb"].lower())
with_price = sum(1 for r in rows if r["median_house_price_aud"] is not None)
print(f"Merged suburbs: {len(rows)} (with median price: {with_price}, N/A: {len(rows)-with_price})")

os.makedirs("data", exist_ok=True)
with open(MERGED_CSV, "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r[k] is None else r[k]) for k in FIELDS})
print(f"-> {MERGED_CSV}")


# ── 2. Patch WA features in the market-metrics overlay ────────────────────────
with open(GEOJSON, encoding="utf-8") as fh:
    doc = json.load(fh)

by_key = {norm(r["suburb"]): r for r in rows}
patched = nomatch = 0
for feat in doc["features"]:
    p = feat.setdefault("properties", {})
    # Drop the 3-month growth field from every feature — the layer is removed.
    p.pop("hvi_3", None)
    if p.get("state") != "WA":
        continue
    rec = by_key.get(norm(p.get("suburb", "")))
    if not rec:
        nomatch += 1
        continue
    if rec["median_house_price_aud"] is not None:
        p["avm_median"] = rec["median_house_price_aud"]
    if rec["median_house_rent_per_week_aud"] is not None:
        p["rent_cur"] = rec["median_house_rent_per_week_aud"]
    p["hvi_12"]  = rec["sales_price_growth_pct_yoy"]    # last-year price growth
    p["rent_1y"] = rec["rental_price_growth_pct_yoy"]   # last-year rent growth
    if rec["postcode"]:
        p["postcode"] = rec["postcode"]
    if rec["source_url"]:
        p["source_url"] = rec["source_url"]
    p["src"] = "REIWA-May2026"
    patched += 1

print(f"WA features patched: {patched} (no REIWA match: {nomatch})")

with open(GEOJSON, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, separators=(",", ":"))
print(f"-> {GEOJSON}: {os.path.getsize(GEOJSON)/1024:.1f} KB")

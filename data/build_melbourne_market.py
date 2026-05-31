"""Merge the four realestate.com.au (REA) "May 2026" Melbourne house-market CSV
exports into one clean file, then patch the Melbourne (VIC) suburbs in
market_metrics_suburb.geojson with those numbers.

Source exports (REA suburb-profile summaries, captured May 2026, all share one
schema — ranks 1-100 / 101-200 / 201-300 / 301-356):
  median_house_price_aud, median_house_rent_per_week_aud,
  house_gross_yield_pct, house_price_growth_12m_pct,
  house_price_growth_3m_pct (empty), house_price_growth_6m_pct (empty)

REA suburb profiles only expose 12-month (last-year) price growth — the 3m/6m
columns are always blank, and there is no rent-growth figure. So:
  * hvi_12  <- house_price_growth_12m_pct   (last-year price growth)
  * rent_1y -> None  (REA has no rent growth; blanked, per project decision)
  * hvi_3   -> dropped from every feature (the 3-month layer was removed)

Outputs:
  data/melbourne_house_market_rea_may2026.csv     — merged, deduplicated, clean
  static/overlays/market_metrics_suburb.geojson   — VIC features patched in place
"""
import csv, json, os, re, unicodedata

SRC_DIR = "data/melbourne_raw"
SRC_FILES = [
    "melbourne_suburbs_001_to_100_house_market_rea_may2026.csv",
    "melbourne_suburbs_101_to_200_house_market_rea_may2026.csv",
    "melbourne_suburbs_201_to_300_house_market_rea_may2026.csv",
    "melbourne_suburbs_301_to_356_house_market_rea_may2026.csv",
]
MERGED_CSV = "data/melbourne_house_market_rea_may2026.csv"
GEOJSON    = "static/overlays/market_metrics_suburb.geojson"

FIELDS = ["suburb", "postcode", "median_house_price_aud",
          "median_house_rent_per_week_aud", "house_gross_yield_pct",
          "house_price_growth_12m_pct", "source_url"]


def norm(s):
    """Normalise a suburb name for matching (ASCII, lowercase, no punctuation)."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if ord(c) < 128).lower()
    s = re.sub(r"\([^)]*\)", "", s)              # strip "(Vic.)" disambiguators
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
    for n in names:
        if n in row and str(row[n]).strip() != "":
            return row[n]
    return None


# ── 1. Read + merge ─────────────────────────────────────────────────────────
merged = {}  # norm(suburb) -> clean row dict
for fname in SRC_FILES:
    with open(os.path.join(SRC_DIR, fname), encoding="utf-8-sig", newline="") as fh:
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
                "house_gross_yield_pct": num(get(row, "house_gross_yield_pct")),
                "house_price_growth_12m_pct": num(get(row, "house_price_growth_12m_pct")),
                "source_url": (get(row, "source_url") or "").strip(),
            }
            prev = merged.get(key)
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


# ── 2. Patch VIC features in the market-metrics overlay ───────────────────────
with open(GEOJSON, encoding="utf-8") as fh:
    doc = json.load(fh)

by_key = {norm(r["suburb"]): r for r in rows}
patched = nomatch = 0
for feat in doc["features"]:
    p = feat.setdefault("properties", {})
    p.pop("hvi_3", None)                 # 3-month layer removed everywhere
    if p.get("state") != "VIC":
        continue
    rec = by_key.get(norm(p.get("suburb", "")))
    if not rec:
        nomatch += 1
        continue
    if rec["median_house_price_aud"] is not None:
        p["avm_median"] = rec["median_house_price_aud"]
    if rec["median_house_rent_per_week_aud"] is not None:
        p["rent_cur"] = rec["median_house_rent_per_week_aud"]
    p["hvi_12"]  = rec["house_price_growth_12m_pct"]   # last-year price growth
    p["rent_1y"] = None                                # REA exposes no rent growth
    if rec["house_gross_yield_pct"] is not None:
        p["yield_pct"] = rec["house_gross_yield_pct"]
    if rec["postcode"]:
        p["postcode"] = rec["postcode"]
    if rec["source_url"]:
        p["source_url"] = rec["source_url"]
    p["src"] = "REA-May2026"
    patched += 1

print(f"VIC features patched: {patched} (no REA match: {nomatch})")

with open(GEOJSON, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, separators=(",", ":"))
print(f"-> {GEOJSON}: {os.path.getsize(GEOJSON)/1024:.1f} KB")

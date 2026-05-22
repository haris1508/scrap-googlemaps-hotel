"""
Google Maps Hotel Scraper
Source: https://www.google.com/maps/search/hotel/

Extracts hotel data via Google Maps internal search API (/search?tbm=map).
No official API key required — uses same endpoint as the browser.

Fields extracted per hotel:
  place_id, name, latitude, longitude, address, city, country, timezone,
  categories, star_rating, rating, amenities, description, price_display,
  website, phone, google_maps_url

For individual reviews (text, date, author, score), see fetch_reviews() below.

Usage:
  python3 fetch.py --city Jakarta --lat -6.2 --lng 106.8 --radius 0.15
  python3 fetch.py --grid ID   # grid scan seluruh Indonesia
  python3 fetch.py --help
"""

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.google.com/maps/",
}

# pb parameter template — encodes: query, viewport center, zoom, and all
# data fields we want (rating, price, amenities, reviews, photos, etc.).
# Verified working; extra trailing params cause empty responses.
PB_TEMPLATE = (
    "!1shotel"
    "!4m8!1m3!1d{radius}!2d{lng}!3d{lat}"
    "!3m2!1i1024!2i768!4f13.1"
    "!7i20"
    "!10b1"
    "!12m53"
    "!1m5!18b1!30b1!31m1!1b1!34e1"
    "!2m4!5m1!6e2!20e3!39b1"
    "!6m25!49b1!63m0!66b1!74i150000!85b1!91b1"
    "!114b1!149b1!206b1!209b1!212b1!216b1!222b1!223b1"
    "!232b1!234b1!235b1!244b1!246b1!250b1!253b1!260b1"
    "!266b1!272b1!273b1"
    "!10b1!12b1!13b1!14b1!16b1"
    "!17m1!3e1"
    "!20m4!5e2!6b1!8b1!14b1"
    "!46m1!1b0!96b1!99b1"
    "!19m4!2m3!1i360!2i120!4i8"
)

# Predefined city grids [name, lat, lng, radius_degrees]
CITY_GRIDS = {
    "jakarta":   [("Jakarta Pusat",    -6.186,  106.820, 0.08),
                  ("Jakarta Selatan",  -6.261,  106.810, 0.08),
                  ("Jakarta Utara",    -6.140,  106.855, 0.08),
                  ("Jakarta Barat",    -6.190,  106.760, 0.08),
                  ("Jakarta Timur",    -6.215,  106.900, 0.08)],
    "bali":      [("Kuta",             -8.720,  115.170, 0.06),
                  ("Seminyak",         -8.690,  115.165, 0.05),
                  ("Ubud",             -8.508,  115.262, 0.07),
                  ("Nusa Dua",         -8.800,  115.232, 0.06),
                  ("Sanur",            -8.700,  115.263, 0.05),
                  ("Canggu",           -8.650,  115.130, 0.06)],
    "surabaya":  [("Surabaya Pusat",   -7.250,  112.750, 0.07),
                  ("Surabaya Timur",   -7.290,  112.810, 0.07)],
    "yogyakarta":[("Malioboro",        -7.793,  110.366, 0.06),
                  ("Prawirotaman",     -7.820,  110.373, 0.05)],
    "bandung":   [("Bandung Pusat",    -6.917,  107.619, 0.07),
                  ("Lembang",          -6.812,  107.617, 0.06)],
    "lombok":    [("Senggigi",         -8.487,  116.038, 0.07),
                  ("Kuta Lombok",      -8.884,  116.305, 0.06)],
    "ID":        None,  # special: full Indonesia grid (slow!)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(val):
    """Return empty string for None, else strip & return."""
    return "" if val is None else str(val).strip()


def _get_nested(obj, *keys, default=None):
    try:
        for k in keys:
            obj = obj[k]
        return obj
    except (IndexError, TypeError, KeyError):
        return default


def _parse_hotel(entry) -> dict | None:
    """
    Parse one raw hotel entry (260-field list) into a clean dict.
    Handles two response shapes:
      Shape A (US-style):  d[0][1][N][14]  → entry = that list
      Shape B (ID-style):  d[64][N][1]     → entry = that list
    Both use the same 260-field schema.
    """
    if not isinstance(entry, list) or len(entry) < 90:
        return None

    name = _safe(_get_nested(entry, 11))
    if not name:
        return None

    lat  = _get_nested(entry, 9, 2)
    lng  = _get_nested(entry, 9, 3)
    if lat is None or lng is None:
        return None

    # Rating
    rating = _get_nested(entry, 4, 7)

    # Address
    address = _safe(_get_nested(entry, 18))

    # Categories
    cats = _get_nested(entry, 13) or []
    categories = "|".join(c for c in cats if isinstance(c, str))

    # Star rating + amenities
    star_label = _safe(_get_nested(entry, 64, 3))
    raw_amenities = _get_nested(entry, 64, 2) or []
    amenities = "|".join(
        a[2] for a in raw_amenities
        if isinstance(a, list) and len(a) > 2 and isinstance(a[2], str)
    )

    # Description (short + long)
    desc_short = _safe(_get_nested(entry, 32, 0, 1) or _get_nested(entry, 32, 1, 1))
    desc_long  = _safe(_get_nested(entry, 161, 1))
    description = desc_long or desc_short

    # Price
    price_display = _safe(_get_nested(entry, 88, 0))

    # Website + phone
    website = _safe(_get_nested(entry, 7, 0))
    phone_raw = _get_nested(entry, 178, 0, 0, 0)
    phone = _safe(phone_raw)

    # Place ID (ChIJ…)
    place_id = _safe(_get_nested(entry, 78))

    # Country / timezone
    country  = _safe(_get_nested(entry, 243))
    timezone = _safe(_get_nested(entry, 30))
    city     = _safe(_get_nested(entry, 166))

    google_maps_url = (
        f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        if place_id else ""
    )

    return {
        "place_id":        place_id,
        "name":            name,
        "latitude":        lat,
        "longitude":       lng,
        "address":         address,
        "city":            city,
        "country":         country,
        "timezone":        timezone,
        "categories":      categories,
        "star_rating":     star_label,
        "rating":          rating,
        "amenities":       amenities,
        "description":     description,
        "price_display":   price_display,
        "website":         website,
        "phone":           phone,
        "google_maps_url": google_maps_url,
    }


def _parse_response(raw: bytes) -> list[dict]:
    """Strip XSSI prefix + parse JSON, then walk both known data shapes."""
    text = raw.decode("utf-8", errors="replace")
    if text.startswith(")]}'\n"):
        text = text[5:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    results = []

    # Shape A: data[0][1][N][14]
    try:
        listings = data[0][1]
        if isinstance(listings, list):
            for item in listings:
                if isinstance(item, list) and len(item) > 14:
                    entry = item[14]
                    parsed = _parse_hotel(entry)
                    if parsed:
                        results.append(parsed)
    except (IndexError, TypeError):
        pass

    # Shape B: data[64][N][1]
    try:
        listings2 = data[64]
        if isinstance(listings2, list):
            for item in listings2:
                if isinstance(item, list) and len(item) > 1:
                    entry = item[1]
                    parsed = _parse_hotel(entry)
                    if parsed and parsed["place_id"] not in {r["place_id"] for r in results}:
                        results.append(parsed)
    except (IndexError, TypeError):
        pass

    return results


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_hotels(session: requests.Session, lat: float, lng: float,
                 radius: float = 0.08) -> list[dict]:
    """Fetch hotels for one viewport tile.

    `radius` in degrees is used only for grid-spacing/dedup logic.
    The pb `1d` value is Google's internal Mercator viewport scale —
    63464 is the constant for zoom 13.1 + 1024×768; using a smaller value
    returns an empty response.
    """
    pb = PB_TEMPLATE.format(lat=lat, lng=lng, radius=63464)
    params = {
        "tbm":      "map",
        "authuser": "0",
        "hl":       "id",
        "gl":       "id",
        "q":        "hotel",
        "pb":       pb,
    }
    url = "https://www.google.com/search?" + urllib.parse.urlencode(params)
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        return _parse_response(resp.content)
    except requests.RequestException as e:
        print(f"  [WARN] request failed: {e}")
        return []


def fetch_reviews(session: requests.Session, place_id: str,
                  max_reviews: int = 20) -> list[dict]:
    """
    Fetch individual reviews for a place via batchexecute.
    Returns list of {place_id, author, rating, text, date_relative, date_iso}.

    NOTE: This uses Google's internal RPC — may break without notice.
    Rate-limit aggressively (>=1s between calls).
    """
    # RPC payload for reviews (pb field 3 = sort by newest)
    rpc_id = "UGCaj"
    req_data = json.dumps([[[rpc_id,
        json.dumps([[place_id], [2, max_reviews, None, None, 3]]),
        None, "generic"]]])

    params = {
        "rpcids":    rpc_id,
        "hl":        "id",
        "gl":        "id",
        "authuser":  "0",
        "f.sid":     "-1",
        "bl":        "boq_maps-frontend_20240101.00_p0",
        "rt":        "c",
    }
    url = "https://www.google.com/maps/_/MapsWizUi/data/batchexecute?" + urllib.parse.urlencode(params)
    headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
    body = "f.req=" + urllib.parse.quote(req_data) + "&at=&"

    try:
        resp = session.post(url, data=body, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [WARN] reviews request failed for {place_id}: {e}")
        return []

    text = resp.text
    if ")]}'" in text:
        text = text[text.index(")]}'")+5:]

    reviews = []
    try:
        # batchexecute returns multi-line envelope; extract inner JSON
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                outer = json.loads(line)
            except Exception:
                continue
            for chunk in (outer if isinstance(outer, list) else [outer]):
                if not isinstance(chunk, list) or len(chunk) < 3:
                    continue
                if chunk[0] != rpc_id:
                    continue
                inner_str = chunk[2]
                if not isinstance(inner_str, str):
                    continue
                inner = json.loads(inner_str)
                raw_reviews = _get_nested(inner, 2) or []
                for r in raw_reviews:
                    if not isinstance(r, list):
                        continue
                    author        = _safe(_get_nested(r, 0, 1))
                    review_rating = _get_nested(r, 4)
                    review_text   = _safe(_get_nested(r, 3))
                    date_relative = _safe(_get_nested(r, 1))
                    date_iso      = ""
                    ts = _get_nested(r, 2)
                    if isinstance(ts, (int, float)) and ts > 0:
                        import datetime
                        date_iso = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                    reviews.append({
                        "place_id":      place_id,
                        "author":        author,
                        "rating":        review_rating,
                        "text":          review_text,
                        "date_relative": date_relative,
                        "date_iso":      date_iso,
                    })
    except Exception as e:
        print(f"  [WARN] review parse error for {place_id}: {e}")

    return reviews


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

HOTEL_FIELDS = [
    "place_id", "name", "latitude", "longitude", "address",
    "city", "country", "timezone", "categories", "star_rating",
    "rating", "amenities", "description", "price_display",
    "website", "phone", "google_maps_url",
]

REVIEW_FIELDS = ["place_id", "author", "rating", "text", "date_relative", "date_iso"]


def _get_writer(path: Path, fields: list[str]):
    is_new = not path.exists()
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
    if is_new:
        # Write source comment + header
        fh.write(f"# source: https://www.google.com/maps/search/hotel/\n")
        writer.writeheader()
    return fh, writer


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def build_grid(lat_center: float, lng_center: float,
               radius_deg: float = 0.08) -> list[tuple]:
    """Single tile."""
    return [(lat_center, lng_center, radius_deg)]


def build_city_grid(city_key: str) -> list[tuple]:
    key = city_key.lower()
    if key not in CITY_GRIDS:
        raise ValueError(f"Unknown city key: {city_key}. Choose from: {list(CITY_GRIDS)}")
    tiles = CITY_GRIDS[key]
    if tiles is None:
        # Full Indonesia bounding box grid (0.3° tiles ≈ 33km)
        step = 0.3
        tiles = []
        for lat in [x / 10 for x in range(-110, 70, 3)]:   # -11 to 6.5
            for lng in [x / 10 for x in range(950, 1420, 3)]:  # 95 to 141
                tiles.append((lat, lng, 0.15))
    return [(lat, lng, r) for (name, lat, lng, r) in tiles] if tiles and len(tiles[0]) == 4 else tiles


def run(tiles: list[tuple], out_prefix: str, fetch_rev: bool = False,
        sleep_sec: float = 0.4):
    session = requests.Session()
    session.headers.update(HEADERS)

    hotels_path  = DATA_DIR / f"{out_prefix}_hotels.csv"
    reviews_path = DATA_DIR / f"{out_prefix}_reviews.csv"

    seen_ids: set[str] = set()

    # Load already-seen IDs (resume support)
    if hotels_path.exists():
        with open(hotels_path, encoding="utf-8") as f:
            for row in csv.DictReader(r for r in f if not r.startswith("#")):
                if row.get("place_id"):
                    seen_ids.add(row["place_id"])
        print(f"Resuming — {len(seen_ids)} hotels already saved")

    h_fh, h_writer = _get_writer(hotels_path, HOTEL_FIELDS)
    r_fh = r_writer = None
    if fetch_rev:
        r_fh, r_writer = _get_writer(reviews_path, REVIEW_FIELDS)

    total_new = 0
    try:
        for i, tile in enumerate(tiles):
            lat, lng, radius = tile
            print(f"[{i+1}/{len(tiles)}] tile lat={lat:.3f} lng={lng:.3f} ... ", end="", flush=True)
            hotels = fetch_hotels(session, lat, lng, radius)
            new_this_tile = 0
            for hotel in hotels:
                pid = hotel["place_id"]
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                h_writer.writerow(hotel)
                total_new += 1
                new_this_tile += 1

                if fetch_rev and r_writer:
                    time.sleep(1.0)
                    reviews = fetch_reviews(session, pid)
                    for rev in reviews:
                        r_writer.writerow(rev)

            h_fh.flush()
            if r_fh:
                r_fh.flush()
            print(f"{len(hotels)} found, {new_this_tile} new (total={total_new})")
            time.sleep(sleep_sec)

    finally:
        h_fh.close()
        if r_fh:
            r_fh.close()

    print(f"\nDone. {total_new} new hotels written to {hotels_path}")
    if fetch_rev:
        print(f"Reviews written to {reviews_path}")
    return total_new


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Google Maps Hotel Scraper")
    parser.add_argument("--city",    help="City key: jakarta|bali|surabaya|yogyakarta|bandung|lombok|ID")
    parser.add_argument("--lat",     type=float, help="Custom lat (requires --lng)")
    parser.add_argument("--lng",     type=float, help="Custom lng (requires --lat)")
    parser.add_argument("--radius",  type=float, default=0.08, help="Viewport radius in degrees (default 0.08 ≈ 9km)")
    parser.add_argument("--out",     default=None, help="Output file prefix (default: city key or 'custom')")
    parser.add_argument("--reviews", action="store_true", help="Also fetch individual reviews per hotel (slow)")
    parser.add_argument("--sleep",   type=float, default=0.4, help="Sleep between tile requests (default 0.4s)")
    args = parser.parse_args()

    if args.city:
        tiles = build_city_grid(args.city)
        prefix = args.out or args.city.lower()
    elif args.lat and args.lng:
        tiles = build_grid(args.lat, args.lng, args.radius)
        prefix = args.out or "custom"
    else:
        parser.error("Provide --city or both --lat and --lng")

    print(f"Starting scrape: {len(tiles)} tiles, prefix='{prefix}', reviews={args.reviews}")
    run(tiles, prefix, fetch_rev=args.reviews, sleep_sec=args.sleep)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Generate schema-lookup.json for Traverse Hospitality
=====================================================
Builds a single JSON file keyed by Guesty listing ID, with a schema.org
VacationRental block per listing, for the booktraverse.com direct-booking site.

Usage:
  cd ~/traverse-schema
  ./venv/bin/python3 generate_schema_lookup.py      # (no need to `source venv/bin/activate`)

Output: schema-lookup.json — push it to https://github.com/lebcwby/traverse-schema
(`main` branch). A monthly local scheduled task does this automatically; it only
needs doing when listings are added/removed in Guesty.

Data source: Guesty Open API (open-api.guesty.com). We intentionally do NOT use
the Booking Engine API (booking.guesty.com) here — it has a hard cap of 5 OAuth
tokens / 24h and burning it locks out the production website.

What each entry includes: name, canonical booktraverse.com URL + @id, description,
images, address (with Mt. Crested Butte corrections), geo, bed/bath/occupancy,
amenities, nightly Offer, and an aggregateRating computed from the listing's
Guesty reviews (Airbnb / VRBO / Booking.com, normalised to a 0–5 scale).

Scope: Colorado listings only (matches the public booktraverse.com inventory).
"""

import json
import os
import re
import sys
import logging

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Run: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

GUESTY_CLIENT_ID = os.getenv("GUESTY_CLIENT_ID", "")
GUESTY_CLIENT_SECRET = os.getenv("GUESTY_CLIENT_SECRET", "")
GUESTY_API_BASE = "https://open-api.guesty.com/v1"
GUESTY_AUTH_URL = "https://open-api.guesty.com/oauth2/token"
# Canonical site (the Next.js direct-booking site, not the Guesty booking engine).
SITE_PROPERTY_BASE = "https://www.booktraverse.com/properties"
ORG_ID = "https://booktraverse.com/#organization"
OUTPUT_FILE = "schema-lookup.json"

# Keep only Colorado listings (the public inventory). Guesty stores the state as
# either the full name or the 2-letter code.
COLORADO_STATES = {"colorado", "co"}

AMENITY_MAP = {
    "Wifi": "Wi-Fi", "Internet": "Wi-Fi", "Wireless Internet": "Wi-Fi",
    "Kitchen": "Kitchen",
    "Free parking on premises": "Free Parking", "Free street parking": "Free Parking",
    "Hot tub": "Hot Tub", "Pool": "Pool Access",
    "Air conditioning": "Air Conditioning", "Heating": "Heating",
    "Washer": "Washer", "Dryer": "Dryer",
    "TV": "Television", "Cable TV": "Cable TV",
    "Pets allowed": "Pet Friendly",
    "Ski-in/Ski-out": "Ski-In/Ski-Out",
    "EV charger": "EV Charging", "BBQ grill": "BBQ Grill",
    "Patio or balcony": "Patio/Balcony",
    "Mountain view": "Mountain View",
    "Indoor fireplace": "Fireplace",
    "Gym": "Fitness Center",
}

PET_AMENITIES = {"Pets allowed", "Pets Allowed", "PETS_ALLOWED"}

# Guesty labels on-mountain Crested Butte Mountain Resort properties as
# "Crested Butte / 81224" when they're actually in Mt. Crested Butte (81225).
# Correct the confident cases by street keyword (from the known buildings).
MT_CB_STREET_KEYWORDS = ("emmons", "snowmass", "gothic", "hunter hill", "mountaineer")
MT_CB_LOCALITY = "Mt. Crested Butte"
MT_CB_ZIP = "81225"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ── slug (mirrors src/lib/utils.ts: slugify + getListingSlug) ──────────────
def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    s = re.sub(r"^-+|-+$", "", s)
    return s[:60]


def listing_slug(title, gid):
    return f"{slugify(title)}-{gid}" if title else gid


# ── Guesty auth + listings ─────────────────────────────────────────────────
def get_token(session):
    log.info("Authenticating...")
    resp = session.post(GUESTY_AUTH_URL, data={
        "grant_type": "client_credentials",
        "scope": "open-api",
        "client_id": GUESTY_CLIENT_ID,
        "client_secret": GUESTY_CLIENT_SECRET,
    }, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    session.headers.update({"Authorization": f"Bearer {resp.json()['access_token']}"})
    log.info("Authenticated.")


def fetch_all_listings(session):
    fields = ("_id title nickname publicDescription.summary address pictures amenities "
              "accommodates bedrooms bathrooms prices.basePrice prices.currency isListed pms.active")
    listings, skip = [], 0
    while True:
        log.info(f"Fetching listings (skip={skip})...")
        resp = session.get(f"{GUESTY_API_BASE}/listings", params={
            "fields": fields, "limit": 100, "skip": skip, "sort": "-_id"
        })
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        for l in results:
            pms = l.get("pms") or {}
            active = l.get("isListed", False) or (pms.get("active", False) if isinstance(pms, dict) else False)
            if not active:
                continue
            state = ((l.get("address") or {}).get("state") or "").strip().lower()
            if state not in COLORADO_STATES:  # Colorado-only (matches the public site)
                continue
            listings.append(l)
        skip += 100
        if skip >= data.get("count", 0):
            break
    log.info(f"Kept {len(listings)} active Colorado listings.")
    return listings


# ── reviews → aggregateRating (Guesty Open API /reviews) ───────────────────
def normalize_rating(channel, raw):
    """Return a guest→listing overall rating on a 0–5 scale, or None."""
    c = (channel or "").lower()
    # Only count guest reviews of the property (skip host-reviews-of-guest).
    if raw.get("reviewer_role") and raw.get("reviewer_role") != "guest":
        return None
    if "airbnb" in c:
        r = raw.get("overall_rating")  # 1–5
        return float(r) if r else None
    if "homeaway" in c or "vrbo" in c:
        r = raw.get("starRatingOverall")  # 1–5 (string)
        try:
            return float(r) if r not in (None, "") else None
        except (TypeError, ValueError):
            return None
    if "booking" in c:
        r = (raw.get("scoring") or {}).get("review_score")  # 1–10
        return float(r) / 2 if r else None
    # Unknown channel — try the common fields before giving up.
    for k in ("overall_rating", "starRatingOverall"):
        if raw.get(k):
            try:
                return float(raw[k])
            except (TypeError, ValueError):
                pass
    return None


def fetch_rating(session, listing_id):
    """(ratingValue_0to5, reviewCount) for a listing, or None when no reviews."""
    ratings, skip = [], 0
    while True:
        resp = session.get(f"{GUESTY_API_BASE}/reviews",
                           params={"listingId": listing_id, "limit": 100, "skip": skip})
        if resp.status_code != 200:
            break
        data = resp.json().get("data", [])
        if not data:
            break
        for rv in data:
            val = normalize_rating(rv.get("channelId"), rv.get("rawReview") or {})
            if val is not None:
                ratings.append(val)
        skip += 100
        if len(data) < 100 or skip >= 1000:  # cap 1000 reviews/listing
            break
    if not ratings:
        return None
    return round(sum(ratings) / len(ratings), 2), len(ratings)


# ── schema builder ─────────────────────────────────────────────────────────
def correct_address(addr):
    street = (addr.get("street") or addr.get("full") or "")
    locality = addr.get("city", "")
    zipc = addr.get("zipcode", "")
    state = (addr.get("state") or "").strip()
    # Normalise region to the 2-letter code.
    region = "CO" if state.lower() in COLORADO_STATES else (state or "CO")
    # Fix on-mountain CB properties mislabelled as downtown "Crested Butte".
    s_lower = street.lower()
    if any(k in s_lower for k in MT_CB_STREET_KEYWORDS):
        locality = MT_CB_LOCALITY
        zipc = MT_CB_ZIP
    return {
        "@type": "PostalAddress",
        "streetAddress": street,
        "addressLocality": locality,
        "addressRegion": region,
        "postalCode": zipc,
        "addressCountry": "US",
    }


def build_schema(listing, rating):
    lid = listing.get("_id", "")
    title = listing.get("title") or listing.get("nickname") or "Vacation Rental"
    url = f"{SITE_PROPERTY_BASE}/{listing_slug(title, lid)}"

    pub = listing.get("publicDescription") or {}
    desc = (pub.get("summary") if isinstance(pub, dict) else str(pub or "")) or ""
    if len(desc) > 300:
        desc = desc[:297] + "..."

    images = []
    for p in (listing.get("pictures") or [])[:5]:
        if isinstance(p, dict):
            u = p.get("original") or p.get("thumbnail")
        else:
            u = p if isinstance(p, str) else None
        if u:
            images.append(u)

    raw_amen = listing.get("amenities") or []
    seen, amenities = set(), []
    for a in raw_amen:
        m = AMENITY_MAP.get(a)
        if m and m not in seen:
            seen.add(m)
            amenities.append({"@type": "LocationFeatureSpecification", "name": m, "value": True})

    addr = listing.get("address") or {}
    prices = listing.get("prices") or {}
    base_price = prices.get("basePrice")

    schema = {
        "@context": "https://schema.org",
        "@type": "VacationRental",
        "@id": f"{url}#vacationrental",
        "name": title,
        "url": url,
        "containedInPlace": {
            "@type": "LodgingBusiness",
            "name": "Traverse Hospitality",
            "@id": ORG_ID,
        },
        "checkinTime": "16:00",
        "checkoutTime": "10:00",
    }
    if desc:
        schema["description"] = desc
    if images:
        schema["image"] = images
    if addr.get("city"):
        schema["address"] = correct_address(addr)
    if addr.get("lat") is not None and addr.get("lng") is not None:
        schema["geo"] = {"@type": "GeoCoordinates", "latitude": addr["lat"], "longitude": addr["lng"]}
    if listing.get("bedrooms"):
        schema["numberOfBedrooms"] = listing["bedrooms"]
    if listing.get("bathrooms"):
        schema["numberOfBathroomsTotal"] = listing["bathrooms"]
    if listing.get("accommodates"):
        schema["occupancy"] = {"@type": "QuantitativeValue", "maxValue": listing["accommodates"], "unitText": "guests"}
    if amenities:
        schema["amenityFeature"] = amenities
    if set(raw_amen) & PET_AMENITIES:
        schema["petsAllowed"] = True
    if rating:
        rating_value, review_count = rating
        schema["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": rating_value,
            "reviewCount": review_count,
            "bestRating": 5,
            "worstRating": 1,
        }
    if base_price:
        schema["offers"] = {
            "@type": "Offer",
            "priceSpecification": {
                "@type": "UnitPriceSpecification",
                "price": base_price,
                "priceCurrency": prices.get("currency", "USD"),
                "unitText": "night",
            },
            "availability": "https://schema.org/InStock",
            "url": url,
            "seller": {"@id": ORG_ID},
        }
    return schema


def main():
    if not GUESTY_CLIENT_ID or not GUESTY_CLIENT_SECRET:
        log.error("Missing Guesty credentials in .env file.")
        sys.exit(1)

    session = requests.Session()
    get_token(session)
    listings = fetch_all_listings(session)

    log.info(f"Fetching reviews for {len(listings)} listings (this is the slow part)...")
    lookup, with_ratings = {}, 0
    for i, l in enumerate(listings, 1):
        lid = l.get("_id", "")
        rating = fetch_rating(session, lid)
        if rating:
            with_ratings += 1
        lookup[lid] = build_schema(l, rating)
        if i % 25 == 0:
            log.info(f"  ...{i}/{len(listings)} listings processed")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(lookup, f, separators=(",", ":"))

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    log.info(f"Generated {OUTPUT_FILE}: {len(lookup)} listings "
             f"({with_ratings} with aggregateRating), {size_kb:.0f} KB")
    log.info("NEXT STEP: push schema-lookup.json to the lebcwby/traverse-schema repo (main).")


if __name__ == "__main__":
    main()

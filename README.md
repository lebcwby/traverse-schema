# traverse-schema

Generates `schema-lookup.json` — a single JSON file keyed by Guesty listing ID,
holding a schema.org **VacationRental** block per property for the
[booktraverse.com](https://www.booktraverse.com) direct-booking site.

## What it does
`generate_schema_lookup.py` pulls Colorado listings from the **Guesty Open API**
and, for each, builds a VacationRental schema with:
name · canonical `booktraverse.com/properties/<slug>` URL + stable `@id` ·
description · images · address (with Mt. Crested Butte corrections) · geo ·
beds/baths/occupancy · amenities · nightly `Offer` · and an **`aggregateRating`**
computed from the listing's Guesty reviews (Airbnb / VRBO / Booking.com,
normalised to a 0–5 scale).

Scope is **Colorado only**, matching the public site inventory.

> Uses the Open API, **not** the Booking Engine API (`booking.guesty.com`) — that
> one caps OAuth at 5 tokens/24h and burning it locks out the production website.

## Run it
```bash
cd ~/traverse-schema
./venv/bin/python3 generate_schema_lookup.py
```
Needs `GUESTY_CLIENT_ID` / `GUESTY_CLIENT_SECRET` in `.env` (Open API creds).
The review fetch makes this take ~1–2 minutes.

## Publish
The served file is `schema-lookup.json` on this repo's **`main`** branch — just
commit the regenerated file. A **monthly local scheduled task** does this
automatically (only needed when listings are added/removed in Guesty).

The Python source lives here too (`generate_schema_lookup.py`); `.env` and `venv/`
are gitignored.

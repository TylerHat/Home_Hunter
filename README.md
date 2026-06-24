# Home Hunter — NYC rental tracker

Scrapes **NYC apartment rentals** from Craigslist into a structured, queryable,
**de-duplicated** database, run **locally on demand** and **100% free** — no
accounts, no paid services. Captures rent, beds/baths, **square footage**, and
**amenities** (laundry, parking, pets, no-fee), plus neighborhood, geolocation,
and a rent-history trend. Craigslist reposts of the same apartment (a fresh
posting id every day or two) are folded into a single listing instead of piling
up as duplicates. Suspected **fake/scam listings are flagged** (most decisively,
posts with **no photos**) so they're badged in the UI and can be filtered out.
Ships with a **read API and a built-in web UI** to browse and filter listings; an
optional manual cloud mode can run it off your home IP.

> **Why Craigslist?** It has little bot detection, so the scraper needs **no
> headless browser** — plain HTTP runs reliably on free CI runners. Zillow and
> StreetEasy have the richest NYC data but use aggressive PerimeterX protection
> that blocks free datacenter IPs. The scraping layer is isolated, so another
> source can be added later without touching the DB, pipeline, or API. The legacy
> Zillow scraper is kept under [src/home_hunter/scraper/zillow/](src/home_hunter/scraper/zillow/).

## How the constraints are met

| Constraint | How |
|---|---|
| 100% free | Local **SQLite** + open-source libs. No paid proxies, APIs, or databases. |
| Reliable | Craigslist needs no browser — just polite, paced HTTP with retries + exponential backoff. |
| Run off my IP (optional) | A manual GitHub Actions workflow can run the scrape on Microsoft-hosted runners. Local on-demand runs use your own IP, which is fine for Craigslist's light protection at modest volume. |

## Architecture

```
Local CLI, on demand   (optional: manual GitHub Actions)
   └─ scripts/run_scrape.py
        └─ home_hunter.pipeline.run()
             ├─ scraper.build_client()              # CraigslistClient (HTTP)
             ├─ scraper.craigslist.search_area()    # search pages -> detail pages
             │     └─ scraper.craigslist.parse      # HTML -> pydantic RentalListing
             └─ db.upsert_listings()                # upsert by pid + rent history
                   └─ Neon Postgres (or SQLite)
                         ▲
                         └─ api/app.py (FastAPI read API)
                               └─ api/static/index.html  ← built-in web UI at /
```

Per borough: page through `/search/<area>/apa` (apartments for rent), then open
each listing's detail page to capture square footage + amenities. Set
`detail_fetch: false` to skip detail pages (search-page fields only — much faster).

## Quick start (local)

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

# 1. Pick your area
#    edit config.yaml -> areas (boroughs) and filters (rent, beds, pets)

# 2. Dry run (scrape + print, no DB write). --no-detail = fast (no detail pages).
python scripts/run_scrape.py --area mnh --once --no-detail --dry-run

# 3. Real run -> writes to a local SQLite file (home_hunter.db) by default
python scripts/run_scrape.py --area mnh --once

# 4. Browse the data in the web UI (or the API)
uvicorn home_hunter.api.app:app --reload --app-dir src
#    UI:        http://127.0.0.1:8000/
#    API docs:  http://127.0.0.1:8000/docs
```

By default (no `DATABASE_URL` set) data goes to a local **SQLite** file, so you
can try everything with zero setup. Set `DATABASE_URL` to use Neon Postgres.

## Configuration

All settings live in [config.yaml](config.yaml):

- `areas` — Craigslist NYC borough codes: `mnh` (Manhattan), `brk` (Brooklyn),
  `que` (Queens), `brx` (Bronx), `stn` (Staten Island).
- `filters` — `min_rent`/`max_rent`, `min_beds`/`max_beds`, `cats_ok`/`dogs_ok`,
  and `max_pages` (search pages per borough, ~120 listings each).
- `detail_fetch` — open each listing for sqft + amenities (`true`) or not (`false`).
- `rate_limit` — delays, retries, backoff, the request `user_agent`, and
  `detail_concurrency` (detail pages fetched in parallel; each worker keeps its
  own pacing, so a full detail run finishes ~Nx faster while staying polite).
- `flags` — scam-detection weights/thresholds (see below). Photos dominate:
  `no_photo_weight` reaches `threshold` on its own, so a photoless post is
  flagged. `market_ratio` flags rent far below its `(borough, beds)` median.

## Database schema

- **`rentals`** — keyed by Craigslist `pid`; title, neighborhood
  (`neighborhood` = the source's free-text label, `neighborhood_key` = the
  canonical neighborhood resolved from lat/long), borough, rent,
  beds, baths, sqft, housing type, text attributes (`laundry`, `parking`,
  `rent_period`), boolean amenity flags (`cats_ok`, `dogs_ok`, `furnished`,
  `no_smoking`, `wheelchair_accessible`, `air_conditioning`, `ev_charging`,
  `no_fee`), a catch-all `amenities` JSON list, lat/long, url,
  `posted_at`/`updated_at`, and `first_seen`/`last_seen`/`last_scraped`.
  Scam-detection fields: `image_count` (photos on the detail page; `0` is the
  strongest scam signal), `flagged`, and `flag_reasons` (e.g. `["no photos"]`).
- **`rent_history`** — a row is appended only when a listing's rent changes, so the
  DB becomes a rent-trend analysis asset over time.

Each run upserts on `pid`: update existing, insert new, append rent history on
change, and flag suspected scams (re-checked against area medians at the end of
the run). Re-run scoring on stored rows with
`python scripts/recompute_flags.py` (e.g. after tuning `flags:` thresholds);
note the photo signal only applies to rows scraped since the feature landed.

## Web UI & query API

`uvicorn home_hunter.api.app:app --app-dir src` serves both a UI and a read API:

- `GET /` — a self-contained web page (no build step) with a filter form,
  listing cards/table, and an **Advanced Neighborhood Selection** map: click
  "🗺️ Advanced Neighborhood Selection" next to Borough to open an interactive
  map of NYC neighborhoods (rendered as inline SVG — no map library, works
  offline), click neighborhoods like *Upper East Side*, *Williamsburg*, or
  *Flatiron District* to toggle them, and the listing table/cards filter to that
  selection. Shading shows how many listings each neighborhood has.
- `GET /rentals` — filter by `borough`, `neighborhood` (repeatable — the map
  filter; matches `neighborhood_key`), `min_rent`, `max_rent`, `min_beds`,
  `max_beds`, `min_sqft`, `housing_type`, `cats_ok`, `dogs_ok`, `no_fee`, and
  `hide_flagged` (drop suspected scams), with `limit`/`offset`. Results are
  ordered by rent ascending (nulls last). Each listing carries `image_count`,
  `flagged`, and `flag_reasons`; the UI shows a **⚠ possible scam** badge and a
  **Hide suspected scams** filter checkbox.
- `GET /rentals/{pid}` and `GET /rentals/{pid}/rent-history`.
- `GET /stats` — totals, min/avg/max rent, and per-borough + per-neighborhood
  counts (powers the UI header and the map shading).
- `GET /neighborhoods.geojson` — NYC neighborhood boundaries the map renders.
- `GET /health`.

The API is also the contract a richer future UI (listing map pins, score
sorting) will call.

## Optional: run on GitHub instead of locally

Not required — the project runs fine locally. This is only if you want scrapes to
run off your home IP on GitHub's infrastructure. The workflow is **manual-only**
(no schedule) and needs a persistent database to keep its results:

1. **Create a free Neon Postgres database** at <https://neon.tech>. Copy its
   connection string and convert the driver prefix to:
   `postgresql+psycopg://USER:PASSWORD@HOST/neondb?sslmode=require`
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, name `DATABASE_URL`, value = the Neon URL above.
3. Run it on demand: **Actions → NYC rental scrape (manual) → Run workflow**.

Without a `DATABASE_URL` secret, a cloud run writes to a throwaway SQLite file
that is discarded when the runner stops — so the secret is what makes cloud data
persist.

## Tests

```bash
pip install pytest
pytest            # offline: Craigslist search/detail parsing + upsert/rent-history
```

Tests never hit the network — they run against saved HTML fixtures in
`tests/fixtures/` and in-memory SQLite.

## Legal / Terms of Service

Craigslist's Terms prohibit automated access. This project keeps volume minimal
and is intended for personal NYC-rental analysis. You are responsible for how you
use it; review Craigslist's Terms before going beyond personal use.

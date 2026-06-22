# Home Hunter — NYC rental tracker

Scrapes **NYC apartment rentals** from Craigslist into a structured, queryable
database, refreshed daily — **without using your home IP** and **100% free**.
Captures rent, beds/baths, **square footage**, and **amenities** (laundry,
parking, pets, no-fee), plus neighborhood, geolocation, and a rent-history trend.
Backend + database + read API (the search/filter UI is a later phase).

> **Why Craigslist?** It has little bot detection, so the scraper needs **no
> headless browser** — plain HTTP runs reliably on free CI runners. Zillow and
> StreetEasy have the richest NYC data but use aggressive PerimeterX protection
> that blocks free datacenter IPs. The scraping layer is isolated, so another
> source can be added later without touching the DB, pipeline, or API. The legacy
> Zillow scraper is kept under [src/home_hunter/scraper/zillow/](src/home_hunter/scraper/zillow/).

## How the constraints are met

| Constraint | How |
|---|---|
| Don't use my IP | The daily job runs on **GitHub Actions** (Microsoft-hosted runners), never your machine. |
| 100% free | GitHub Actions free minutes + **Neon** Postgres free tier (or local SQLite) + open-source libs. No paid proxies/APIs. |
| Reliable | Craigslist needs no browser — just polite, paced HTTP with retries + exponential backoff. |

## Architecture

```
GitHub Actions (daily cron)
   └─ scripts/run_scrape.py
        └─ home_hunter.pipeline.run()
             ├─ scraper.build_client()              # CraigslistClient (HTTP)
             ├─ scraper.craigslist.search_area()    # search pages -> detail pages
             │     └─ scraper.craigslist.parse      # HTML -> pydantic RentalListing
             └─ db.upsert_listings()                # upsert by pid + rent history
                   └─ Neon Postgres (or SQLite)
                         ▲
                         └─ api/app.py (FastAPI read API)  ← future UI consumes this
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

# 4. Browse the data via the API
uvicorn home_hunter.api.app:app --reload --app-dir src
#    open http://127.0.0.1:8000/docs
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
- `rate_limit` — delays, retries, backoff, and the request `user_agent`.

## Database schema

- **`rentals`** — keyed by Craigslist `pid`; title, neighborhood, borough, rent,
  beds, baths, sqft, housing type, amenity columns (`laundry`, `parking`,
  `cats_ok`, `dogs_ok`, `furnished`, `no_smoking`, `wheelchair_accessible`,
  `air_conditioning`, `ev_charging`, `no_fee`), a catch-all `amenities` JSON list,
  lat/long, url, `posted_at`/`updated_at`, and `first_seen`/`last_seen`/`last_scraped`.
- **`rent_history`** — a row is appended only when a listing's rent changes, so the
  DB becomes a rent-trend analysis asset over time.

Daily runs upsert on `pid`: update existing, insert new, append rent history on change.

## Query API

`uvicorn home_hunter.api.app:app --app-dir src` exposes:

- `GET /rentals` — filter by `borough`, `min_rent`, `max_rent`, `min_beds`,
  `max_beds`, `min_sqft`, `housing_type`, `cats_ok`, `dogs_ok`, `no_fee`, with
  `limit`/`offset`.
- `GET /rentals/{pid}` and `GET /rentals/{pid}/rent-history`.
- `GET /health`.

This is the contract the future search UI will call.

## Free production setup (daily, off your IP)

1. **Create a free Neon Postgres database** at <https://neon.tech>. Copy its
   connection string and convert the driver prefix to:
   `postgresql+psycopg://USER:PASSWORD@HOST/neondb?sslmode=require`
2. **Push this repo to GitHub.**
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, name `DATABASE_URL`, value = the Neon URL above.
4. The workflow in [.github/workflows/scrape.yml](.github/workflows/scrape.yml)
   runs daily. Trigger it manually first: **Actions → Daily NYC rental scrape →
   Run workflow**, and watch the logs.

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

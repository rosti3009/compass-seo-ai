# Compass SEO AI MVP

A production-ready FastAPI SEO MVP for crawling and auditing `https://compassgrill.co.il`.

## Features

- FastAPI backend with health, dashboard, crawler, latest results, and stats endpoints.
- SQLite persistence via SQLAlchemy.
- Bounded same-domain SEO crawler for titles, meta descriptions, H1s, canonicals, word count, links, and scores.
- Google Search Console and GA4 wrapper stubs with clear missing credential errors.
- Container-ready Dockerfile and Docker Compose setup.
- Pytest and Ruff configuration.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

## API endpoints

- `GET /health` — service health.
- `GET /` — HTML dashboard.
- `POST /crawler/run` — run a crawl for the configured target domain.
- `GET /crawler/latest` — latest crawl and page audit results.
- `GET /stats` — aggregate crawl stats.
- `GET /integrations/gsc/status` — validate GSC configuration.
- `GET /integrations/ga4/status` — validate GA4 configuration.

## Google credentials

The MVP validates configuration before any live Google API call. Set these variables in `.env`:

```bash
GOOGLE_SERVICE_ACCOUNT_FILE=/app/secrets/google-service-account.json
GSC_SITE_URL=https://compassgrill.co.il/
GA4_PROPERTY_ID=123456789
```

Mount the service account JSON file securely in production. Do not commit credentials.

## Development checks

```bash
pytest
ruff check .
```

## Docker

```bash
docker compose up --build
```

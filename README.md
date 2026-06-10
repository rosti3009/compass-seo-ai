# Compass SEO AI MVP

Compass SEO AI is a FastAPI SEO workflow app for crawling and auditing a configured domain, creating SEO tasks from crawl gaps, and preparing AI-assisted recommendations and article exports.

## What the app includes

- FastAPI backend with health, dashboard, crawler, latest crawl, stats, SEO task, and integration status endpoints.
- SQLite persistence via SQLAlchemy by default.
- Bounded same-domain SEO crawler for titles, meta descriptions, H1s, canonicals, word count, links, missing fields, and SEO scores.
- Dashboard pages for crawl health, SEO tasks, internal link opportunities, topical clusters, generated article previews, and CMS export payloads.
- OpenAI integration for SEO recommendations, article packages, internal link refinements, and topical clusters when `OPENAI_API_KEY` is configured.
- Google Search Console and GA4 configuration checks using connected Google OAuth first, with service account credentials as fallback.
- Container-ready Dockerfile and Docker Compose setup.
- Pytest, Ruff, and compile checks for deployment readiness.

## Required environment variables

Copy `.env.example` to `.env` for local development and configure these values in Render for deployment.

| Variable | Required | Purpose | Example / notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | Yes | SQLAlchemy database URL. | Local Docker default: `sqlite:///./data/compass_seo.db`. Render persistent disk example: `sqlite:////var/data/compass_seo.db`. |
| `TARGET_DOMAIN` | Yes | Domain the crawler audits. | `https://compassgrill.co.il` |
| `OPENAI_API_KEY` | Yes for AI generation endpoints | OpenAI API key used by SEO recommendation and content generation flows. | Set as a secret env var; do not commit it. |
| `OPENAI_MODEL` | Yes for AI generation endpoints | OpenAI model name used by the app. | `gpt-4o-mini` |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | Optional Google fallback | Complete Google service account JSON stored directly as an env var. | Used only when no Google OAuth token is connected. Preferred on Render for service-account fallback because it avoids committing or mounting secret files. |
| `GOOGLE_OAUTH_CLIENT_ID` | Yes for user OAuth | Google Cloud OAuth web client ID. | Required for `/auth/google/start`. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes for user OAuth | Google Cloud OAuth web client secret. | Store as a secret env var; do not commit it. |
| `GOOGLE_OAUTH_REDIRECT_URI` | Yes for user OAuth | Authorized redirect URI configured in Google Cloud. | Render example: `https://your-service.onrender.com/auth/google/callback`; local example: `http://127.0.0.1:8000/auth/google/callback`. |
| `GSC_SITE_URL` | Yes for GSC status | Verified Google Search Console property URL. | `https://compassgrill.co.il/` |
| `GA4_PROPERTY_ID` | Yes for GA4 status | Numeric GA4 property ID. | `123456789` |
| `ISTORE_BASE_URL` | Yes for ISTORE endpoints | Base URL for the ISTORE read-only API. | `https://example.istore.local/api/` |
| `ISTORE_COMPANY_ID` | Yes for ISTORE endpoints | Company identifier sent with ISTORE product reads. | `12345` |
| `ISTORE_X_TOKEN` | Yes for ISTORE endpoints | ISTORE API token sent as `X-Token`; responses redact it. | Set as a secret env var; do not commit it. |

Additional supported settings:

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_NAME` | FastAPI app title. | `Compass SEO AI` |
| `ENVIRONMENT` | Deployment environment label. | `development` |
| `LOG_LEVEL` | Application log level. | `INFO` |
| `CRAWLER_MAX_PAGES` | Maximum pages per crawl. | `25` |
| `CRAWLER_TIMEOUT_SECONDS` | Per-request crawler timeout. | `10` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Optional local file-path alternative to `GOOGLE_APPLICATION_CREDENTIALS_JSON`. | Empty |
| `MANUAL_ACTION_TOKEN` | Optional extra protection for manual write/sync endpoints; when set, send it as `X-Manual-Action-Token`. | Empty |
| `ISTORE_TIMEOUT_SECONDS` | Timeout for ISTORE read-only API calls. | `10` |

## Local run instructions

### Native Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your target domain and secrets.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://127.0.0.1:8000>.

For development reloads, use:

```bash
uvicorn app.main:app --reload
```

### Docker Compose

```bash
cp .env.example .env
# Edit .env before starting the app.
docker compose up --build
```

Docker Compose builds the included Dockerfile, loads `.env`, publishes container port `8000` to local port `8000`, and mounts `./data` to `/app/data` so the default SQLite database persists between container restarts.

## Render deployment instructions

Use these settings for a Render Web Service deployment:

1. Create a new Render Web Service from the repository.
2. Use the Python runtime or deploy from the included Dockerfile.
3. Set all required environment variables listed above in the Render dashboard.
4. For SQLite persistence, add a Render persistent disk and set `DATABASE_URL` to a disk-backed absolute path such as `sqlite:////var/data/compass_seo.db`. Without a persistent disk, SQLite data can be lost when instances restart or redeploy.
5. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REDIRECT_URI` for user OAuth.
6. Optionally store service-account fallback credentials in `GOOGLE_APPLICATION_CREDENTIALS_JSON` as the full service account JSON value. Do not commit credentials to the repository.
7. Deploy, verify `GET /health` returns `{"status":"ok"}`, then open `/auth/google/start` to connect a Google account.

Recommended Render start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

The app was also verified locally with the fixed-port command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Google OAuth setup

Use user OAuth when service-account access to Search Console is hard to grant. The app prefers a stored Google OAuth token for Search Console and GA4 calls, then falls back to `GOOGLE_APPLICATION_CREDENTIALS_JSON` or `GOOGLE_SERVICE_ACCOUNT_FILE` when no user token is connected.

1. In Google Cloud Console, select or create the project that has access to Search Console and GA4.
2. Configure the OAuth consent screen for the project. Add the user account that will connect the app as a test user if the app is in testing mode.
3. Enable the Google Search Console API and Google Analytics Data API for the project.
4. Create an OAuth client ID with application type **Web application**.
5. Add the authorized redirect URI exactly as it will be sent by the app, for example `https://your-service.onrender.com/auth/google/callback` in Render or `http://127.0.0.1:8000/auth/google/callback` locally.
6. Copy the OAuth client ID, client secret, and redirect URI into `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REDIRECT_URI`.
7. Deploy or restart the app, then open `/auth/google/start` and approve the requested scopes: Search Console readonly and Analytics readonly.
8. Confirm `GET /auth/google/status` returns `connected: true` with the granted scopes.

Required OAuth scopes:

- `https://www.googleapis.com/auth/webmasters.readonly`
- `https://www.googleapis.com/auth/analytics.readonly`

## Main API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service health check. |
| `GET` | `/` | HTML dashboard. |
| `POST` | `/crawler/run` | Crawl the configured `TARGET_DOMAIN`. |
| `GET` | `/crawler/latest` | Latest crawl run and page audit results. |
| `GET` | `/stats` | Aggregate crawl stats. |
| `POST` | `/seo/tasks/from-latest-crawl` | Create SEO tasks from the latest crawl's low-score or missing-basic pages. |
| `GET` | `/seo/tasks` | List saved SEO tasks as JSON. |
| `POST` | `/seo/tasks/{task_id}/generate-recommendation` | Generate and persist an OpenAI SEO recommendation for a task. |
| `POST` | `/seo/tasks/{task_id}/generate-article` | Generate and persist an OpenAI article package for a task. |
| `GET` | `/seo/tasks/{task_id}/export` | Return CMS-copyable article export JSON. |
| `GET` | `/seo/internal-link-opportunities` | Return internal linking opportunities from the latest crawl. |
| `GET` | `/seo/topical-clusters` | Return topical cluster suggestions from the latest crawl. |
| `GET` | `/auth/google/start` | Redirect to Google OAuth consent for Search Console and GA4 readonly scopes. |
| `GET` | `/auth/google/callback` | Store the Google OAuth token returned by Google. |
| `GET` | `/auth/google/status` | Report whether Google OAuth is connected and list stored scopes. |
| `GET` | `/integrations/gsc/status` | Validate Search Console configuration. |
| `POST` | `/gsc/manual-sync` | Manually import the last 30 days of query/page/date Search Console rows for `sc-domain:compassgrill.co.il`; body confirmation must be `SYNC sc-domain:compassgrill.co.il`, and `X-Manual-Action-Token` is required when `MANUAL_ACTION_TOKEN` is configured. |
| `GET` | `/integrations/ga4/status` | Validate GA4 configuration. |
| `GET` | `/integrations/istore/status` | Validate ISTORE read-only configuration with token redacted. |
| `GET` | `/integrations/istore/products` | Fetch ISTORE products with a GET-only read. |
| `GET` | `/integrations/istore/products/{product_id}` | Fetch one ISTORE product with a GET-only read. |
| `GET` | `/sitemap/discover` | Discover sitemap URLs for the configured target domain. |

## Dashboard URLs

| URL | Purpose |
| --- | --- |
| `/` | Main dashboard with latest crawl context, metrics, and workflow links. |
| `/seo/tasks-view` | HTML SEO task review and generation workflow. |
| `/seo/internal-link-opportunities-view` | HTML internal linking opportunities. |
| `/seo/topical-clusters-view` | HTML topical cluster strategy. |
| `/auth/google/start` | Connect a Google account for Search Console and GA4 OAuth access. |
| `/seo/tasks/{task_id}/preview` | Rendered preview for a generated article. |
| `/seo/tasks/{task_id}/export-view` | CMS-copyable export view for generated article HTML and schema payloads. |

## Recommended workflow

1. Start the app and open `/`.
2. Run `POST /crawler/run` from an API client or the dashboard controls to crawl `TARGET_DOMAIN`.
3. Review `/crawler/latest` or the dashboard for crawl status, scores, and missing fields.
4. Run `POST /seo/tasks/from-latest-crawl` to create tasks from pages with low scores or missing SEO basics.
5. Review tasks at `/seo/tasks-view`.
6. Configure `OPENAI_API_KEY` and `OPENAI_MODEL`, then generate recommendations for priority tasks.
7. Generate article packages only for approved tasks.
8. Review `/seo/internal-link-opportunities-view` and `/seo/topical-clusters-view` for site architecture improvements.
9. Use `/seo/tasks/{task_id}/export-view` to copy generated content and schema into the CMS.
10. Re-crawl after publishing changes and compare updated stats.

## Production readiness notes

- The Dockerfile installs `requirements.txt`, runs as a non-root `appuser`, exposes port `8000`, and starts `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml` is compatible with the Dockerfile, loads `.env`, maps `8000:8000`, and persists local SQLite data through the `./data:/app/data` volume.
- Render deployments should prefer secret environment variables over files. `GOOGLE_APPLICATION_CREDENTIALS_JSON` is supported for Google service account JSON on Render; `GOOGLE_SERVICE_ACCOUNT_FILE` remains available for local file-mounted credentials.
- The default database is SQLite. For production durability on Render, use a persistent disk or configure an external database URL and include the required database driver.
- The app creates database tables at startup through the FastAPI lifespan hook.
- AI generation endpoints require valid OpenAI credentials; non-AI crawler, dashboard, stats, and task-listing flows can run without an OpenAI key.

## Development checks

Run the deployment-readiness checks before opening a PR:

```bash
python -m ruff check .
python -m pytest
python -m compileall app tests
```

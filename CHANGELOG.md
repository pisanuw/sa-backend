# Changelog

All notable changes to the backend. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each entry that needs a
database change lists the migration to run **before** deploying (`make migrate`).

## [Unreleased]

### Added
- CI (GitHub Actions): the unit tests on Python 3.12 on every push and pull request, installed
  without the image pipeline's machine-learning packages.
- Chart image upload: a picture of a chart becomes an editable data table, a suggested chart type
  and a Vega-Lite spec.
  - `POST /jobs/extract-chart` (signed in) and `POST /guest/extract-chart` (no account);
    `GET /chart-extraction/options` reports which readers this deployment has, so the page offers
    only real choices.
  - The built-in reader is UniChart, a separate service (`services/unichart/`, set `UNICHART_URL`,
    optional `UNICHART_TOKEN`, optional `CHART_EXTRACTOR=unichart|llm`). No third party sees the
    image. When its result looks incomplete (several series, a pie, nothing numeric) and the person
    has agreed for that upload, the AI model (OpenRouter) is used instead; without agreement the
    built-in result comes back with a warning. A request for the AI model without agreement is
    refused. The guest route never calls the AI model.
  - Results are validated like any other chart table and marked as estimated unless the chart
    printed its values.
  - Not deployed yet: without `UNICHART_URL`, `POST /guest/extract-chart` answers 503 and
    `/chart-extraction/options` reports `local: false`.
- Image store: uploaded originals live in one table (`stem_images`) and are referenced by id from
  flowcharts and long descriptions, with `image_public` still deciding who may see them. Legacy
  inline `image_data_url` columns are kept for now. **Migrations `ee8798c827c8` and `b7d2e4f19a60` —
  run `make migrate` before deploying.**
- Job image retention: the source image of an upload job is deleted after the job finishes, or kept
  only when the person asked. `flask purge-job-images` removes expired ones and
  `flask migrate-images [--dry-run]` moves legacy inline images into `stem_images`.
- `CORS_ORIGIN_PATTERNS`: https origin patterns where `*` matches `a-z`, `0-9` and `-` only
  (for example `https://frontend-git-*-stem-accessibility.vercel.app` for Vercel preview
  branches). A pattern needs a literal domain after the last `*`; others are ignored.
  `tests/test_cors_patterns.py`.
- `tests/test_chart_extract.py`.
- Chart permalinks: `POST/GET /charts`, `GET/PUT/DELETE /charts/<chart_id>`,
  `PUT /charts/<chart_id>/sharing`, and the public `GET /c/<slug>`. A chart is its table plus a
  whitelisted config (chart type, columns, title, axis labels, category order); the server bounds and
  validates both (up to 5000 rows and 100 columns, 2 MB, 200 charts per account). Shared by link by
  default; `"public"` is accepted and stored, but there is no chart listing yet, so a public chart is
  reachable only by its link, like `"link"`. Table `charts`,
  **migrations 0005 and 0006 (`spec_json`) — run `make migrate` before deploying**.
- Charts can carry an optional Vega-Lite `spec`, returned with the chart and on the public link so a
  shared chart is described the way its author described it. It is stored as-is and never executed by the
  server, and only accepted if it is a Vega-Lite object up to 200 KB and 40 levels deep that contains no
  `url` anywhere (so it cannot make a viewer's browser fetch anything).
- `Create_Chart` research event type (the frontend logs it when someone chooses Chart on /create; unknown
  event types are a 400).
- `tests/test_charts.py`.

### Changed
- The worker loads the image pipeline (torch, YOLO, Google Vision) only when it runs a flowchart
  upload. Chart, summary and test jobs, and the unit tests, no longer need those packages
  installed. Before, every inline job imported them, so an install without them (a contributor
  setup, the test suite) could not run chart jobs either.

### Removed
- `POST /upload`, `POST /generate-summary` and `POST /generate-outline`. They ran the paid image pipeline
  (OCR, detection, LLM) and an LLM call with no sign-in and no rate limit, and nothing in the frontend called
  them (it uses the signed-in `POST /jobs/process-upload` and `POST /jobs/generate-summary`). Anything outside
  the app that called them directly must switch to the `/jobs/...` routes and send a Bearer token.

### Security
- Images sent to the AI model for chart extraction need the person's agreement for that upload; the
  server refuses without it, and guests (no account) can never reach the AI model.
- Rate limits no longer trust a forged `X-Forwarded-For`. The address is now the one the platform appended
  (counted from the right; one trusted hop on Cloud Run, none elsewhere; `TRUSTED_PROXY_HOPS` overrides).
  Before, the leftmost entry was used, so any caller could get a fresh limit with every request. This applies
  to the guest write limits, the report limit and the long-description limit.
- `POST /longdesc` with an image now needs a signed-in account (text-only saves still do not), with a
  per-account hourly limit and a cap of 100 stored images.

### Fixed
- `POST /longdesc` returned 500 for every request: `long_descriptions.owner_id` is NOT NULL and the route
  never set it. It now stores a placeholder owner (`anonymous`; the /ld page has no sign-in). Because the
  endpoint is open to anyone, it also validates sizes (alt text 2,000 characters, description 50,000, title
  255, image 5 MB as a data: URL, slug 3-64 letters, digits, `-` or `_`), is limited to 30 saves per
  address per hour, and no longer returns SQL in its error responses (here and on `GET /longdesc/<slug>`).
  `tests/test_longdesc.py`.

## [0.3.0] — 2026-10-11

Deployed to Cloud Run (us-west1) from commit 7fdad86. Migration 0004 (`reports`) was applied first.
Verified on the live service: `GET /guest/image` answers 400 without a token and 200 with one, a report for
an unknown page answers 404, the CORS preflight allows `X-Guest-Token`, and `GET /repo` answers 200.

### Added
- Try it (no account): `GET/PUT/DELETE /guest/image` and `POST /guest/claim`. A browser token
  (`X-Guest-Token`, hashed into `guest:<hash>`) owns one public image; guests cannot use uploads or AI, always
  share publicly, and are limited in size (200 nodes, 400 edges, text lengths) and rate (per token and per IP,
  per process). `claim` moves the image into an account after sign-up. Responses include `guest: true`.
- `POST /p/<slug>/report` stores a report (table `reports`, **migration 0004 — run `make migrate` before
  deploying**); `flask takedown <slug>` unpublishes and hides a page.
- CORS allows the `X-Guest-Token` header.
- `tests/test_guest.py`.

## [0.2.0] — 2026-10-11

Deployed to Cloud Run (us-west1) from commit 47801cb. The database was already at migration 0003, so
`make migrate` was a no-op. Verified: `PUT /flowcharts/<id>/sharing` answers 401 without a token and
`GET /repo` answers 200.

### Added
- `PUT /flowcharts/<id>/sharing` with `{"sharing": "public" | "link" | "private", "image_public": bool}`:
  one setting that sets visibility and publication together (public = page + listed, link = page only,
  private = owner only). `POST /flowcharts` accepts `sharing`; responses include `sharing` and `image_public`.
- `image_public` column: the uploaded source image is served on public pages only when the owner opts in.
  **Migration 0003 — run `make migrate` before deploying.** It backfills `image_public = true` for rows that
  are already published, so existing public pages are unchanged.
- `tests/test_sharing.py` (`python -m unittest tests.test_sharing`).

### Changed
- `POST /publish/<id>` and `POST /research/<id>` (deprecated) now use the same helper. Setting
  visibility to public through `/research` also publishes, so a diagram can no longer be public but
  invisible. They keep working for the deployed frontend until the rename (issue #1).
- `GET /p/<slug>` reports `has_image` only when the source image is shared.

### Added
- `AUTH_MODE=dev`: run the API without a Supabase project (fixed local user, SQLite).
  Refused when `K_SERVICE` is set, so it cannot start on Cloud Run.

### Changed
- Documentation uses the project name "STEM-Accessibility". The `ACCESSFLOW_OPENROUTER_API_KEY`
  variable and the `accessflow-backend` Cloud Run service keep their names.

### Added
- `LICENSE` (University of Washington).

## [0.1.0] — 2026-10-11

Deployed to Cloud Run (us-west1) as revision `accessflow-backend-00003-hs9`.
Database: stamped at migration `0002`.

### Added
- Cloud Run deployment: `Dockerfile` (CPU-only torch), gunicorn entrypoint
  (`backend/wsgi.py`), `.dockerignore`.
- `RUN_JOBS_INLINE=1`: run each job inside the request that created it so the
  service can scale to zero; jobs stuck in `running` are reported as failed after
  `JOB_TIMEOUT_SECONDS`.
- `CORS_ORIGINS` env var for allowed browser origins.
- Alembic migrations via Flask-Migrate (`make migrate`). **Migrations:** `0001`
  baseline, `0002` Supabase `profiles` table + row-level security.
- `.env.example`, rewritten README.

### Changed
- LLM calls moved from `google-genai` to OpenRouter (`backend/llm.py`); models set by
  `LLM_MODEL_EDGES` / `LLM_MODEL_SUMMARY`. Model names sent by clients are ignored.
- OCR uses Application Default Credentials when `GOOGLE_CREDENTIALS` is unset.
- Tables are no longer created at startup; use migrations (`AUTO_CREATE_TABLES=1`
  restores the old behavior for throwaway SQLite databases).
- Hard-coded production CORS origins removed.

### Security
- `/jobs/*` require a login, and jobs are visible only to their creator
  (`jobs.owner_id`).
- Row-level security enabled on all backend tables (Supabase exposes `public`
  over its REST API). Service-account key files are git-ignored.

### Upgrading an existing database
Databases created before migrations existed (the schema already matches `0002`):
`flask db stamp 0002`. Fresh databases: `make migrate`.

## 0.0.0 — 2026-10-11
Snapshot of `stem-accessibility/flowchart-accessibility` at `main`, without
history, venv, local database or credentials.

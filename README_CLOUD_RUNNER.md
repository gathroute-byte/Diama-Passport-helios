# DIAMA HelioS Back-test Runner (cloud-ready package)

This package exposes the existing, versioned Python back-test as a small authenticated HTTP service. It is prepared for a Docker-based host such as Render or Railway. It has **not** been deployed because the currently connected Railway account reports that its trial has expired; no live cloud service was created.

## What it does

- `GET /health` — service status and model version.
- `POST /build-panel` — accepts `seasons` and `daily` CSV files; returns `helios_phase_features.csv`.
- `POST /run` — accepts a phase-level `panel` CSV and matching `daily` CSV; returns a ZIP containing the model report, yearly results, summaries, sensitivity tables, predictions and run manifest.

All execution endpoints require `Authorization: Bearer <HELIOS_API_TOKEN>`. The token must be set as a secret environment variable on the host. Uploads are limited to 25 MB per file and `source_manifest` to 256 KB; `target` must be a plain column name; interactive API docs are disabled. Working files are temporary and are deleted after the response has been sent. No uploaded data are persisted to a database.

## Deploy on Render

1. Create a **private** GitHub repository and push the contents of this folder, preserving `Dockerfile`, `requirements.txt`, `cloud_app.py`, `helios_backtest.py` and `render.yaml` at the repository root.
2. Create a Render Blueprint/Web Service from that repository. `render.yaml` selects the Docker runtime, the `free` instance type, the `/health` check, and sets `HELIOS_API_TOKEN` with `generateValue: true`, so Render creates the random secret on the host. Confirm in the Render dashboard that the instance type shows $0 before approving anything.
3. Read the generated `HELIOS_API_TOKEN` from the service's Environment tab in the Render dashboard when you need to call the API. Never commit it or paste it into chat or issue trackers. A token shorter than 16 characters is treated as "not configured" and every execution request is rejected.
4. After deployment, open `/health`. `status: ready` checks service availability only, not model validity. `auth_configured: true` confirms a usable token is set.

Free-instance caveats (Render docs): the service spins down after 15 minutes idle (about a minute to wake), has 512 MB RAM, and may restart at any time. The runner admits one analysis at a time (HTTP 429 otherwise) and aborts jobs after 900 s (HTTP 504).

## Deploy on Railway

Upload the same repository to GitHub, then deploy it to a new Railway project using its Dockerfile. The included `railway.toml` configures the Docker builder, `/health` healthcheck, failure restarts and sleep mode. The current connected Railway account needs its trial/plan issue resolved before it can create new infrastructure.

## Request contract

`POST /build-panel` — multipart fields `seasons` (CSV), `daily` (CSV), and optional `allow_overlapping_phases` (`true`/`false`). It follows the model code's requirement that the phase start/end dates are explicit.

`POST /run` — multipart fields `panel` (CSV), `daily` (CSV), optional `target`, `interaction_mode` (`auto`, `vpd`, or `soil_moisture`), `run_district_holdout`, `allow_overlapping_phases`, and optional `source_manifest` (JSON text). Both CSVs must satisfy the input contract in `BACKTEST_README.md`.

Example (from a terminal; replace the URL and token):

```bash
curl -f -H "Authorization: Bearer $HELIOS_API_TOKEN" \
  -F "panel=@phase_features.csv" \
  -F "daily=@district_daily.csv" \
  -F "target=yield_anomaly" \
  -F "interaction_mode=auto" \
  -o helios_backtest_results.zip \
  https://YOUR-SERVICE-URL/run
```

## Scientific boundary

The API executes `helios-v0.9-int-flower-fill`; it does not source, repair or infer scientific inputs. It does not assert that the HelioS hypothesis works. A credible test still requires source-documented crop outcomes, daily climate/moisture observations, crop/district/date matching, defined phenology windows, and enough held-out seasons. A passing health check or software test is not empirical validation.

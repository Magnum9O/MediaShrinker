# Docker Compose Deployment

This folder contains the "deploy-only" entrypoint for running MediaShrinker with Docker Compose.

The image is built from the repository root (two levels up), so you still need the whole repo checkout.

There are now two supported Docker Compose paths:

- `docker-compose.yml`: local build from repo checkout
- `docker-compose.ghcr.yml`: pull a prebuilt image from GHCR

## What To Commit vs Ignore

Commit (code/config):

- `app/`
- `Dockerfile`, `requirements.txt`
- `docker/` (entrypoint + hwcheck)
- `docker/compose/` (compose + env example)
- `.github/workflows/` (CI)

Ignore (runtime data):

- `reports/` (includes `mediashrinker_runs.sqlite` and `run-*.json/.log`)
- `staging/`
- `logs/`
- `tessdata/`
- `media/` (optional local sample mounts)
- `.env`

## 1) Configure

From this folder:

```bash
cp .env.example .env
```

Edit `.env`:

- `MOVIES_ROOT`: host path to Movies
- `TV_ROOT`: host path to TVSeries
- `STAGING_ROOT`: host path to a fast local disk (SSD) for staging
- `REPORT_ROOT`: host path where reports + `mediashrinker_runs.sqlite` live
- `MEDIA_PORT`: host port to expose the web UI (default `8787`)

Runtime knobs:

- `MEDIA_LIBRARY`: `movies` / `series` / `both`
- `MEDIA_JOBS`: parallelism
- `MEDIA_ENCODER`: `auto` / `hevc_nvenc` / `libx265`
- `MEDIA_OCR_ENGINE`: `pgsrip` / `none`
- `MEDIA_OCR_LANGS`: e.g. `ita,eng,spa`
- `MEDIA_DELETE_BAK`: `1` to delete `.bak` after successful swaps

## PLAN vs RUN

- **PLAN** (dry run) scans and analyzes the library, produces a queue + reasons, writes live `run-*.json` and stores a run in SQLite. It does not modify media files.
- **RUN** executes the plan: copy to staging, subtitle work/OCR if needed, transcode if needed, then swap back to the library.

Recommended workflow: run PLAN first, check `/dashboard` and the run detail page, then run RUN.

## 2) Start

```bash
docker compose up -d --build mediashrinker
```

## Registry / Git-first Start

If your fork publishes `ghcr.io/<owner>/mediashrinker`, use:

```bash
cp .env.ghcr.example .env
```

Edit `.env`:

- `IMAGE_NAME`: for example `ghcr.io/magnum9o/mediashrinker:latest`
- the same bind mounts as the local-build flow

Then launch:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d mediashrinker
```

VAAPI:

```bash
docker compose -f docker-compose.ghcr.yml --profile vaapi up -d mediashrinker-vaapi
```

Synology VAAPI note:

- if the host uses a dedicated video group like `videodriver`, add the Synology override:

```bash
docker compose \
  -f docker-compose.ghcr.yml \
  -f docker-compose.ghcr.synology-vaapi.yml \
  --profile vaapi up -d mediashrinker-vaapi
```

NVIDIA:

```bash
docker compose -f docker-compose.ghcr.yml --profile nvidia up -d mediashrinker-nvidia
```

Open:

- `http://127.0.0.1:8787/ops` (operator panel: start PLAN/RUN/CLEANUP)
- `http://127.0.0.1:8787/dashboard` (live dashboard)
- `http://127.0.0.1:8787/schedule` (cron-like scheduler)
- `http://127.0.0.1:8787/healthz` (health)

## NVIDIA profile

```bash
docker compose --profile nvidia up -d --build mediashrinker-nvidia
docker compose --profile nvidia run --rm mediashrinker-nvidia hwcheck
```

## VAAPI profile (Intel/AMD)

```bash
docker compose --profile vaapi up -d --build mediashrinker-vaapi
```

Requires `/dev/dri` on the host (Linux).

## Watch daemon (periodic RUN loop)

```bash
docker compose --profile watchd up -d --build mediashrinker-watchd
```

Controls:

- `MEDIA_WATCH_INTERVAL` (seconds between runs)
- `MEDIA_NOTIFY_URL` (optional ntfy endpoint)

## Notes

- Encoding profiles: set `MEDIA_ENCODING_PROFILE` (`space_saver`, `balanced`, `quality`, `hq`).
- Extra OCR languages: set `TESSDATA_LANGS` (build arg) and rebuild the image.
- File ownership in container: set `PUID`/`PGID` to match the NAS owner/group, then rebuild.

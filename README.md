# MediaShrinker

MediaShrinker is a video-library pipeline that reduces disk usage while keeping media playable in Plex/Jellyfin/Emby libraries.

It scans Movies and TV-Series folders, decides which files need work, copies only the required files to a local staging area, converts non-HEVC video to HEVC, preserves all subtitle tracks, optionally OCRs PGS/image subtitles into text, writes JSON/log reports, stores run history in SQLite, and exposes a lightweight web dashboard — all inside a single Docker image.

## What It Does

- Converts non-HEVC video to HEVC with `ffmpeg`.
- Auto-detects the best available encoder: NVIDIA GPU → Intel/AMD GPU → CPU software.
- Four named encoding profiles: `space_saver`, `balanced` (default), `quality`, `hq`.
- Keeps all existing subtitle tracks (PGS, VobSub, ASS, SRT …).
- Adds external text subtitles found next to the source file.
- OCRs PGS/image subtitles into searchable text tracks (via `pgsrip` + Tesseract).
- Avoids replacing files when the output is larger than the source.
- Writes live `run-*.json` reports and a SQLite run history under `/reports`.
- Web UI: run history, per-file details, live monitor, dashboard, and job scheduler.
- Push notifications via [ntfy](https://ntfy.sh) on job completion.
- Periodic watch-daemon mode (runs every N seconds, no cron needed).

---

## Quick Start (CPU encoding, one command)

```bash
# 1. Clone / copy the repo
git clone https://github.com/lmerega/MediaShrinker.git
cd MediaShrinker

# 2. Configure
cp docker/compose/.env.example docker/compose/.env
$EDITOR docker/compose/.env      # set MOVIES_ROOT, TV_ROOT, STAGING_ROOT, REPORT_ROOT

# 3. Launch
cd docker/compose
docker compose up -d

# 4. Open the web UI
open http://localhost:8787
```

The web UI lets you run **PLAN** (dry run), **RUN** (transcode), or **cleanup** jobs with a single click.

## PLAN vs RUN (why PLAN exists)

MediaShrinker is designed to operate safely on large libraries and network mounts. For that reason, **PLAN** is a first-class action, not a debug-only feature.

- **PLAN** (dry run)
  - Scans and analyzes the library and produces a plan (what would be processed and why).
  - Writes a live `run-*.json` report and persists the run in SQLite.
  - Does not copy to staging, transcode, OCR, or replace files.
  - Use it to validate: mounts/paths, permissions, tools, OCR config, and to preview the queue in the dashboard.

- **RUN** (execute)
  - Executes the plan: copies only the needed files to staging, runs subtitle fixing/OCR if required, transcodes if required, then swaps the result back to the library.
  - Also writes live `run-*.json` + SQLite history.

Recommended workflow:

1. Run **PLAN** from `/ops`, then check `/dashboard` and the run detail page.
2. If the queue and reasons look correct, run **RUN**.

## Supported Usage

This repository is **Docker-first and Docker-only**.

- The supported way to run it is via `docker compose` under `docker/compose/`.
- Running from a local Python virtualenv is intentionally not documented or supported.

## Registry Deploy (GHCR)

If you publish the image from your fork to GitHub Container Registry, you can deploy without a local repo checkout.

Expected image name:

```bash
ghcr.io/magnum9o/mediashrinker:latest
```

Important:

- GHCR image names should be treated as lowercase.
- The publish workflow in `.github/workflows/publish-ghcr.yml` pushes `latest` on the default branch plus branch / tag / sha tags.
- So the correct pull is:

```bash
docker pull ghcr.io/magnum9o/mediashrinker:latest
```

Quick flow:

```bash
# in your fork
git push origin main

# then on the target host
cd docker/compose
cp .env.ghcr.example .env
$EDITOR .env   # set IMAGE_NAME and your bind mounts
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

---

## Hardware-Accelerated Encoding

### NVIDIA GPU (hevc_nvenc)

**Requirements:** [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed on the host.

```bash
# .env — force NVIDIA or leave MEDIA_ENCODER=auto (recommended)
MEDIA_ENCODER=hevc_nvenc

# Launch with the nvidia profile
cd docker/compose
docker compose --profile nvidia up -d mediashrinker-nvidia
```

The `nvidia` profile passes `NVIDIA_VISIBLE_DEVICES=all` and sets the GPU resource reservation so Docker allocates the hardware encoder. The `hevc_nvenc` encoder uses VBR mode with configurable CQ per resolution tier.

Verify that the GPU is visible inside the container:

```bash
docker compose --profile hwcheck run --rm mediashrinker-hwcheck
```

### Intel / AMD GPU (hevc_vaapi)

**Requirements:** `/dev/dri/renderD128` must exist on the host (i915, amdgpu, or xe driver loaded).

```bash
# .env — force VAAPI or leave MEDIA_ENCODER=auto (recommended)
MEDIA_ENCODER=hevc_vaapi

# Launch with the vaapi profile
cd docker/compose
docker compose --profile vaapi up -d mediashrinker-vaapi
```

The `vaapi` profile bind-mounts `/dev/dri` into the container. The pipeline uses software decode → `hwupload` → `hevc_vaapi` encode (NV12, 8-bit). Note: VAAPI does not support 10-bit HEVC output in most driver stacks, so the output is always 8-bit.

### CPU (libx265)

No special hardware needed. This is the default when no GPU is detected.

```bash
MEDIA_ENCODER=libx265   # or leave MEDIA_ENCODER=auto
cd docker/compose
docker compose up -d
```

### Auto-detection (recommended)

Leave `MEDIA_ENCODER=auto` (the default). On startup the container probes the available encoders in order: NVENC → VAAPI → libx265. The first one that is both compiled into ffmpeg **and** has the required device node is chosen.

---

## Configuration Reference

Copy `docker/compose/.env.example` to `docker/compose/.env` and edit as needed.

### Glossary

- **PGS (HDMV PGS)**: BluRay subtitle format made of images (not searchable text).
- **VobSub**: DVD subtitle format made of images.
- **OCR**: converts image subtitles (PGS/VobSub) into text subtitles (typically `.srt`) using `pgsrip` + Tesseract.
- **Staging**: fast local workspace where files are copied before processing; originals are only replaced at the end.

| Variable | Default | Description |
|---|---|---|
| `MOVIES_ROOT` | *(required)* | Host path to the Movies library |
| `TV_ROOT` | *(required)* | Host path to the TV-Series library |
| `STAGING_ROOT` | *(required)* | Host path for temporary work files |
| `REPORT_ROOT` | *(required)* | Host path for JSON/log reports and the SQLite DB |
| `MEDIA_PORT` | `8787` | Web UI port |
| `MEDIA_ENCODER` | `auto` | `auto` / `hevc_nvenc` / `hevc_vaapi` / `libx265` |
| `MEDIA_ENCODING_PROFILE` | `balanced` | `space_saver` / `balanced` / `quality` / `hq` |
| `MEDIA_LIBRARY` | `both` | `both` / `movies` / `series` |
| `MEDIA_JOBS` | `1` | Parallel transcoding jobs |
| `MEDIA_OCR_ENGINE` | `pgsrip` | `pgsrip` / `none` |
| `MEDIA_OCR_LANGS` | `ita,eng` | OCR target languages (ISO 639-2, comma-separated). Example: `ita,eng,spa`. |
| `MEDIA_EXTRACT_PGS` | `1` | Enables the PGS/VobSub extraction + OCR path (set `0` to disable OCR pipeline). |
| `MEDIA_ADD_EXTERNAL_TEXT_SUBS` | `1` | Mux external `.srt`/`.ass` files into the output |
| `MEDIA_DELETE_BAK` | `0` | Delete `.bak` backup after successful upload |
| `MEDIA_BITRATE_THRESHOLD_MBPS` | `55.0` | Skip video transcode below this bitrate |
| `MEDIA_BITRATE_4K_MBPS` | `45.0` | 4K-specific bitrate threshold |
| `MEDIA_NO_MULTIPASS` | `0` | Disable two-pass encoding |
| `MEDIA_NOTIFY_URL` | *(empty)* | ntfy URL for push notifications (e.g. `https://ntfy.sh/my-topic`) |
| `MEDIA_WATCH_INTERVAL` | `3600` | Seconds between runs in watch-daemon mode. Example: `14400` = every 4 hours. |
| `PUID` | `1000` | UID for the in-container process (must match NAS file owner) |
| `PGID` | `1000` | GID for the in-container process |
| `TESSDATA_LANGS` | `eng ita` | Space-separated list of Tesseract language packs (apt) to install at build time. |

### Encoding Profiles

| Profile | Goal | CQ range | Preset |
|---|---|---|---|
| `space_saver` | Minimum file size | 26–32 | nvenc p7 / x265 slow |
| `balanced` | Quality/size balance (default) | 22–28 | nvenc p5 / x265 medium |
| `quality` | High quality | 18–24 | nvenc p4 / x265 fast |
| `hq` | Maximum quality | 16–22 | nvenc p3 / x265 veryfast |

CQ targets vary by resolution tier (4K / 1080p / 720p / SD) and content type (movie / series). All values are adjustable in `app/mediashrinker_core/policy.py`.

---

## Docker Compose Profiles

| Profile | Command | Use case |
|---|---|---|
| *(default)* | `docker compose up -d` | CPU encoding, web UI |
| `nvidia` | `docker compose --profile nvidia up -d mediashrinker-nvidia` | NVIDIA GPU encoding, web UI |
| `vaapi` | `docker compose --profile vaapi up -d mediashrinker-vaapi` | Intel/AMD GPU encoding, web UI |
| `watchd` | `docker compose --profile watchd up -d mediashrinker-watchd` | Periodic daemon (no web UI) |
| `hwcheck` | `docker compose --profile hwcheck run --rm mediashrinker-hwcheck` | Hardware capability check |

---

## Web UI

The web UI is available at `http://<host>:<MEDIA_PORT>` (default port 8787).

| Page | URL | Description |
|---|---|---|
| Runs list | `/` | All past runs with key metrics |
| Control Room | `/ops` | Start PLAN / RUN / cleanup, stop running job, runtime config |
| Scheduler | `/schedule` | Add/remove/toggle cron-based scheduled runs |
| Dashboard | `/dashboard` | Live KPIs, active jobs, queue, recent results |
| Live monitor | `/live` | Auto-refreshing live status of the current run |
| Run detail | `/run?id=N` | Per-file details with subtitle and OCR columns |
| File detail | `/file?run_id=N&path=…` | Track-by-track subtitle view for a single file |

---

## Watch Daemon (periodic)

The `watchd` profile runs PLAN+RUN in a loop, sleeping `MEDIA_WATCH_INTERVAL` seconds between iterations. This is useful if you prefer a simple always-on daemon over a cron/scheduler.

```bash
# Run every 4 hours
MEDIA_WATCH_INTERVAL=14400
docker compose --profile watchd up -d mediashrinker-watchd
```

---

## Push Notifications (ntfy)

Set `MEDIA_NOTIFY_URL` to any ntfy-compatible endpoint. A notification is sent at the end of each RUN with the number of processed/transcoded files and the total size delta.

```env
# Public ntfy server — use a hard-to-guess topic name
MEDIA_NOTIFY_URL=https://ntfy.sh/my-secret-mediashrinker-topic

# Self-hosted
MEDIA_NOTIFY_URL=http://ntfy.lan/mediashrinker
```

---

## Multi-Language OCR

Tesseract language packs are installed **at build time** via the `TESSDATA_LANGS` build argument. Add all the languages you need before building:

```env
# .env
TESSDATA_LANGS=eng ita fra deu spa
MEDIA_OCR_LANGS=ita,eng,fra
```

Supported language codes follow the `tesseract-ocr-XXX` apt package naming. Common ones: `eng`, `ita`, `fra`, `deu`, `spa`, `por`, `nld`, `pol`, `rus`, `jpn`, `chi-sim`.

After changing `TESSDATA_LANGS` you must rebuild the image:

```bash
docker compose build --no-cache
docker compose up -d
```

---

## Building the Image

```bash
# Default (CPU only, eng+ita tessdata)
docker compose build

# With French tessdata
docker compose build --build-arg TESSDATA_LANGS="eng ita fra"

# Force rebuild from scratch
docker compose build --no-cache
```

---

## File Backup and Safety

- Every processed file is copied to staging first; the original is never touched until the output is verified.
- On success the output replaces the source and a `.bak` is kept alongside it.
- Set `MEDIA_DELETE_BAK=1` (or tick the checkbox in the web UI) to delete `.bak` files automatically.
- If the output is larger than the source (growth guard: >5%), the original is restored and the file is marked `skipped`.

---

## Non-Docker Usage
Not supported. Use Docker.

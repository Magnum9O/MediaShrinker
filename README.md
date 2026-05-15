# MediaShrinker

## Crediti (PRIMA COSA)

Questo repository è un refork del progetto originale di lmerega. Tutto il merito dell'idea e del lavoro iniziale va a lui:

- Progetto originale: [lmerega/MediaShrinker](https://github.com/lmerega/MediaShrinker)
- Autore originale: `lmerega`

Questo fork aggiunge adattamenti operativi, UI e deploy; il progetto di base e l'idea originale restano del repository upstream.
MediaShrinker è una pipeline per librerie video che riduce l'uso di spazio su disco mantenendo i file riproducibili in librerie Plex/Jellyfin/Emby.

Scansiona le cartelle di Film e Serie TV, decide quali file necessitano di intervento, copia solo i file necessari in un'area di staging locale, converte video non-HEVC in HEVC, preserva tutte le tracce dei sottotitoli, opzionalmente esegue OCR su sottotitoli immagine/PGS per ottenere testo, scrive report JSON/log, conserva la cronologia delle esecuzioni in SQLite ed espone una web dashboard leggera — tutto all'interno di un singolo container Docker.

## Cosa fa

- Converte video non-HEVC in HEVC usando `ffmpeg`.
- Rileva automaticamente il miglior encoder disponibile: GPU NVIDIA → GPU Intel/AMD → CPU software.
- Quattro profili di codifica nominati: `space_saver`, `balanced` (default), `quality`, `hq`.
- Mantiene tutte le tracce di sottotitoli esistenti (PGS, VobSub, ASS, SRT …).
- Aggiunge sottotitoli di testo esterni trovati accanto al file sorgente.
- Esegue OCR su sottotitoli immagine/PGS per convertirli in tracce di testo ricercabili (tramite `pgsrip` + Tesseract).
- Evita di sovrascrivere i file quando l'output è più grande della sorgente.
- Scrive report live `run-*.json` e conserva la cronologia in un DB SQLite sotto `/reports`.
- Web UI: cronologia delle esecuzioni, dettagli per file, monitor live, dashboard e scheduler di job.
- Notifiche push via [ntfy](https://ntfy.sh) al termine dei job.
- Modalità watch-daemon periodica (esegue ogni N secondi, senza cron).

---

## Avvio rapido (modalità raccomandata: immagine GHCR)

La maniera più semplice è usare l'immagine pubblicata su GitHub Container Registry: `ghcr.io/magnum9o/mediashrinker:latest`.

## Installazione via Docker Compose / Portainer

Se preferisci gestire il deployment con `docker compose`, Portainer (Stacks) o strumenti simili, ecco come procedere.

Opzione consigliata (GHCR image + Compose):

```bash
cd docker/compose
cp .env.ghcr.example .env
$EDITOR .env   # imposta MOVIES_ROOT, TV_ROOT, STAGING_ROOT, REPORT_ROOT

# Scarica l'immagine e avvia il stack pensato per GHCR
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

Usa questa opzione quando vuoi un deployment ripetibile senza rebuild locali.

Portainer (Stack) — istruzioni rapide:

- Apri Portainer → Stacks → Add stack.
- Dai un nome allo stack (es. `mediashrinker`).
- Nel campo "Web editor" incolla il contenuto di `docker-compose.ghcr.yml` o usa "Git repository" e punta al tuo fork con il path `docker/compose/docker-compose.ghcr.yml`.
- Aggiungi le variabili d'ambiente nel pannello "Env" oppure assicurati che il file `.env` sia incluso nel repository (o settale come file in Portainer se supportato).
- Deploy/Update. Portainer scaricherà l'immagine GHCR e avvierà i container.

Nota su immagini private: se l'immagine GHCR è privata, configura le credenziali nel registry settings di Portainer o sul daemon Docker (`docker login ghcr.io`).

Esempio `docker-compose` per Portainer (incolla questo come Stack):

```yaml
version: '3.8'
services:
  mediashrinker:
    image: ghcr.io/magnum9o/mediashrinker:latest
    container_name: mediashrinker
    env_file:
      - .env
    ports:
      - "8787:8787"
    volumes:
      - ${MOVIES_ROOT}:/media/movies
      - ${TV_ROOT}:/media/tv
      - ${STAGING_ROOT}:/staging
      - ${REPORT_ROOT}:/reports
    restart: unless-stopped

# Note: in Portainer puoi incollare il contenuto sopra in Stacks → Add stack → Web editor
# oppure collegare il repository Git che contiene `docker/compose/docker-compose.ghcr.yml`.
```


```bash
# Copia l'esempio di env e modifica i percorsi
cd docker/compose
cp .env.ghcr.example .env
$EDITOR .env   # imposta IMAGE_NAME (opzionale), MOVIES_ROOT, TV_ROOT, STAGING_ROOT, REPORT_ROOT

# Scarica l'immagine dal registry e avvia
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d

# Apri la web UI
open http://localhost:8787
```

La web UI permette di eseguire con un click **PLAN** (simulazione), **RUN** (transcodifica) o job di **cleanup**.

### Opzione alternativa: build dal sorgente (solo se modifichi il codice)

Se vuoi modificare il codice o ricostruire l'immagine localmente, puoi ancora clonare il repository e buildare l'immagine:

```bash
# Clona il fork (o il repository upstream se preferisci)
git clone https://github.com/magnum9o/MediaShrinker.git
cd MediaShrinker

# Costruisci e avvia (default: CPU build)
cd docker/compose
docker compose build
docker compose up -d
```

Usa la modalità GHCR per deployment veloci e senza checkout locale.

## PLAN vs RUN (perché esiste PLAN)

MediaShrinker è progettato per operare in sicurezza su librerie grandi e mount di rete. Per questo motivo **PLAN** è un'azione di prima classe, non una semplice funzionalità di debug.

- **PLAN** (simulazione)
  - Scansiona e analizza la libreria e produce un piano (cosa verrebbe processato e perché).
  - Scrive un report live `run-*.json` e registra l'esecuzione in SQLite.
  - Non copia in staging, non transcodifica, non esegue OCR né sostituisce file.
  - Usalo per convalidare: mount/percorso, permessi, tool, configurazione OCR e per anteprima della coda nella dashboard.

- **RUN** (esecuzione)
  - Esegue il piano: copia solo i file necessari in staging, esegue il fixing/OCR dei sottotitoli se richiesto, transcodifica se necessario, quindi sostituisce i file nella libreria.
  - Scrive anch'esso report live `run-*.json` e aggiorna la cronologia in SQLite.

Workflow consigliato:

1. Esegui **PLAN** da `/ops`, poi controlla `/dashboard` e la pagina di dettaglio dell'esecuzione.
2. Se la coda e i motivi sono corretti, lancia **RUN**.

## Uso supportato

Questo repository è **Docker-first e Docker-only**.

- Il modo supportato per eseguirlo è tramite `docker compose` sotto `docker/compose/`.
- L'esecuzione da un virtualenv Python locale non è documentata né supportata intenzionalmente.

## Distribuzione su registry (GHCR)

Se pubblichi l'immagine dal tuo fork su GitHub Container Registry (GHCR), puoi distribuire senza avere il repository locale sulla macchina di destinazione.

Nome immagine previsto:

```bash
ghcr.io/magnum9o/mediashrinker:latest
```

Importante:

- I nomi delle immagini GHCR devono essere trattati in minuscolo.
- Il workflow di pubblicazione in `.github/workflows/publish-ghcr.yml` pubblica il tag `latest` sul branch di default e aggiunge tag per branch/tag/sha.
- Quindi il pull corretto è:

```bash
docker pull ghcr.io/magnum9o/mediashrinker:latest
```

Flusso rapido:

```bash
# nel tuo fork
git push origin main

# poi sull'host di destinazione
cd docker/compose
cp .env.ghcr.example .env
$EDITOR .env   # imposta IMAGE_NAME e i bind mount
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

In pratica non è necessario impostare manualmente tutte le variabili.
Di solito servono:

- `IMAGE_NAME`
- `MOVIES_ROOT`
- `TV_ROOT`
- `STAGING_ROOT`
- `REPORT_ROOT`

Il resto può rimanere sui valori di default del template, salvo esigenze specifiche.

---

## Codifica accelerata via hardware

### GPU NVIDIA (hevc_nvenc)

Requisiti: installare il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) sull'host.

```bash
# .env — forzare NVIDIA o lasciare MEDIA_ENCODER=auto (consigliato)
MEDIA_ENCODER=hevc_nvenc

# Avvia con il profilo nvidia
cd docker/compose
docker compose --profile nvidia up -d mediashrinker-nvidia
```

Il profilo `nvidia` imposta `NVIDIA_VISIBLE_DEVICES=all` e riserva le risorse GPU così Docker può allocare l'encoder hardware. L'encoder `hevc_nvenc` utilizza VBR con CQ configurabile per fascia di risoluzione.

Verifica che la GPU sia visibile dentro il container:

```bash
docker compose --profile hwcheck run --rm mediashrinker-hwcheck
```

### GPU Intel / AMD (hevc_vaapi)

Requisiti: `/dev/dri/renderD128` deve esistere sull'host (driver i915, amdgpu o xe caricati).

```bash
# .env — forzare VAAPI o lasciare MEDIA_ENCODER=auto (consigliato)
MEDIA_ENCODER=hevc_vaapi

# Avvia con il profilo vaapi
cd docker/compose
docker compose --profile vaapi up -d mediashrinker-vaapi
```

Il profilo `vaapi` bind-monta `/dev/dri` nel container. La pipeline usa decode software → `hwupload` → encode `hevc_vaapi` (NV12, 8-bit). Nota: VAAPI in molti stack driver non supporta HEVC 10-bit in output, quindi l'output è sempre 8-bit.

### CPU (libx265)

Nessun hardware speciale richiesto. Questo è il comportamento di default quando non viene rilevata una GPU.

```bash
MEDIA_ENCODER=libx265   # o lasciare MEDIA_ENCODER=auto
cd docker/compose
docker compose up -d
```

### Rilevamento automatico (consigliato)

Lascia `MEDIA_ENCODER=auto` (default). All'avvio il container prova gli encoder disponibili in ordine: NVENC → VAAPI → libx265. Il primo che è sia compilato in ffmpeg **che** ha il device node richiesto viene scelto.

---

## Riferimento di configurazione

Copia `docker/compose/.env.example` in `docker/compose/.env` e modifica secondo necessità.

### Glossario

- **PGS (HDMV PGS)**: formato sottotitoli BluRay composto da immagini (non testo ricercabile).
- **VobSub**: formato sottotitoli DVD composto da immagini.
- **OCR**: converte sottotitoli immagine (PGS/VobSub) in sottotitoli di testo (tipicamente `.srt`) usando `pgsrip` + Tesseract.
- **Staging**: workspace locale veloce dove i file vengono copiati prima della lavorazione; gli originali vengono sostituiti solo al termine.

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

### Profili di codifica

| Profile | Goal | CQ range | Preset |
|---|---|---|---|
| `space_saver` | Minimum file size | 26–32 | nvenc p7 / x265 slow |
| `balanced` | Quality/size balance (default) | 22–28 | nvenc p5 / x265 medium |
| `quality` | High quality | 18–24 | nvenc p4 / x265 fast |
| `hq` | Maximum quality | 16–22 | nvenc p3 / x265 veryfast |

I target CQ variano per fascia di risoluzione (4K / 1080p / 720p / SD) e tipo di contenuto (film / serie). Tutti i valori sono modificabili in `app/mediashrinker_core/policy.py`.

---

## Profili Docker Compose

| Profile | Command | Use case |
|---|---|---|
| *(default)* | `docker compose up -d` | CPU encoding, web UI |
| `nvidia` | `docker compose --profile nvidia up -d mediashrinker-nvidia` | NVIDIA GPU encoding, web UI |
| `vaapi` | `docker compose --profile vaapi up -d mediashrinker-vaapi` | Intel/AMD GPU encoding, web UI |
| `watchd` | `docker compose --profile watchd up -d mediashrinker-watchd` | Periodic daemon (no web UI) |
| `hwcheck` | `docker compose --profile hwcheck run --rm mediashrinker-hwcheck` | Hardware capability check |

---

## Web UI

La web UI è disponibile su `http://<host>:<MEDIA_PORT>` (porta predefinita 8787).

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

## Watch Daemon (periodico)

Il profilo `watchd` esegue PLAN+RUN in loop, dormendo `MEDIA_WATCH_INTERVAL` secondi tra un'iterazione e l'altra. Utile se preferisci un semplice daemon sempre attivo invece di cron/scheduler.

```bash
# Esegui ogni 4 ore
MEDIA_WATCH_INTERVAL=14400
docker compose --profile watchd up -d mediashrinker-watchd
```

---

## Notifiche Push (ntfy)

Imposta `MEDIA_NOTIFY_URL` con un endpoint compatibile ntfy. Una notifica viene inviata al termine di ogni RUN con il numero di file processati/transcodificati e il delta totale di dimensione.

```env
# Server ntfy pubblico — usare un topic difficile da indovinare
MEDIA_NOTIFY_URL=https://ntfy.sh/my-secret-mediashrinker-topic

# Self-hosted
MEDIA_NOTIFY_URL=http://ntfy.lan/mediashrinker
```

---

## OCR multilingua

I pacchetti lingua di Tesseract vengono installati **in fase di build** tramite l'argomento `TESSDATA_LANGS`. Aggiungi tutte le lingue necessarie prima di buildare:

```env
# .env
TESSDATA_LANGS=eng ita fra deu spa
MEDIA_OCR_LANGS=ita,eng,fra
```

I codici lingua supportati seguono il naming dei pacchetti apt `tesseract-ocr-XXX`. Alcuni comuni: `eng`, `ita`, `fra`, `deu`, `spa`, `por`, `nld`, `pol`, `rus`, `jpn`, `chi-sim`.

Dopo aver modificato `TESSDATA_LANGS` è necessario ricostruire l'immagine:

```bash
docker compose build --no-cache
docker compose up -d
```

---

## Build dell'immagine

```bash
# Default (CPU only, eng+ita tessdata)
docker compose build

# Con tessdata francese
docker compose build --build-arg TESSDATA_LANGS="eng ita fra"

# Forza rebuild da zero
docker compose build --no-cache
```

---

## Backup file e sicurezza

- Ogni file processato viene prima copiato in staging; l'originale non viene toccato finché l'output non è verificato.
- In caso di successo l'output sostituisce la sorgente e viene mantenuto un `.bak` a fianco.
- Imposta `MEDIA_DELETE_BAK=1` (o seleziona l'opzione nella web UI) per eliminare automaticamente i `.bak`.
- Se l'output è più grande della sorgente (growth guard: >5%), l'originale viene ripristinato e il file viene marcato come `skipped`.

---

## Uso senza Docker
Non supportato. Usa Docker.

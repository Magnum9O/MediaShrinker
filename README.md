# MediaShrinker

> Fork di [lmerega/MediaShrinker](https://github.com/lmerega/MediaShrinker). Tutto il merito dell'idea e del lavoro originale va a lui.
> Questo fork aggiunge adattamenti operativi, UI e deploy.

Pipeline Docker per librerie video (Plex / Jellyfin / Emby) che riduce l'uso di spazio su disco mantenendo i file riproducibili.

**Cosa fa:**
- Converte video non-HEVC in HEVC con `ffmpeg` (GPU NVIDIA / Intel-AMD / CPU).
- Quattro profili di codifica: `space_saver`, `balanced` *(default)*, `quality`, `hq`.
- Mantiene le tracce testuali e sostituisce le tracce bitmap supportate (PGS/VobSub) con `.srt` per evitare transcoding dei sottotitoli.
- OCR su sottotitoli immagine (PGS/VobSub) → testo ricercabile, via `pgsrip` + Tesseract / ffmpeg.
- Aggiunge sottotitoli `.srt`/`.ass` esterni trovati accanto al file sorgente.
- Skippa i file se l'output è più grande della sorgente (growth guard).
- Web UI: dashboard live, control room, cronologia, scheduler cron, dettagli per file.
- Notifiche push via [ntfy](https://ntfy.sh).

---

## Avvio rapido

Incolla questo compose, sostituisci i quattro percorsi, avvia:

```yaml
services:
  mediashrinker:
    image: ghcr.io/magnum9o/mediashrinker:latest
    container_name: mediashrinker
    ports:
      - "8787:8787"
    volumes:
      - /percorso/host/movies:/data/movies
      - /percorso/host/tv:/data/tv
      - /percorso/host/staging:/staging
      - /percorso/host/reports:/reports
    restart: unless-stopped
```

```bash
docker compose up -d
# poi apri http://<ip-host>:8787
```

### Personalizzazione opzionale

Aggiungi un blocco `environment:` solo per i valori che vuoi cambiare rispetto ai default:

```yaml
    environment:
      MEDIA_ENCODER: auto           # auto | hevc_nvenc | hevc_vaapi | libx265
      MEDIA_ENCODING_PROFILE: balanced   # space_saver | balanced | quality | hq
      MEDIA_LIBRARY: both           # both | movies | series
      MEDIA_JOBS: "1"
      MEDIA_OCR_ENGINE: pgsrip      # pgsrip | none
      MEDIA_OCR_LANGS: ita,eng
      MEDIA_BITRATE_THRESHOLD_MBPS: "55.0"
      MEDIA_NOTIFY_URL: ""          # https://ntfy.sh/my-topic
```

Tutto il resto si configura dalla web UI → **Control Room → Advanced settings**.

---

## GPU / Hardware encoding

### NVIDIA (hevc_nvenc)

Installa il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) sull'host, poi:

```yaml
    environment:
      MEDIA_ENCODER: hevc_nvenc
      NVIDIA_VISIBLE_DEVICES: all
      NVIDIA_DRIVER_CAPABILITIES: compute,video,utility
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu, video]
```

### Intel / AMD (hevc_vaapi)

Richiede `/dev/dri/renderD128` sull'host (driver i915, amdgpu o xe):

```yaml
    environment:
      MEDIA_ENCODER: hevc_vaapi
    devices:
      - /dev/dri:/dev/dri
```

### CPU (libx265)

Default se non viene rilevata nessuna GPU. Nessuna configurazione aggiuntiva.

### Auto-detect (raccomandato)

Lascia `MEDIA_ENCODER: auto` (o ometti la variabile). Il container prova in ordine: NVENC → VAAPI → libx265.

---

## PLAN vs RUN

**PLAN** — simulazione, non tocca nulla:
- Scansiona la libreria e mostra cosa verrebbe processato e perché.
- Utile per verificare percorsi, permessi e configurazione OCR prima di procedere.

**RUN** — esecuzione reale:
- Copia i file in staging, esegue OCR/fix sottotitoli, transcodifica, sostituisce i file nella libreria.
- Mantiene un `.bak` accanto a ogni file sostituito (eliminabile con `MEDIA_DELETE_BAK=1`).

Workflow consigliato: PLAN → controlla la dashboard → RUN.

---

## OCR multilingua

I pacchetti Tesseract vengono installati **a build time**. Se usi l'immagine GHCR precostituita (`eng` e `ita` inclusi), hai già inglese e italiano.

Per aggiungere altre lingue devi ricostruire l'immagine:

```bash
git clone https://github.com/magnum9o/MediaShrinker.git
cd MediaShrinker
docker build --build-arg TESSDATA_LANGS="eng ita fra deu spa" -t mediashrinker:custom .
```

Poi usa `image: mediashrinker:custom` nel compose.

Codici lingua (pacchetti apt `tesseract-ocr-XXX`): `eng`, `ita`, `fra`, `deu`, `spa`, `por`, `nld`, `pol`, `rus`, `jpn`, `chi-sim` …

---

## Web UI

Disponibile su `http://<host>:8787` (porta configurabile con `MEDIA_PORT`).

| Pagina | URL | Descrizione |
|---|---|---|
| Lista run | `/` | Storico con metriche chiave |
| Control Room | `/ops` | Avvia PLAN / RUN / cleanup, stop job, config |
| Scheduler | `/schedule` | Aggiunta/rimozione job cron |
| Dashboard | `/dashboard` | KPI live, coda, job attivi, risultati recenti |
| Live monitor | `/live` | Stato live del run corrente |
| Run detail | `/run?id=N` | Dettagli per file con colonne sottotitoli/OCR |
| File detail | `/file?run_id=N&path=…` | Vista tracce sottotitoli per singolo file |

---

## Watch daemon (periodico)

Per eseguire automaticamente ogni N secondi senza usare lo scheduler interno:

```yaml
    command: ["watchd"]
    environment:
      MEDIA_WATCH_INTERVAL: "3600"   # secondi tra un run e l'altro
```

---

## Notifiche push (ntfy)

```yaml
    environment:
      MEDIA_NOTIFY_URL: https://ntfy.sh/my-secret-topic
```

Una notifica viene inviata al termine di ogni RUN con file processati e delta dimensione.

---

## Profili di codifica

| Profilo | Obiettivo | CQ range | Preset |
|---|---|---|---|
| `space_saver` | Dimensione minima | 26–32 | nvenc p7 / x265 slow |
| `balanced` | Bilanciamento qualità/dimensione *(default)* | 22–28 | nvenc p5 / x265 medium |
| `quality` | Alta qualità | 18–24 | nvenc p4 / x265 fast |
| `hq` | Qualità massima | 16–22 | nvenc p3 / x265 veryfast |

I valori CQ variano per risoluzione (4K / 1080p / 720p / SD) e tipo di contenuto. Tutto configurabile in `app/mediashrinker_core/policy.py`.

---

## Sicurezza dei file

- Ogni file viene copiato in staging prima di essere lavorato; l'originale non viene mai toccato fino a completamento.
- In caso di successo l'output sostituisce la sorgente e viene mantenuto un `.bak`.
- Se l'output è più grande del 5% della sorgente, l'originale viene ripristinato e il file viene marcato come `skipped`.
- `MEDIA_DELETE_BAK=1` elimina automaticamente i `.bak` dopo ogni swap riuscito.

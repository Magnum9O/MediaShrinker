# Piano Operativo UI e Compose

Ultimo aggiornamento: 2026-05-15

## Obiettivo

Ridurre la complessità percepita di MediaShrinker su due fronti:

- UI più chiara e meno tecnica
- configurazione Docker Compose meno dispersiva e meno "manuale"

Questo file traduce le decisioni già emerse in una linea operativa concreta.

## Decisioni Confermate

### Navigazione

La navbar deve mostrare solo queste voci:

- `Dashboard`
- `Control Room`
- `Live`
- `Scheduler`
- `Runs`

Devono apparire come pulsanti.

Endpoint tecnici da non mostrare in UI:

- `dashboard.json`
- `ops.json`

Nota:

- gli endpoint possono continuare a esistere per uso interno / debug
- semplicemente non devono comparire nella navigazione

## Ruolo delle Pagine

### Dashboard

Scopo:

- overview rapida dello stato del sistema
- capire subito se c’è un job attivo, quanto è avanti e cosa sta succedendo

Non deve essere:

- una pagina tecnica
- un dump di configurazione
- una replica della live

### Control Room

Scopo:

- avviare e controllare i job
- cambiare solo i parametri operativi davvero usati spesso

Non deve essere:

- una pagina di installazione
- una pagina piena di path e dettagli interni

### Live

Scopo:

- console operativa durante PLAN/RUN
- log controllabili
- accesso immediato al report finale

### Run Detail

Scopo:

- analisi dettagliata completa
- ispezione tecnica della singola run

Qui ha senso tenere molto più dettaglio.

### Scheduler

Scopo:

- automazione
- niente overload di stato runtime

## Compose: cosa deve essere manuale davvero

Risposta breve:

- **no**, non ha senso mettere tutto a mano ogni volta

La configurazione va divisa in:

1. valori host-specifici
2. valori operativi con default sensati
3. valori avanzati raramente toccati

## Parametri da Compilare Manualmente

Questi sono i parametri che in pratica cambiano davvero da host a host e quindi hanno senso come input esplicito:

- `MOVIES_ROOT`
- `TV_ROOT`
- `STAGING_ROOT`
- `REPORT_ROOT`

In deployment da registry/GHCR aggiungere anche:

- `IMAGE_NAME`

Parametri opzionali che si possono cambiare ma non devono essere obbligatori:

- `MEDIA_PORT`
- `PUID`
- `PGID`

## Parametri con Default Sensato

Questi non dovrebbero richiedere inserimento manuale nella maggior parte dei casi:

- `MEDIA_ENCODER`
- `MEDIA_ENCODING_PROFILE`
- `MEDIA_LIBRARY`
- `MEDIA_JOBS`
- `MEDIA_OCR_ENGINE`
- `MEDIA_OCR_LANGS`
- `MEDIA_EXTRACT_PGS`
- `MEDIA_ADD_EXTERNAL_TEXT_SUBS`
- `MEDIA_DELETE_BAK`
- `MEDIA_BITRATE_THRESHOLD_MBPS`
- `MEDIA_BITRATE_4K_MBPS`
- `MEDIA_NO_MULTIPASS`
- `MEDIA_NOTIFY_URL`
- `MEDIA_WATCH_INTERVAL`

Principio:

- devono vivere nel `.env.example` o `.env.synology.example` con valori già utili
- l’utente li tocca solo se ha un motivo preciso

## Strategia Compose Consigliata

### Template standard

Fornire template con valori già pronti:

- `.env.example`
- `.env.synology.example`
- `.env.ghcr.example`

Ogni template deve chiarire bene:

- cosa devi compilare per forza
- cosa puoi lasciare così com’è

### Filosofia

L’utente non deve vedere una lista lunga di variabili e pensare:

- “devo decidere tutto io?”

L’esperienza corretta è:

1. compila 4 path
2. opzionalmente cambia porta / utente / gruppo
3. avvia

Il resto deve già avere default decenti.

## UI: cosa tenere visibile di default

### Control Room — sezione principale

Devono restare visibili di default:

- scope:
  - libreria intera
  - titolo/cartella singola
- libreria:
  - film
  - serie
  - entrambe
- encoder
- profilo encoding
- jobs
- OCR on/off
- delete `.bak` on/off
- pulsanti:
  - `PLAN`
  - `RUN`
  - `Cleanup`

### Control Room — sezione avanzata

Da spostare in `Advanced settings` collassabile:

- `Movies path`
- `TV path`
- `Staging path`
- `Reports path`
- `OCR target langs`
- `Bitrate threshold`
- `4K threshold`
- `pgsrip_bin`
- `TESSDATA_PREFIX`
- `notify_url`
- `extract_pgs`
- `add_external_text_subs`
- `no_multipass`

Nota:

- alcuni toggle possono anche restare visibili se risultano davvero usati spesso
- ma la prima impressione della pagina deve essere più snella

## Live: direzione da applicare

### Tenere in primo piano

- stato corrente
- run corrente
- report run appena disponibile
- log in finestra con scroll interno
- controlli live/pause

### Ridurre o declassare

- `Run scope`
- `Config`
- `Totals`

Possibile destinazione:

- sezione avanzata collassabile
- oppure rimozione dalla vista default

## Dashboard: direzione da applicare

La dashboard deve essere:

- più pulita
- più orientata allo stato
- meno tecnica

Deve mostrare soprattutto:

- run attiva
- progress
- coda
- ultimi risultati
- storico sintetico

Non deve trasformarsi in:

- dump tecnico della live

## Semplificazione Prodotto

Principio guida:

- la stessa informazione non deve comparire in tre pagine diverse in tre forme diverse

Distribuzione consigliata:

- `Dashboard` = overview
- `Control Room` = comando
- `Live` = osservazione runtime
- `Run` = analisi tecnica
- `Scheduler` = automazione

## Modifiche Operative da Fare Ora

1. Rendere la navbar a pulsanti
2. Rimuovere `dashboard.json` e `ops.json` dalla UI
3. Snellire la `Control Room`
4. Spostare i parametri tecnici dentro una sezione `Advanced settings`
5. Lasciare i template compose con default già utili e chiarire che i manuali reali sono soprattutto i path

## Modifiche Successive

1. Rifare la `Live` con log window tipo `Dockhand`
2. Aggiungere `Pause live` / `Follow tail`
3. Rendere il report immediatamente apribile a fine PLAN/RUN
4. Rivedere eventuale duplicazione tra `Dashboard`, `Live` e `Run`

# Note UI Live di MediaShrinker

Ultimo aggiornamento: 2026-05-15

## Obiettivo

Definire le prossime modifiche UX della pagina `live` prima di implementarle.

Questo file è volutamente orientato alla discussione.
Niente qui dentro va considerato una decisione finale finché non viene confermato.

## Problemi Attuali

### 1. La pagina live diventa troppo lunga

Problema attuale:

- la pagina cresce troppo in verticale
- la sezione log spinge tutto verso il basso
- leggere le informazioni importanti diventa faticoso

Direzione desiderata:

- farla assomigliare di più a `Dockhand`
- tenere i log dentro un pannello ad altezza fissa
- usare uno scrollbar interno invece di allungare tutta la pagina

### 2. I log hanno bisogno di controlli migliori

Problema attuale:

- i log sono renderizzati semplicemente inline nella pagina
- non c’è una vera sensazione di “tail live” controllabile
- non c’è modo di mettere in pausa l’auto-refresh mentre si legge

Direzione desiderata:

- finestra log ad altezza fissa
- auto-scroll quando si è in modalità live
- controlli `Pausa` / `Riprendi`
- toggle opzionale `Segui tail`
- opzionale `Vai in fondo`

Comportamento di riferimento:

- simile ai log container di `Dockhand`
- viewport stabile mentre si leggono righe vecchie
- scelta esplicita tra:
  - modalità live-follow
  - modalità pausa/ispezione manuale

### 3. La visibilità del report è troppo debole

Messaggio osservato:

```text
[DB] Run salvato in /reports/mediashrinker_runs.sqlite
Plan complete. Report: /reports/run-20260515-204352.json
```

Problema attuale:

- il report esiste, ma la UI non lo rende immediatamente evidente
- dopo un PLAN completato, il prossimo click utile dovrebbe essere ovvio

Direzione desiderata:

- rendere il report subito visibile nella `live`
- mostrare una CTA chiara quando PLAN o RUN finiscono:
  - `Apri report run`
  - `Apri dettagli run`
  - `Apri JSON raw`
- se possibile, mostrare il link alla run corrente appena il run id è disponibile

### 4. Alcune sezioni della live sono troppo tecniche / poco utili

Problema attuale:

- `Run scope`
- `Config`
- `Totals`

Non sono inutili, ma non sono la cosa principale da guardare durante l’esecuzione live.

Direzione desiderata:

- la `live` deve dare priorità alla visibilità operativa
- il dettaglio tecnico/config andrebbe spostato più in basso, collassato, oppure tolto dalla vista di default
- la pagina di analisi dettagliata esiste già:
  - `http://192.168.1.1:8787/run?id=1`

## Direzione Prodotto Suggerita

### La live dovrebbe diventare una “console operativa”

Scopo principale:

- vedere cosa sta facendo il PLAN o il RUN in questo momento
- capire se il job è in salute
- individuare file corrente / fase corrente / decisioni recenti
- saltare rapidamente al report finale

Contenuti da enfatizzare:

- stato corrente
- file corrente / titolo corrente
- fase corrente
  - scanning
  - analyzing
  - transcode
  - subtitle work
  - finalize
- eventi recenti importanti
- pannello log live
- link chiaro al report finale

Contenuti secondari:

- preview della coda
- elementi completati di recente

Contenuti a bassa priorità:

- dump raw della config
- dump raw dei totals
- payload tecnici completi in stile JSON

## Idea di Layout Proposta

### Area alta

- header compatto
- run id corrente
- badge stato
- badge modalità (`PLAN` / `RUN`)
- azioni rapide:
  - `Apri report run`
  - `Apri JSON`
  - `Pausa live`
  - `Riprendi live`

### Corpo principale

Colonna sinistra:

- stato attivo / file corrente
- eventi recenti
- preview coda o decisioni correnti

Colonna destra:

- console log ad altezza fissa
- scroll interno
- controlli live-follow

### Area bassa

- sezioni opzionali collassate:
  - config
  - totals
  - payload tecnico

## Idee UX Specifiche da Valutare

### Opzione A: live minimale orientata all’operatore

Tenere solo:

- stato corrente
- file corrente
- eventi recenti
- console log
- link al report

Pro:

- molto focalizzata
- più facile da leggere

Contro:

- meno dettaglio tecnico direttamente in pagina

### Opzione B: pagina orientata all’operatore con sezioni avanzate collassabili

Vista di default:

- UI minimale operativa

Pannelli opzionali collassati:

- config
- totals
- payload raw

Pro:

- miglior compromesso
- conserva la diagnostica senza sporcare la vista principale

Contro:

- logica UI leggermente più complessa

### Opzione C: live a tab

Tab:

- `Overview`
- `Log`
- `Advanced`

Pro:

- layout molto controllato
- evita la pagina troppo lunga

Contro:

- più complessità
- può rallentare la lettura rapida

## Idee sulla Visibilità del Report

### Candidata migliore

Appena la run è nota:

- mostrare `Run #N`
- rendere cliccabile il numero run
- aggiungere bottone evidente:
  - `Apri dettagli run`

A completamento:

- mostrare banner riassuntivo di successo/fallimento
- mostrare:
  - `Apri report run`
  - `Apri JSON raw`

### Nice-to-have

- se un PLAN completa, mostrare subito un mini-riassunto:
  - file scansionati
  - file in coda
  - tipi di lavoro stimati

## Idee sul Comportamento della Console Log

### Baseline

- altezza fissa, circa `320px` a `480px`
- monospace
- pannello stile console scuro
- auto-refresh ogni 2s in modalità live

### Controlli

- `Live: ON/OFF`
- `Auto-scroll: ON/OFF`
- `Vai in fondo`

### Opzionale

- evidenziazione livelli/tag log:
  - `[ERR]`
  - `[WRN]`
  - `[PLAN]`
  - `[RUN]`
  - `[DB]`
  - `[STOP]`

## Domande Aperte

1. La `live` deve mostrare soprattutto:
   - card strutturate di stato
   - oppure una vista più log-first?
2. La `queue preview` deve restare nella `live`, oppure stare soprattutto nella `dashboard`?
3. `config` e `totals` devono:
   - sparire dalla `live`
   - oppure diventare collassabili?
4. Quando un PLAN finisce, la pagina deve:
   - restare sulla `live`
   - oppure spingere visivamente l’utente verso `/run?id=N`?

## Direzione Consigliata

Raccomandazione attuale:

- scegliere **Opzione B**
- rendere la `live` operator-first
- tenere i log in una console ad altezza fissa
- aggiungere controlli pausa/live-follow
- rendere subito visibile il link al report run
- declassare `Run scope`, `Config` e `Totals` in sezioni avanzate collassabili oppure toglierle dalla vista di default

## Prossimo Passo di Implementazione

Se confermato, il prossimo giro di codice dovrebbe fare questo:

1. Ridisegnare la `live` come layout operatore a due colonne
2. Sostituire il render log lungo inline con una console ad altezza fissa
3. Aggiungere controlli `Pausa live` e `Segui tail` via JS lato browser
4. Rendere prominenti i link al report run
5. Spostare `config` / `totals` in sezioni avanzate collassabili o rimuoverle

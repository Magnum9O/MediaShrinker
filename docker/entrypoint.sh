#!/usr/bin/env sh
# MediaShrinker entrypoint
# Comandi: web (default) | plan | run | cleanup | watchd | hwcheck | shell
set -eu

APP_DIR="/opt/mediashrinker/app"
REPORT_DIR="${MEDIA_REPORT_DIR:-/reports}"
STAGING_DIR="${MEDIA_STAGING_DIR:-/staging}"
MOVIES_DIR="${MEDIA_MOVIES_DIR:-/data/movies}"
TV_DIR="${MEDIA_TV_DIR:-/data/tv}"
HOST="${MEDIA_HOST:-0.0.0.0}"
PORT="${MEDIA_PORT:-8787}"
ENCODER="${MEDIA_ENCODER:-auto}"
JOBS="${MEDIA_JOBS:-1}"
OCR_ENGINE="${MEDIA_OCR_ENGINE:-pgsrip}"
OCR_LANGS="${MEDIA_OCR_LANGS:-ita,eng}"
TESSDATA="${MEDIA_TESSDATA_PREFIX:-/usr/share/tesseract-ocr/5/tessdata}"
PGSRIP_BIN="${MEDIA_PGSRIP_BIN:-pgsrip}"
ENCODING_PROFILE="${MEDIA_ENCODING_PROFILE:-balanced}"
NOTIFY_URL="${MEDIA_NOTIFY_URL:-}"
WATCH_INTERVAL="${MEDIA_WATCH_INTERVAL:-3600}"

mkdir -p "$REPORT_DIR" "$STAGING_DIR"

# ---------------------------------------------------------------------------
# Rileva il miglior encoder HEVC disponibile
# ---------------------------------------------------------------------------
detect_encoder() {
  case "$ENCODER" in
    auto) ;;
    *) printf '%s\n' "$ENCODER"; return ;;
  esac

  FFENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"

  if printf '%s\n' "$FFENCODERS" | grep -q 'hevc_nvenc' \
     && { [ -e /dev/nvidia0 ] || [ -e /proc/driver/nvidia/version ]; }; then
    printf '%s\n' "hevc_nvenc"
    return
  fi

  if printf '%s\n' "$FFENCODERS" | grep -q 'hevc_vaapi' \
     && [ -e /dev/dri/renderD128 ]; then
    printf '%s\n' "hevc_vaapi"
    return
  fi

  printf '%s\n' "libx265"
}

bool_enabled() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|y|Y|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Esegue mediashrinker.py — NON usa exec, usabile anche in loop (watchd)
# Argomenti: <mode_flag> [extra_flag]
# ---------------------------------------------------------------------------
do_run() {
  _mode_flag="$1"
  _extra="${2:-}"

  SELECTED_ENCODER="$(detect_encoder)"

  EXTRACT_ARG="--extract-pgs"
  bool_enabled "${MEDIA_EXTRACT_PGS:-1}" || EXTRACT_ARG="--no-extract-pgs"

  EXTERNAL_SUBS_ARG="--add-external-text-subs"
  bool_enabled "${MEDIA_ADD_EXTERNAL_TEXT_SUBS:-1}" || EXTERNAL_SUBS_ARG="--no-add-external-text-subs"

  MULTIPASS_ARG=""
  bool_enabled "${MEDIA_NO_MULTIPASS:-0}" && MULTIPASS_ARG="--no-multipass"

  # shellcheck disable=SC2086
  python "$APP_DIR/mediashrinker.py" \
    "$_mode_flag" $_extra \
    --yes \
    --library              "${MEDIA_LIBRARY:-both}" \
    --movies-root          "$MOVIES_DIR" \
    --series-root          "$TV_DIR" \
    --staging-dir          "$STAGING_DIR" \
    --report-dir           "$REPORT_DIR" \
    --encoder              "$SELECTED_ENCODER" \
    --encoding-profile     "$ENCODING_PROFILE" \
    --jobs                 "$JOBS" \
    --bitrate-threshold-mbps "${MEDIA_BITRATE_THRESHOLD_MBPS:-55.0}" \
    --bitrate-4k-mbps      "${MEDIA_BITRATE_4K_MBPS:-45.0}" \
    --ocr-engine           "$OCR_ENGINE" \
    --ocr-target-langs     "$OCR_LANGS" \
    --pgsrip-bin           "$PGSRIP_BIN" \
    --tessdata-prefix      "$TESSDATA" \
    --notify-url           "$NOTIFY_URL" \
    $EXTRACT_ARG \
    $EXTERNAL_SUBS_ARG \
    $MULTIPASS_ARG \
    --no-save-config
}

# ---------------------------------------------------------------------------
# Dispatcher principale
# ---------------------------------------------------------------------------
CMD="${1:-web}"
shift || true

case "$CMD" in

  web)
    exec python "$APP_DIR/mediashrinker_web.py" \
      --db   "$REPORT_DIR/mediashrinker_runs.sqlite" \
      --host "$HOST" \
      --port "$PORT"
    ;;

  plan)
    do_run "--plan"
    ;;

  run)
    DELETE_BAK_ARG=""
    bool_enabled "${MEDIA_DELETE_BAK:-0}" && DELETE_BAK_ARG="--delete-bak"
    do_run "--run" "$DELETE_BAK_ARG"
    ;;

  cleanup)
    do_run "--cleanup-only"
    ;;

  # Demone periodico: ripete RUN ogni MEDIA_WATCH_INTERVAL secondi (default 3600)
  watchd)
    DELETE_BAK_ARG=""
    bool_enabled "${MEDIA_DELETE_BAK:-0}" && DELETE_BAK_ARG="--delete-bak"
    while true; do
      _T0="$(date +%s)"
      do_run "--run" "$DELETE_BAK_ARG" || true
      _T1="$(date +%s)"
      _SLEEP=$(( WATCH_INTERVAL - (_T1 - _T0) ))
      [ "$_SLEEP" -gt 0 ] && sleep "$_SLEEP"
    done
    ;;

  hwcheck)
    exec mediashrinker-hwcheck
    ;;

  shell)
    exec /bin/sh
    ;;

  *)
    exec "$CMD" "$@"
    ;;

esac

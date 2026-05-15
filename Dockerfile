FROM python:3.13-slim-bookworm

# Runtime user — override con --build-arg PUID=1234 PGID=1234
ARG PUID=1000
ARG PGID=1000

# Lingue Tesseract da installare a build-time (nomi pacchetti apt, space-separated).
# Aggiungere lingue: --build-arg TESSDATA_LANGS="eng ita fra deu spa por nld"
ARG TESSDATA_LANGS="eng ita"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    MEDIA_MOVIES_DIR=/data/movies \
    MEDIA_TV_DIR=/data/tv \
    MEDIA_STAGING_DIR=/staging \
    MEDIA_REPORT_DIR=/reports \
    MEDIA_HOST=0.0.0.0 \
    MEDIA_PORT=8787

RUN set -e; \
    TESS_PKGS=""; \
    for lang in ${TESSDATA_LANGS}; do TESS_PKGS="$TESS_PKGS tesseract-ocr-$lang"; done; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        mkvtoolnix \
        tesseract-ocr \
        $TESS_PKGS \
        tini \
        vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mediashrinker

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/mediashrinker-entrypoint
COPY docker/hwcheck.sh    /usr/local/bin/mediashrinker-hwcheck

RUN chmod +x /usr/local/bin/mediashrinker-entrypoint /usr/local/bin/mediashrinker-hwcheck \
    && GROUP_NAME="$(getent group "${PGID}" | cut -d: -f1 || true)" \
    && if [ -z "${GROUP_NAME}" ]; then groupadd -g "${PGID}" appuser; GROUP_NAME=appuser; fi \
    && useradd  -u "${PUID}" -g "${GROUP_NAME}" -M -s /sbin/nologin appuser \
    && mkdir -p /data/movies /data/tv /staging /reports \
    && chown -R "appuser:${GROUP_NAME}" /opt/mediashrinker /staging /reports

USER appuser

EXPOSE 8787

HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "mediashrinker-entrypoint"]
CMD ["web"]

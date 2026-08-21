FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="AceUnzipBot" \
      org.opencontainers.image.description="Secure owner-controlled Telegram archive extractor" \
      org.opencontainers.image.source="https://github.com/Aceddev/AceUnzipBot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Ace <Acedevmc@gmail.com>"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg p7zip-full p7zip-rar zstd ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 acebot
WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=acebot:acebot . .
RUN mkdir -p /app/data /app/work \
    && chown -R acebot:acebot /app \
    && install -m 0755 scripts/docker-entrypoint.sh /usr/local/bin/aceunzip-entrypoint

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD gosu acebot python -m acebot --healthcheck || exit 1

ENTRYPOINT ["/usr/local/bin/aceunzip-entrypoint"]
CMD ["python", "-m", "acebot"]

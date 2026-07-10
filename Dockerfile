FROM python:3.11-slim

ARG PUID=1000
ARG PGID=1000

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gifsicle gosu \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "$PGID" app \
    && useradd -m -u "$PUID" -g "$PGID" app \
    && mkdir -p /library /state \
    && chown -R app:app /library /state

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PUID=99 \
    PGID=100

EXPOSE 904

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:904/healthz', timeout=3)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:904", "--workers", "1", "--threads", "8", "--graceful-timeout", "10", "--timeout", "60", "app.wsgi:app"]

FROM python:3.11-slim

ARG PUID=1000
ARG PGID=1000

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gosu \
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

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-u", "-m", "app.main"]

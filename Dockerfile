FROM python:3-slim

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        ffmpeg gosu ca-certificates libchromaprint-tools curl unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    apt-get purge -y curl unzip && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --upgrade -r requirements.txt

COPY . .

RUN mkdir -p /config && sed -i 's/\r//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 5000
ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1
ENV LIDARR_URL=""
ENV TELEGRAM_ENABLED="false"
ENV TELEGRAM_CHAT_ID=""
ENV SCHEDULER_ENABLED="false"
ENV SCHEDULER_INTERVAL="60"
ENV SCHEDULER_AUTO_DOWNLOAD="false"
ENV PUID=0
ENV PGID=0
ENV UMASK=002
ENV DISCORD_ENABLED="false"
ENV ACOUSTID_ENABLED="true"

ENTRYPOINT ["/app/entrypoint.sh"]

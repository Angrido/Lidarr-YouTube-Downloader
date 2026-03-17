FROM python:alpine

RUN apk add --no-cache ffmpeg gosu ca-certificates chromaprint

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /config && chmod +x /app/entrypoint.sh

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
ENV ACOUSTID_API_KEY=""

ENTRYPOINT ["/app/entrypoint.sh"]

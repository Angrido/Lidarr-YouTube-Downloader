FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /downloads /config

EXPOSE 5000

ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1
ENV LIDARR_URL=""
ENV LIDARR_API_KEY=""
ENV TELEGRAM_ENABLED="false"
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""
ENV SCHEDULER_ENABLED="false"
ENV SCHEDULER_INTERVAL="60"

CMD ["python", "app.py"]

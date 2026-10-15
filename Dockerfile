# Everything floats to its latest release on each build: the Python base image,
# the Debian packages, Deno and every Python dependency. Build with
# `--pull --no-cache`, or Docker reuses cached layers and nothing updates.
#
# Two guards keep "latest" from shipping a broken image:
# - wheels are built in a throwaway stage that has a compiler, because a
#   brand-new Python often has no prebuilt wheel yet for some packages
#   (Python 3.15 and brotli), and the slim runtime image has no gcc;
# - the build fails on a dependency conflict (`pip check`) or when the app no
#   longer imports on this Python, instead of producing an image that crashes.
ARG PYTHON_IMAGE=python:3-slim

FROM ${PYTHON_IMAGE} AS wheels

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM ${PYTHON_IMAGE}

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        ffmpeg gosu ca-certificates libchromaprint-tools curl unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    apt-get purge -y curl unzip && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
COPY --from=wheels /wheels /wheels

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt && \
    rm -rf /wheels && \
    pip check

COPY . .

# Smoke test: every module imports on this Python with these libraries.
RUN mkdir -p /config && sed -i 's/\r//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh && \
    python -c "import app" && \
    deno --version >/dev/null && ffmpeg -version >/dev/null && fpcalc -version >/dev/null

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/app/entrypoint.sh"]

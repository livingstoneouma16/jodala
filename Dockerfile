# Production image for Jodala Microfinance.
#
# Build:  docker build -t jodala-microfinance .
# Run:    docker run -p 8000:8000 --env-file .env.production \
#           -v jodala-data:/data jodala-microfinance
#
# The SQLite database lives at /data/sacco.db (see DB_PATH below) on a named
# volume so it survives container restarts/redeploys -- without that volume
# every deploy wipes all data.

FROM python:3.12-slim AS base

# Keep image layers small and predictable; don't write .pyc files into the
# image, and flush stdout/stderr immediately so logs show up in real time
# under `docker logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build `cryptography` and `pillow` wheels on slim,
# plus gosu (a minimal, purpose-built su/sudo replacement) used by
# entrypoint.sh to drop from root to the app user after fixing volume
# ownership -- see that file for why this is necessary.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# Create the unprivileged user the app actually runs as. Deliberately NOT
# switching to it here with USER -- the container must start as root so
# entrypoint.sh can fix ownership of a freshly-mounted volume (which
# arrives root-owned on Render/Railway/Fly.io) before it execs into
# appuser for the real gunicorn process. The app itself never runs as root.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

ENV APP_ENV=production \
    DB_PATH=/data/sacco.db \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else sys.exit(1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]

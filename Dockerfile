# Production image for Jodala Microfinance.
#
# Build:  docker build -t jodala-microfinance .
# Run:    docker run -p 8000:8000 --env-file .env.production \
#           jodala-microfinance
#
# Data lives in PostgreSQL, reached over the network via DATABASE_URL --
# point that at a managed Postgres instance (Render/Railway/Fly.io addon)
# or the `postgres` service in docker-compose.yml. There's no local volume
# to manage; every deploy is stateless as far as this container is concerned.

# ---------------------------------------------------------------------------
# Stage 1: build the /v3 React frontend (frontend/ -> frontend/dist).
# core/routes/v3.py serves frontend/dist directly and 404s ("Frontend not
# built yet") if it's missing -- this stage is what makes sure it isn't.
# Kept separate from the final image so the ~200MB+ node_modules tree and
# the Node runtime itself never end up in the image that actually ships.
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

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
# entrypoint.sh to drop from root to the app user before exec'ing the real
# process. libpq-dev isn't required: psycopg2-binary ships libpq statically
# bundled in its wheel.
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
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN chmod +x entrypoint.sh

# Create the unprivileged user the app actually runs as. Deliberately NOT
# switching to it here with USER -- the container must start as root so
# entrypoint.sh can drop privileges cleanly via gosu before exec'ing into
# the real gunicorn process. The app itself never runs as root.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app

ENV APP_ENV=production \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else sys.exit(1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]

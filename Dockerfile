FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_PYTHON_DOWNLOADS=0
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    postgresql-client \
    netcat-traditional \
    procps \
    supervisor \
    curl \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the committed lockfile.
# --frozen (not --locked): install strictly from the lock with NO re-resolve —
# --locked re-resolves against the live index, which is nondeterministic inside
# kaniko's build-ns network view (the multi-day pulse deploy blockage, PLS-282
# class) and left this image with a venv missing its bin/ entrypoints entirely
# (connect-bb6b410: python/daphne/celery/mcp all absent → supervisord FATAL
# crashloop, 2026-07-09). Matches the pulse Dockerfile pattern.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project --no-editable
# Fail the BUILD loudly if the venv is missing its entrypoints — a venv with
# packages but an empty bin/ otherwise ships silently and only explodes as a
# supervisord "can't find command" crashloop at rollout time.
RUN test -x /app/.venv/bin/python && test -x /app/.venv/bin/daphne \
    && test -x /app/.venv/bin/celery \
    && /app/.venv/bin/python -c "import mcp" \
    || { echo "FATAL: /app/.venv is incomplete (missing bin entrypoints or mcp package)"; ls -la /app/.venv/bin; exit 1; }

# Copy project
COPY . /app/

# Collect static files
RUN SECRET_KEY=build-static-only POSTGRES_PASSWORD=build-static-only MINIO_ACCESS_KEY=build-static-only MINIO_SECRET_KEY=build-static-only MINIO_ENDPOINT=http://127.0.0.1:9 python manage.py collectstatic --noinput

# Copy entrypoint and supervisord configs
COPY docker-entrypoint.sh /app/
COPY supervisord.conf /app/
COPY supervisord.web.conf /app/
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8019 18108

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]

# PLAT-10781: rebuild-trigger after 3.11-slim base cascade (ruling: in-scope; no behavioral change).

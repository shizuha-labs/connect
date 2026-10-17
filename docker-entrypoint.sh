#!/bin/bash
# docker-entrypoint.sh — Role-aware entrypoint for Shizuha Connect
#
# PLAT-4143 / CON-219: The image ships a single supervisor that runs MCP, Daphne,
# Celery worker, Celery beat, AND migrations. When HPA scales web replicas, it
# multiplies worker/beat/migration processes, causing cgroup throttling and
# liveness restart storms.
#
# This entrypoint accepts CONNECT_ROLE to run only the processes for that role:
#   web      — Daphne + MCP (stateless web tier, HPA-safe)
#   worker   — Celery worker only
#   beat     — Celery beat only (singleton scheduler)
#   migrate  — Run migrations and exit
#
# Default: web (backward compatible for existing chart without explicit role).
# Invalid role: fail closed with an error.
#
# Usage:
#   docker run -e CONNECT_ROLE=web ...
#   docker run -e CONNECT_ROLE=worker ...
#   docker run -e CONNECT_ROLE=beat ...
#   docker run -e CONNECT_ROLE=migrate ...

set -euo pipefail

ROLE="${CONNECT_ROLE:-web}"

# --- Validate role ---
case "$ROLE" in
  web|worker|beat|migrate) ;;
  *)
    echo "FATAL: invalid CONNECT_ROLE='$ROLE'. Must be one of: web, worker, beat, migrate"
    exit 1
    ;;
esac

# --- Wait for dependencies (all roles except migrate need them) ---
if [ "$ROLE" != "migrate" ]; then
  # Wait for PostgreSQL
  echo "Waiting for PostgreSQL..."
  while ! nc -z "${POSTGRES_HOST:-localhost}" "${POSTGRES_PORT:-5432}"; do
    sleep 1
  done
  echo "PostgreSQL is available"

  # Wait for Redis - parse from CELERY_BROKER_URL if REDIS_HOST not set
  if [ -z "$REDIS_HOST" ] && [ -n "$CELERY_BROKER_URL" ]; then
    export REDIS_HOST=$(echo "$CELERY_BROKER_URL" | sed -n 's|redis://\([^:]*\):.*|\1|p')
    export REDIS_PORT=$(echo "$CELERY_BROKER_URL" | sed -n 's|redis://[^:]*:\([0-9]*\)/.*|\1|p')
  fi
  REDIS_CHECK_HOST="${REDIS_HOST:-localhost}"
  REDIS_CHECK_PORT="${REDIS_PORT:-6379}"
  echo "Waiting for Redis at ${REDIS_CHECK_HOST}:${REDIS_CHECK_PORT}..."
  while ! nc -z "$REDIS_CHECK_HOST" "$REDIS_CHECK_PORT"; do
    sleep 1
  done
  echo "Redis is available"
fi

# --- Role dispatch ---
case "$ROLE" in
  web)
    # Web role: Daphne + MCP only. No Celery worker, beat, or migrations.
    echo "Starting Shizuha Connect (role: web)..."
    exec supervisord -c /app/supervisord.web.conf
    ;;

  worker)
    # Worker role: Celery worker only.
    echo "Starting Shizuha Connect (role: worker)..."
    exec celery -A connectmanager worker -l INFO --concurrency=2
    ;;

  beat)
    # Beat role: Celery beat scheduler only (singleton).
    echo "Starting Shizuha Connect (role: beat)..."
    exec celery -A connectmanager beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;

  migrate)
    # Migrate role: run migrations and exit.
    echo "Running migrations..."
    python manage.py migrate --noinput
    python manage.py migrate --run-syncdb --noinput 2>/dev/null || true
    echo "Migrations complete."
    exit 0
    ;;
esac

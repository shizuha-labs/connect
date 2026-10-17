#!/usr/bin/env bash
# Fast local mirror of the required Connect backend + frontend premerge gate.
#
# Reuses the deployed image's dependency layer and PostgreSQL data volume while
# bind-mounting the current source read-only. The generic shared hook used
# to rebuild Debian + PostgreSQL + Redis + the Python environment and clone one
# database per host CPU on every push (304s in the 2026-07-14 baseline).
# Warm measurement: 519 tests in 18.2s, 31s end-to-end (8 workers).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "gate: uv lock consistency"
command -v uv >/dev/null 2>&1 || {
  echo "gate: uv is required to verify pyproject.toml against uv.lock" >&2
  exit 1
}
env -u UV_DEFAULT_INDEX -u UV_INDEX_URL -u UV_INSECURE_HOST \
  uv lock --check --no-progress

echo "gate: frontend clean install + lint + unit + production build"
docker run --rm --network host \
  -v "$PWD/frontend:/src:ro" \
  -e NPM_CONFIG_REGISTRY=http://127.0.0.1:30512/ \
  node:22-bookworm bash -lc '
    set -euo pipefail
    mkdir -p /tmp/frontend
    tar -C /src --exclude=node_modules --exclude=dist --exclude=playwright-report --exclude=test-results -cf - . \
      | tar -C /tmp/frontend -xf -
    cd /tmp/frontend
    npm ci
    npm run lint
    npm test
    npm run build
  '

exec 9>/tmp/connect-gate.volume.lock
flock -w 900 9 || {
  echo "gate: another Connect gate held the shared DB lock for >15min" >&2
  exit 1
}

IMG="${SHIZUHA_CI_IMAGE:-}"
if [ -z "$IMG" ] && command -v kubectl >/dev/null 2>&1; then
  IMG=$(kubectl get deploy -n shizuha shizuha-connect \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
fi
IMG="${IMG:-localhost:30500/shizuha-connect:connect-73842be}"
TEST_JOBS="${SHIZUHA_TEST_JOBS:-8}"

NET="connect-gate-$$"
PG="connect-gate-pg-$$"
RD="connect-gate-redis-$$"
VOLUME=connect-gate-pgdata

cleanup() {
  if docker inspect "$PG" >/dev/null 2>&1; then
    docker exec -u postgres "$PG" \
      pg_ctl -D /var/lib/postgresql/data -m fast -w stop >/dev/null 2>&1 \
      || docker stop -t 20 "$PG" >/dev/null 2>&1 \
      || true
  fi
  docker rm -f "$PG" "$RD" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_postgres() {
  docker run -d --name "$PG" --network "$NET" \
    -v "$VOLUME:/var/lib/postgresql/data" \
    -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=shizuha_connect_ci \
    postgres:16-alpine \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
    -c autovacuum=off >/dev/null
}

wait_postgres() {
  local _attempt
  for _attempt in $(seq 1 90); do
    docker exec "$PG" pg_isready -U postgres -d shizuha_connect_ci >/dev/null 2>&1 \
      && return 0
    sleep 1
  done
  return 1
}

docker volume create "$VOLUME" >/dev/null
docker network create "$NET" >/dev/null
start_postgres
docker run -d --name "$RD" --network "$NET" redis:7-alpine >/dev/null

if ! wait_postgres; then
  echo "gate: cached PostgreSQL volume did not recover; recreating it once" >&2
  docker rm -f "$PG" >/dev/null 2>&1
  docker volume rm "$VOLUME" >/dev/null 2>&1
  docker volume create "$VOLUME" >/dev/null
  start_postgres
  wait_postgres || {
    echo "gate: PostgreSQL still not ready after cache recreation" >&2
    exit 1
  }
fi

echo "gate: image=$IMG workers=$TEST_JOBS"
docker run --rm --network "$NET" \
  -v "$PWD/connect:/app/connect:ro" \
  -v "$PWD/connectmanager:/app/connectmanager:ro" \
  -v "$PWD/frontend/package.json:/app/frontend/package.json:ro" \
  -v "$PWD/.forgejo:/app/.forgejo:ro" \
  -v "$PWD/.prepush-test.sh:/app/.prepush-test.sh:ro" \
  -e POSTGRES_DB=shizuha_connect_ci \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_HOST="$PG" \
  -e POSTGRES_PORT=5432 \
  -e REDIS_URL="redis://$RD:6379/5" \
  -e CACHE_REDIS_URL="redis://$RD:6379/6" \
  -e REDIS_CHANNEL_URL="redis://$RD:6379/7" \
  -e CELERY_BROKER_URL="redis://$RD:6379/8" \
  -e CELERY_RESULT_BACKEND="redis://$RD:6379/8" \
  -e REDIS_HOST="$RD" \
  -e REDIS_PORT=6379 \
  -e DEBUG=True \
  -e SECRET_KEY=ci-secret \
  -e MINIO_ACCESS_KEY=ci-storage \
  -e MINIO_SECRET_KEY=ci-storage \
  -e JWT_SECRET_KEY=ci-secret \
  -e SHIZUHA_JWT_SIGNING_KEY=ci-secret \
  --entrypoint sh "$IMG" -c \
  "uv pip install -q --python /app/.venv/bin/python tblib 2>/dev/null || pip install -q tblib 2>/dev/null; python manage.py makemigrations --check --dry-run; python .forgejo/scripts/check-django-migration-graph.py; python manage.py test --noinput --parallel $TEST_JOBS"

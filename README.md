# Shizuha Connect

Shizuha Connect is the platform's hybrid social-networking service — LinkedIn-style
professional graph + Twitter/X-style real-time feed. Django + DRF backend on
port `8019`, React frontend on port `5183`, PostgreSQL `shizuha_connect`,
Redis DB `8`, URL-mounted at `/connect/`.

**Key capabilities:**

- Professional profiles, social posts, stories, and real-time feeds
- Professional connections and shared content
- Direct and group messaging with idempotent sends and reconnect replay
- Hive personal-agent discovery and owner-scoped message delivery
- RS256/JWKS authentication with token-revocation enforcement
- Search, onboarding, presence, and people suggestions
- WhatsApp and other external-channel bridge delivery
- Drive, Pulse, Wiki, and Notes sharing integrations

For the full API surface and operational details, see `CLAUDE.md` in this
repo and the **Connect** space in the Shizuha wiki.

Product planning docs live under `docs/product/`; start with
`docs/core-boundary.md` for external Guild integration and upgrade compatibility.

## Running tests locally

Contributors should be able to run the full CI test pass on their workstation
without reading the Forgejo workflow. The recipe matches what
`.forgejo/workflows/premerge.yml` runs in CI; that workflow is the authoritative
source if anything below ever drifts.

### Prerequisites

- Python `3.12` (the version used by the CI gate).
- `uv==0.11.26` and the committed `uv.lock`:

  ```bash
  python -m pip install uv==0.11.26
  env -u UV_INDEX_URL -u UV_DEFAULT_INDEX -u UV_INSECURE_HOST uv lock --check
  uv sync --frozen --no-dev --no-install-project --no-editable
  ```

  The lock check intentionally ignores alternate-index environment variables:
  they change resolver input and can make an unchanged lockfile look stale.

- A local PostgreSQL reachable as `shizuha_connect` (the default `settings.py`
  database name) and a Redis instance for Channels / Celery. Both are wired
  up automatically inside the Shizuha platform docker-compose; outside of it,
  point your environment at any reachable Postgres + Redis.

### Static and migration checks

Run the same source and migration guards before the Django suite:

```bash
bash .forgejo/scripts/check-no-curl-pipe-shell.sh
uv run python -m compileall -q connect connectmanager
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python .forgejo/scripts/check-django-migration-graph.py
```

### Tests (Django)

```bash
uv run python manage.py test --noinput --keepdb --verbosity=1
```

`connectmanager/test_settings.py` is present on `master`; no auxiliary branch
or pull-request ref is required.

### Pulse task reference

CI also enforces that every PR title contains a Pulse task key
(`[#PLAT-31]`, `[#AT-91]`, `#PLAT-6`, etc. — two-to-five uppercase letters,
dash, digits). Pure local runs don't care, but the Origin PR gate rejects a
title that does not match.

### CI workflow (authoritative)

The recipes above mirror `.forgejo/workflows/premerge.yml`. If you need the exact
runner image, Python version, or job ordering, read that file directly —
it's the single source of truth and supersedes anything written here.

Runtime configuration requires `SECRET_KEY`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_ENDPOINT` and `POSTGRES_PASSWORD`. Generate private values for a new installation; an existing deployment must preserve its existing effective values before upgrading. These values have no source fallback.

Video meetings additionally require a reachable LiveKit server and explicitly configured `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `LIVEKIT_EXTERNAL_URL`; unavailable installations return a clear error instead of minting demo tokens.

All RS256 token types, including sessions and agents without `client_id`, are checked against ID. A token version older than the authoritative current version is rejected; malformed present versions deny access without caching. Revocation checks accept an optional `SHIZUHA_REVOCATION_STATUS_URL` for the trusted ID transport endpoint, independently of the canonical `SHIZUHA_EXPECTED_ISSUER`. Without it, Connect uses the issuer plus `/api/oauth/revocation-status`. Redirects and malformed verdicts deny access. Lookup failures deny the current request without caching a revoked result; authoritative boolean verdicts retain the existing 30-second cache.

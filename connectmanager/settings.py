"""
Django settings for Shizuha Connect.

Social networking service combining LinkedIn's professional networking
with Twitter/X's real-time social feeds.
"""

import os
import sys
from pathlib import Path
from datetime import timedelta

def _required_env(name):
    value = os.environ.get(name)
    if not value:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(f'{name} must be configured')
    return value


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = _required_env('SECRET_KEY')
CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET = os.environ.get('CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET', '')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

# Shared HMAC secret for internal system-message origin verification.
# Pulse and other internal callers sign X-System-Message-Token with this value;
# without reading the env var, every hardened /internal/system-message/ POST 403s.
CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET = os.environ.get('CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET', '')
CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS = int(os.environ.get('CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS', '300'))
CONNECT_SYSTEM_MESSAGE_ALLOWLIST = {
    service.strip()
    for service in os.environ.get(
        'CONNECT_SYSTEM_MESSAGE_ALLOWLIST',
        'shizuha-agent,shizuha-connect,shizuha-pulse,forgejo-bridge',
    ).split(',')
    if service.strip()
}

def _env_csv(name, default):
    return [value.strip() for value in os.environ.get(name, default).split(',') if value.strip()]


ALLOWED_HOSTS = _env_csv('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0,host.docker.internal,shizuha-nginx,shizuha-connect,*')

# Disable trailing slash enforcement
APPEND_SLASH = False

# Reverse proxy configuration
FORCE_SCRIPT_NAME = os.environ.get('FORCE_SCRIPT_NAME', '/shizuha-connect')
LOGIN_URL = '/admin/login/'
LOGIN_REDIRECT_URL = '/admin/'

USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Disable security features that interfere with admin behind proxy
SECURE_CONTENT_TYPE_NOSNIFF = False
SECURE_CROSS_ORIGIN_OPENER_POLICY = None

# Application definition
INSTALLED_APPS = [
    'daphne',  # ASGI server for WebSocket support
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',  # For full-text search
    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    'corsheaders',
    'django_celery_beat',
    'channels',
    'django_filters',
    # App
    'connect.apps.ConnectConfig',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'connectmanager.middleware.ScriptNameMiddleware',
    # CON-208: reject ingress-borne calls to /api/internal/ before any view runs.
    'connect.middleware.InternalApiIngressGuardMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'connect.middleware.ServiceAccessMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'connectmanager.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'connectmanager.wsgi.application'
ASGI_APPLICATION = 'connectmanager.asgi.application'

# Database
# Connect is served by Daphne/ASGI. Django explicitly recommends disabling
# persistent database connections under ASGI because sync ORM work can run on
# many executor threads, each retaining its own direct PostgreSQL session. Keep
# this service-local override available for a future reviewed pooler deployment,
# but make the safe direct-to-PostgreSQL default non-persistent.
CONNECT_DB_CONN_MAX_AGE_SECONDS = int(
    os.environ.get('CONNECT_DB_CONN_MAX_AGE_SECONDS', '0')
)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'shizuha_connect'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': _required_env('POSTGRES_PASSWORD'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': CONNECT_DB_CONN_MAX_AGE_SECONDS,
        'CONN_HEALTH_CHECKS': True,
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'shizuha-connect/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', '6379'))
REDIS_CHANNEL_DB = os.environ.get('REDIS_CHANNEL_DB', '0')
CHANNEL_REDIS_URL = os.environ.get(
    'REDIS_CHANNEL_URL',
    f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CHANNEL_DB}',
)
CHANNEL_REDIS_SOCKET_TIMEOUT = float(os.environ.get('REDIS_CHANNEL_SOCKET_TIMEOUT', '30'))
CHANNEL_REDIS_CONNECT_TIMEOUT = float(os.environ.get('REDIS_CHANNEL_CONNECT_TIMEOUT', '10'))

# Channels (WebSocket) configuration
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [{
                'address': CHANNEL_REDIS_URL,
                'socket_timeout': CHANNEL_REDIS_SOCKET_TIMEOUT,
                'socket_connect_timeout': CHANNEL_REDIS_CONNECT_TIMEOUT,
                'socket_keepalive': True,
                'health_check_interval': 30,
            }],
            'prefix': 'shizuha_connect',
            'capacity': 10000,
            'expiry': 3600,  # 1 hour — must exceed keepalive interval to prevent silent group expiry
        },
    },
}

# Celery Configuration
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://:@localhost:6379/8')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://:@localhost:6379/8')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Kolkata'
CELERY_ENABLE_UTC = True

# Task routing for isolation
CELERY_TASK_DEFAULT_QUEUE = 'shizuha_connect'
CELERY_TASK_DEFAULT_EXCHANGE = 'shizuha_connect'
CELERY_TASK_DEFAULT_ROUTING_KEY = 'shizuha_connect'

# PLAT-401: Daily standup automation. The Celery-beat task
# connect.tasks.post_daily_standup_prompt posts STANDUP_PROMPT to the
# STANDUP_CONVERSATION_ID group as STANDUP_POSTER_EMAIL (resolved from the
# group's participants) on weekdays at 09:17 IST. Defaults target the all-org
# "Daily Standup" group; override per deployment via env.
STANDUP_CONVERSATION_ID = os.environ.get(
    'STANDUP_CONVERSATION_ID', '25978164-4001-4eda-af12-d6f92e6ff58c'
)
STANDUP_POSTER_EMAIL = os.environ.get('STANDUP_POSTER_EMAIL', 'shizuha@shizuha.com')
STANDUP_PROMPT = os.environ.get('STANDUP_PROMPT', '')  # empty -> task default

# Redis connection retry settings
CELERY_BROKER_CONNECTION_RETRY = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = 10
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'visibility_timeout': 3600,
    'socket_timeout': 30,
    'socket_connect_timeout': 30,
    'socket_keepalive': True,
    'retry_on_timeout': True
}

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'connect.pagination.CreatedAtCursorPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'connect.authentication.ShizuhaJWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
        'connect.permissions.ServiceAccessPermission',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
}

# JWT settings
# CON-122: Connect user/browser JWT verification is RS256/JWKS-only in
# connect.authentication.ShizuhaJWTAuthentication. Do not configure SimpleJWT
# with a platform-wide shared signing secret for user-token verification here.

# DRF Spectacular settings
SPECTACULAR_SETTINGS = {
    'TITLE': 'Shizuha Connect API',
    'DESCRIPTION': '''
Shizuha Connect - Social Networking Platform

A hybrid social networking service combining:
- LinkedIn's professional networking features
- Twitter/X's real-time social feeds
- Full integration with the Shizuha platform

## Features

- **Profiles**: Professional profiles with skills, experience, education
- **Posts**: Rich content posts with media, mentions, hashtags
- **Feed**: Algorithmic and chronological feeds
- **Connections**: Follow users and connect professionally
- **Messaging**: Direct messages and group chats
- **Stories**: Ephemeral 24-hour content
- **Integration**: Share content from Drive, Pulse, Wiki, Notes

## Authentication

Uses JWT tokens issued by Shizuha ID service.
    ''',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': r'/api/',
    'TAGS': [
        {'name': 'Profile', 'description': 'User profile management'},
        {'name': 'Posts', 'description': 'Post creation and engagement'},
        {'name': 'Feed', 'description': 'Timeline feeds'},
        {'name': 'Social', 'description': 'Following and connections'},
        {'name': 'Messaging', 'description': 'Direct messages'},
        {'name': 'Stories', 'description': 'Ephemeral content'},
        {'name': 'Search', 'description': 'Search functionality'},
        {'name': 'Integration', 'description': 'Cross-service sharing'},
    ],
}

# CORS settings
CORS_ALLOW_ALL_ORIGINS = os.environ.get('CORS_ALLOW_ALL_ORIGINS', 'true').lower() in ('true', '1', 'yes')
CORS_ALLOWED_ORIGINS = _env_csv('CORS_ALLOWED_ORIGINS', '')

# CSRF settings
CSRF_TRUSTED_ORIGINS = _env_csv(
    'CSRF_TRUSTED_ORIGINS',
    'http://localhost:8019,http://127.0.0.1:8019,http://shizuha-nginx,https://ai.shizuha.com,https://chat.shizuha.com',
)
CSRF_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False

# Service Integration URLs
SHIZUHA_ID_API_URL = os.environ.get('SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
SHIZUHA_ADMIN_API_URL = os.environ.get('SHIZUHA_ADMIN_API_URL', 'http://shizuha-admin:8003/api')

# CON-129: shared-organization tenancy gate for messaging. Enforcement is on
# by default; the env kill-switch exists only for emergency rollback. This gate
# (connect.services.org_tenancy) is the canonical DM-send tenancy check and
# distinguishes 403 cross_org vs 503 lookup-unavailable with a two-tier stale
# cache; privacy.users_share_org delegates its membership lookup here so there
# is a single admin-membership resolution path (CON-133 convergence).
# Enforcement is ON in every real environment. Under the test runner it
# defaults OFF so the ~40 messaging/e2e tests that exercise DM flows (not the
# org gate) don't 503 on a live shizuha-admin membership lookup that no test
# fixture can reach — matching the design the dedicated suite already assumes
# ("test_con129_org_tenancy switches it ON explicitly via override_settings").
# The env kill-switch still wins when set (emergency rollback in prod).
_RUNNING_TESTS = 'test' in sys.argv
# Consumer tests drive async code via async_to_sync; asgiref can leave a thread
# event loop set, which trips Django's async_unsafe guard on the NEXT test's
# sync ORM (SynchronousOnlyOperation) — the MissedMessage suite passes in
# isolation but errored after an async test. Tests are single-threaded with no
# real concurrency, so allowing sync-in-async is safe here (Django's documented
# escape hatch). Never set in a real environment.
if _RUNNING_TESTS:
    os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')
CONNECT_ENFORCE_ORG_TENANCY = (
    (os.environ['CONNECT_ENFORCE_ORG_TENANCY'].lower() != 'false')
    if 'CONNECT_ENFORCE_ORG_TENANCY' in os.environ
    else (not _RUNNING_TESTS)
)
CONNECT_ORG_CACHE_TTL_SECONDS = int(os.environ.get('CONNECT_ORG_CACHE_TTL_SECONDS', '300'))
CONNECT_ORG_CACHE_STALE_TTL_SECONDS = int(os.environ.get('CONNECT_ORG_CACHE_STALE_TTL_SECONDS', '3600'))
SHIZUHA_DRIVE_API_URL = os.environ.get('SHIZUHA_DRIVE_API_URL', 'http://shizuha-drive:8018/api')
SHIZUHA_PULSE_API_URL = os.environ.get('SHIZUHA_PULSE_API_URL', 'http://shizuha-pulse:8002/api')
SHIZUHA_WIKI_API_URL = os.environ.get('SHIZUHA_WIKI_API_URL', 'http://shizuha-wiki:8013/api')
SHIZUHA_NOTES_API_URL = os.environ.get('SHIZUHA_NOTES_API_URL', 'http://shizuha-notes:8012/api')

# CON-131: Hive personal-agent discovery/delivery endpoints. The deployment env
# override must reach connect.services.hive_agents (it reads these via
# getattr(settings, ...), so the env var has no effect unless defined here).
HIVE_CONNECT_AGENTS_URL = os.environ.get(
    'HIVE_CONNECT_AGENTS_URL',
    'http://shizuha-hive.shizuha.svc.cluster.local:8000/api/v1/connect/agents/',
)
HIVE_CONNECT_DELIVERY_URL = os.environ.get(
    'HIVE_CONNECT_DELIVERY_URL',
    'http://hive-connect-delivery.shizuha-hive.svc.cluster.local:8031/api/v1/internal/connect/messages/',
)

# HIVE-613: HMAC shared secret for Connect→Hive delivery authentication.
# Both services must share the same secret. If unset, HMAC signing is skipped
# (the Hive side fails closed, so delivery will be rejected).
HIVE_CONNECT_HMAC_SECRET = os.environ.get('HIVE_CONNECT_HMAC_SECRET', '')

# CON-121 / VEN-53: zero-spend Connect Jobs -> DOJO CTA funnel.
# Static settings only: listing pages can render the CTA without DB joins or
# runtime calls. Dojo is now a standalone SPA (shizuha.com/dojo), free for all
# logged-in users — no waitlist gate.

# MinIO/S3 Storage for media uploads
AWS_ACCESS_KEY_ID = _required_env('MINIO_ACCESS_KEY')
AWS_SECRET_ACCESS_KEY = _required_env('MINIO_SECRET_KEY')
AWS_STORAGE_BUCKET_NAME = os.environ.get('MINIO_BUCKET', 'shizuha-connect')
AWS_S3_ENDPOINT_URL = _required_env('MINIO_ENDPOINT')
AWS_S3_REGION_NAME = 'us-east-1'
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = True
AWS_S3_SIGNATURE_VERSION = 's3v4'

# Server Port
SERVER_PORT = os.environ.get('SERVER_PORT', '8019')

# Feed Settings
FEED_CACHE_SIZE = int(os.environ.get('FEED_CACHE_SIZE', '500'))  # Posts per user
STORY_EXPIRY_HOURS = int(os.environ.get('STORY_EXPIRY_HOURS', '24'))
TRENDING_WINDOW_HOURS = int(os.environ.get('TRENDING_WINDOW_HOURS', '6'))

# LiveKit Settings (Video Conferencing)
LIVEKIT_API_KEY = os.environ.get('LIVEKIT_API_KEY', '')
LIVEKIT_API_SECRET = os.environ.get('LIVEKIT_API_SECRET', '')
LIVEKIT_URL = os.environ.get('LIVEKIT_URL', 'http://shizuha-livekit:7880')
LIVEKIT_EXTERNAL_URL = os.environ.get('LIVEKIT_EXTERNAL_URL', 'ws://localhost:7880')

# External bridge settings
CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY = os.environ.get('CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY', '')
CONNECT_BRIDGE_ADMIN_IDENTITY_FRESHNESS_HOURS = int(os.environ.get('CONNECT_BRIDGE_ADMIN_IDENTITY_FRESHNESS_HOURS', '24'))

# Content Limits
POST_MAX_LENGTH = int(os.environ.get('POST_MAX_LENGTH', '3000'))
BIO_MAX_LENGTH = int(os.environ.get('BIO_MAX_LENGTH', '500'))
COMMENT_MAX_LENGTH = int(os.environ.get('COMMENT_MAX_LENGTH', '1000'))
MAX_MEDIA_PER_POST = int(os.environ.get('MAX_MEDIA_PER_POST', '4'))

# Logging configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'connect': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'channels': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}


# ─────────── OAuth 2.0 Phase 2/4 — advisory claims + enforcement flag ───────
# Used by ShizuhaJWTAuthentication to log aud/iss mismatches and optionally
# reject tokens whose `aud` doesn't include this service. Defaults keep
# Phase 2 (advisory only) behavior; flip SHIZUHA_AUD_ENFORCE to True once
# advisory logs show clean traffic.
import os as _os
SHIZUHA_SERVICE_NAME = _os.environ.get('SHIZUHA_SERVICE_NAME', 'connect')
SHIZUHA_EXPECTED_ISSUER = _os.environ.get(
    'SHIZUHA_EXPECTED_ISSUER',
    'http://s1.tail.shizuha.com/id',
)
SHIZUHA_AUD_ENFORCE = _os.environ.get('SHIZUHA_AUD_ENFORCE', '').lower() in ('1', 'true', 'yes')

# CON-104: server-side content-storm dedup window for /api/messaging/dm/.
# An identical (sender, conversation, content) DM within this many seconds is
# suppressed (returns the existing message, no duplicate, no broadcast) to stop
# a looping sender from flooding a recipient. Set 0 to disable.
CONNECT_DM_DEDUP_WINDOW_SECONDS = int(os.environ.get('CONNECT_DM_DEDUP_WINDOW_SECONDS', '15'))

# CON-129/PLAT-1236: direct messaging is tenant-scoped. Participants must share
# at least one org membership; unknown org lookups fail closed by default.
# Same as CONNECT_ENFORCE_ORG_TENANCY: the shared-org read/visibility gate is ON
# in every real env, but defaults OFF under the test runner so read/enumeration
# tests (suggestions, group participants, people search) that can't reach a live
# admin membership lookup aren't filtered to empty. Env override still wins;
# the dedicated org tests re-enable it via override_settings.
CONNECT_REQUIRE_SHARED_ORG_FOR_DM = (
    (os.environ['CONNECT_REQUIRE_SHARED_ORG_FOR_DM'].lower() in ('1', 'true', 'yes'))
    if 'CONNECT_REQUIRE_SHARED_ORG_FOR_DM' in os.environ
    else (not _RUNNING_TESTS)
)
CONNECT_PRIVACY_ORG_CACHE_SECONDS = int(os.environ.get('CONNECT_PRIVACY_ORG_CACHE_SECONDS', '300'))

# PLAT-1237: event-driven SIEM-lite alerts for Connect runtime boundary anomalies.
CONNECT_SECURITY_ANOMALY_ALERTS_ENABLED = os.environ.get('CONNECT_SECURITY_ANOMALY_ALERTS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID = os.environ.get('CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID', '')
CONNECT_SECURITY_ALERT_RECIPIENT_USERNAME = os.environ.get('CONNECT_SECURITY_ALERT_RECIPIENT_USERNAME', 'akira')
CONNECT_SECURITY_ID_ORG_LOOKUP_ENABLED = os.environ.get('CONNECT_SECURITY_ID_ORG_LOOKUP_ENABLED', 'true').lower() in ('1', 'true', 'yes')
CONNECT_SECURITY_DM_FANOUT_WINDOW_SECONDS = int(os.environ.get('CONNECT_SECURITY_DM_FANOUT_WINDOW_SECONDS', '300'))
CONNECT_SECURITY_DM_FANOUT_THRESHOLD = int(os.environ.get('CONNECT_SECURITY_DM_FANOUT_THRESHOLD', '20'))
# PLAT-4133: dedicated HMAC origin for cross-service boundary-denial events.
# Deliberately separate from CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET so Wiki/Admin/ID
# producers cannot acquire the user-visible system-message bypass.
CONNECT_SECURITY_EVENT_ORIGIN_SECRET = os.environ.get('CONNECT_SECURITY_EVENT_ORIGIN_SECRET', '')
CONNECT_SECURITY_EVENT_TOKEN_MAX_AGE_SECONDS = int(os.environ.get('CONNECT_SECURITY_EVENT_TOKEN_MAX_AGE_SECONDS', '300'))
CONNECT_SECURITY_EVENT_IDEMPOTENCY_SECONDS = int(os.environ.get('CONNECT_SECURITY_EVENT_IDEMPOTENCY_SECONDS', '600'))
CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS = int(os.environ.get('CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS', '300'))
CONNECT_SECURITY_DEDUPE_REDIS_URL = os.environ.get('CONNECT_SECURITY_DEDUPE_REDIS_URL', CELERY_BROKER_URL)
# CON-217 durable outbox relay/worker bounds. Postgres remains authoritative;
# these values only control wake-up cadence, leases and retry pressure.
CONNECT_SECURITY_OUTBOX_RELAY_BATCH_SIZE = int(os.environ.get('CONNECT_SECURITY_OUTBOX_RELAY_BATCH_SIZE', '50'))
CONNECT_SECURITY_OUTBOX_RELAY_LEASE_SECONDS = int(os.environ.get('CONNECT_SECURITY_OUTBOX_RELAY_LEASE_SECONDS', '45'))
CONNECT_SECURITY_OUTBOX_WORKER_LEASE_SECONDS = int(os.environ.get('CONNECT_SECURITY_OUTBOX_WORKER_LEASE_SECONDS', '60'))
CONNECT_SECURITY_OUTBOX_BACKOFF_BASE_SECONDS = int(os.environ.get('CONNECT_SECURITY_OUTBOX_BACKOFF_BASE_SECONDS', '5'))
CONNECT_SECURITY_OUTBOX_BACKOFF_MAX_SECONDS = int(os.environ.get('CONNECT_SECURITY_OUTBOX_BACKOFF_MAX_SECONDS', '300'))
CONNECT_SECURITY_OUTBOX_MAX_DELIVERY_ATTEMPTS = int(os.environ.get('CONNECT_SECURITY_OUTBOX_MAX_DELIVERY_ATTEMPTS', '12'))
# PLAT-4138: security-alert evaluation/delivery runs in Celery workers while
# Prometheus scrapes web pods. Persist the bounded stage counters in shared Redis
# and render them from web `/metrics`; process-local worker counters are not a
# valid fail-loud signal.
CONNECT_SECURITY_METRICS_REDIS_URL = os.environ.get(
    'CONNECT_SECURITY_METRICS_REDIS_URL', CELERY_BROKER_URL,
)
# A worker Redis-write failure is recorded independently in Postgres and kept
# alertable for this window even if the next web scrape can read Redis again.
CONNECT_SECURITY_METRICS_WRITE_FAILURE_RECENT_SECONDS = int(os.environ.get(
    'CONNECT_SECURITY_METRICS_WRITE_FAILURE_RECENT_SECONDS', '600',
))
CONNECT_SECURITY_EVENT_ALLOWLIST = {
    service.strip()
    for service in os.environ.get(
        'CONNECT_SECURITY_EVENT_ALLOWLIST',
        'shizuha-wiki,shizuha-admin,shizuha-id',
    ).split(',')
    if service.strip()
}

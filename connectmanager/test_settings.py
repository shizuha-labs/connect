"""Test settings — SQLite in-memory DB so unit tests don't need Postgres."""
from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Avoid Redis dependency in unit tests.
CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}

# Unit tests must not require the production Redis/Celery stack.
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
CELERY_TASK_IGNORE_RESULT = True

# The production migration chain contains PostgreSQL-specific DDL (sequences/
# triggers/recovery SQL) that SQLite cannot execute. Unit tests using this
# in-memory settings module create Connect tables from the current model state.
MIGRATION_MODULES = {
    'connect': None,
}

# CON-133: the unit suite has no admin service to answer membership lookups,
# so the org-tenancy DM gate is off by default here; the tenancy test classes
# switch it on explicitly via override_settings.
CONNECT_REQUIRE_SHARED_ORG_FOR_DM = False

# CON-129: the org-tenancy gate needs the admin service, which the unit suite
# does not run. Enforcement is exercised explicitly by
# connect/tests/test_con129_org_tenancy.py via override_settings.
CONNECT_ENFORCE_ORG_TENANCY = False

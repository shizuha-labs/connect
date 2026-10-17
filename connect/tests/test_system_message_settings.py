import importlib
import os
from unittest import mock

from django.test import SimpleTestCase


class SystemMessageSettingsEnvTests(SimpleTestCase):
    @staticmethod
    def _database_conn_max_age_with_env(env):
        """Read the production setting in isolation, then restore the module."""
        import connectmanager.settings as settings_module

        configured = {'POSTGRES_PASSWORD': 'fixture-database-password', 'SECRET_KEY': 'fixture-django', 'MINIO_ACCESS_KEY': 'fixture-storage-user',
                      'MINIO_SECRET_KEY': 'fixture-storage-password',
                      'MINIO_ENDPOINT': 'http://storage.example.invalid:8333', **env}
        try:
            with mock.patch.dict(os.environ, configured, clear=True):
                reloaded = importlib.reload(settings_module)
                return reloaded.DATABASES['default']['CONN_MAX_AGE']
        finally:
            importlib.reload(settings_module)

    def test_asgi_database_connections_are_non_persistent_by_default(self):
        self.assertEqual(self._database_conn_max_age_with_env({}), 0)

    def test_asgi_database_connection_age_accepts_explicit_override(self):
        self.assertEqual(
            self._database_conn_max_age_with_env({
                'CONNECT_DB_CONN_MAX_AGE_SECONDS': '45',
            }),
            45,
        )

    def test_settings_read_system_message_origin_env(self):
        with mock.patch.dict(os.environ, {
            'CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET': 'shared-secret',
            'CONNECT_SYSTEM_MESSAGE_ALLOWLIST': 'shizuha-pulse, shizuha-connect',
            'CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS': '123',
        }, clear=False):
            import connectmanager.settings as settings_module
            reloaded = importlib.reload(settings_module)

        self.assertEqual(reloaded.CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET, 'shared-secret')
        self.assertEqual(reloaded.CONNECT_SYSTEM_MESSAGE_ALLOWLIST, {'shizuha-pulse', 'shizuha-connect'})
        self.assertEqual(reloaded.CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS, 123)

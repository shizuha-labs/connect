"""Exercise the actual settings import with independent installation environments."""
import json
import os
import subprocess
import sys

from django.test import SimpleTestCase


class SelfHostSettingsTests(SimpleTestCase):
    keys = ('ALLOWED_HOSTS', 'FORCE_SCRIPT_NAME', 'CSRF_TRUSTED_ORIGINS',
            'CORS_ALLOW_ALL_ORIGINS', 'CORS_ALLOWED_ORIGINS')

    def imported(self, **overrides):
        env = {key: value for key, value in os.environ.items() if key not in self.keys}
        env.update(overrides)
        program = ('import json; import connectmanager.settings as s; '
                   'print(json.dumps({k:getattr(s,k) for k in ' + repr(self.keys) + '}))')
        result = subprocess.run([sys.executable, '-c', program], env=env,
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_custom_prefix_and_strict_origins(self):
        actual = self.imported(ALLOWED_HOSTS=' platform.example, localhost, ',
                               FORCE_SCRIPT_NAME='/connect',
                               CSRF_TRUSTED_ORIGINS='https://platform.example',
                               CORS_ALLOW_ALL_ORIGINS='false',
                               CORS_ALLOWED_ORIGINS='https://platform.example, ')
        self.assertEqual(actual, {
            'ALLOWED_HOSTS': ['platform.example', 'localhost'],
            'FORCE_SCRIPT_NAME': '/connect',
            'CSRF_TRUSTED_ORIGINS': ['https://platform.example'],
            'CORS_ALLOW_ALL_ORIGINS': False,
            'CORS_ALLOWED_ORIGINS': ['https://platform.example'],
        })

    def test_existing_deployment_defaults_are_preserved(self):
        actual = self.imported()
        self.assertEqual(actual['FORCE_SCRIPT_NAME'], '/shizuha-connect')
        self.assertIn('*', actual['ALLOWED_HOSTS'])
        self.assertIn('https://chat.shizuha.com', actual['CSRF_TRUSTED_ORIGINS'])
        self.assertTrue(actual['CORS_ALLOW_ALL_ORIGINS'])

    def test_root_install_and_empty_origin_lists(self):
        actual = self.imported(FORCE_SCRIPT_NAME='', CSRF_TRUSTED_ORIGINS='',
                               CORS_ALLOW_ALL_ORIGINS='0', CORS_ALLOWED_ORIGINS='')
        self.assertEqual(actual['FORCE_SCRIPT_NAME'], '')
        self.assertEqual(actual['CSRF_TRUSTED_ORIGINS'], [])
        self.assertFalse(actual['CORS_ALLOW_ALL_ORIGINS'])
        self.assertEqual(actual['CORS_ALLOWED_ORIGINS'], [])


class RequiredCredentialSettingsTests(SimpleTestCase):
    def test_missing_and_empty_credentials_fail_closed(self):
        for missing in ('SECRET_KEY', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'MINIO_ENDPOINT'):
            for value in (None, ''):
                with self.subTest(setting=missing, value=value):
                    env = dict(os.environ, SECRET_KEY='fixture-django',
                               MINIO_ACCESS_KEY='fixture-storage-user',
                               MINIO_SECRET_KEY='fixture-storage-password',
                               MINIO_ENDPOINT='http://storage.example.invalid:8333')
                    if value is None:
                        env.pop(missing)
                    else:
                        env[missing] = value
                    result = subprocess.run([sys.executable, '-c', 'import connectmanager.settings'],
                                            env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(missing + ' must be configured', result.stderr)


    def test_explicit_storage_endpoint_is_preserved(self):
        env = dict(os.environ, SECRET_KEY='fixture-django', MINIO_ACCESS_KEY='fixture-storage-user',
                   MINIO_SECRET_KEY='fixture-storage-password', MINIO_ENDPOINT='http://storage.example.invalid:8333')
        result = subprocess.run([sys.executable, '-c',
                                 'import connectmanager.settings as s; print(s.AWS_S3_ENDPOINT_URL)'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), env['MINIO_ENDPOINT'])

    def test_database_password_is_explicit_and_never_a_source_default(self):
        for value in (None, ''):
            with self.subTest(configured=value is not None):
                env = dict(os.environ, SECRET_KEY='fixture-django',
                           MINIO_ACCESS_KEY='fixture-storage-user',
                           MINIO_SECRET_KEY='fixture-storage-password',
                           MINIO_ENDPOINT='http://storage.example.invalid:8333')
                if value is None:
                    env.pop('POSTGRES_PASSWORD',None)
                else:
                    env['POSTGRES_PASSWORD']=value
                result=subprocess.run([sys.executable,'-c','import connectmanager.settings'],
                                      env=env,text=True,capture_output=True)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('POSTGRES_PASSWORD must be configured',result.stderr)
        env=dict(os.environ,SECRET_KEY='fixture-django',MINIO_ACCESS_KEY='fixture-storage-user',
                 MINIO_SECRET_KEY='fixture-storage-password',MINIO_ENDPOINT='http://storage.example.invalid:8333',
                 POSTGRES_PASSWORD='fixture-database-password')
        program='import os; import connectmanager.settings as s; assert s.DATABASES["default"]["PASSWORD"] == os.environ["POSTGRES_PASSWORD"]'
        result=subprocess.run([sys.executable,'-c',program],env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

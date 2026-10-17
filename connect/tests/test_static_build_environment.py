"""Run each image's real static-collection command without runtime credentials."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

from django.test import SimpleTestCase


class StaticBuildEnvironmentTests(SimpleTestCase):
    def test_image_static_collection_uses_only_inline_build_credentials(self):
        root = Path(__file__).resolve().parents[2]
        for filename in ('Dockerfile', 'Dockerfile.con79'):
            with self.subTest(image=filename):
                lines = [line.removeprefix('RUN ') for line in (root / filename).read_text().splitlines()
                         if line.startswith('RUN ') and 'manage.py collectstatic' in line]
                self.assertEqual(len(lines), 1)
                tokens = shlex.split(lines[0])
                env = {name: os.environ[name] for name in ('PATH', 'HOME', 'LANG') if name in os.environ}
                while tokens and '=' in tokens[0]:
                    name, value = tokens.pop(0).split('=', 1)
                    env[name] = value
                self.assertEqual(tokens[:3], ['python', 'manage.py', 'collectstatic'])
                command = [sys.executable, *tokens[1:], '--dry-run']
                result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                for missing in ('POSTGRES_PASSWORD', 'MINIO_ENDPOINT'):
                    with self.subTest(missing=missing):
                        incomplete = {key: value for key, value in env.items() if key != missing}
                        result = subprocess.run(command, cwd=root, env=incomplete, capture_output=True,
                                                text=True, timeout=60)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(missing + ' must be configured', result.stderr)

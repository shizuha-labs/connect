"""Render the actual workflow heredoc without executing comment text."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class BuildJobRenderingTests(unittest.TestCase):
    def test_comments_do_not_execute_uv_on_dispatcher(self):
        source = (Path(__file__).parents[1] / 'workflows/deploy.yml').read_text()
        start = source.index('          render_build_job()')
        end = source.index('          # Launch both arch builds', start)
        script = '\n'.join(line[10:] for line in source[start:end].splitlines())
        script = script.replace('${{ github.sha }}', 'a' * 40)
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'uv'
            stub.write_text('#!/bin/sh\necho UNEXPECTED_DISPATCHER_EXEC >&2\nexit 97\n')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=directory+os.pathsep+os.environ['PATH'],
                       SHORT='fixture', TAG='connect-fixture', REG='registry.example.test',
                       NPM_CONFIG_REGISTRY='https://registry.npmjs.org',
                       PIP_INDEX_URL='https://pypi.org/simple', PIP_TRUSTED_HOST='pypi.org')
            result = subprocess.run(['bash'], input='set -euo pipefail\n'+script+'\nrender_build_job amd64\n',
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertIn('name: ci-build-connect-fixture-amd64', result.stdout)
            self.assertIn('re-ran apt-get + uv sync', result.stdout)
            self.assertIn('${GIT_PASSWORD}', result.stdout)


if __name__ == '__main__': unittest.main()

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CiJobCleanupPolicyTests(unittest.TestCase):
    def test_local_gate_mounts_current_workflows(self):
        gate = (ROOT / ".prepush-test.sh").read_text()
        self.assertIn('-v "$PWD/.forgejo:/app/.forgejo:ro', gate)
        self.assertIn("manage.py makemigrations --check --dry-run", gate)
        self.assertIn("check-django-migration-graph.py", gate)
        self.assertIn("manage.py test --noinput", gate)

    def test_required_premerge_and_local_gate_cover_frontend(self):
        premerge = (ROOT / ".forgejo/workflows/premerge.yml").read_text()
        local = (ROOT / ".prepush-test.sh").read_text()
        package = (ROOT / "frontend/package.json").read_text()
        for command in ("npm ci", "npm run lint", "npm test", "npm run build"):
            with self.subTest(command=command):
                self.assertIn(command, premerge)
                self.assertIn(command, local)
        self.assertIn('"test": "node --test', package)

    def test_active_workflow_jobs_expire_and_builds_clean_up(self):
        for name in ("deploy.yml", "deploy-frontend.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".forgejo/workflows" / name).read_text()
                ttls = [
                    int(value)
                    for value in re.findall(
                        r"ttlSecondsAfterFinished:\s*(\d+)", workflow
                    )
                ]
                self.assertEqual(workflow.count("kind: Job"), len(ttls))
                self.assertTrue(ttls)
                self.assertTrue(all(ttl <= 300 for ttl in ttls))
                self.assertEqual(
                    workflow.count("- --cleanup"),
                    workflow.count(
                        "image: gcr.io/kaniko-project/executor:v1.23.2"
                    ),
                )
                if name == "deploy.yml":
                    self.assertIn('"$BUILD_GUARD" wait-pair', workflow)
                    self.assertIn('--job "amd64=${AMD64_JOB}"', workflow)
                    self.assertIn('--job "arm64=${ARM64_JOB}"', workflow)
                    self.assertNotIn("wait_job()", workflow)
                else:
                    self.assertIn("wait_job amd64 &", workflow)
                    self.assertIn("wait_job arm64 &", workflow)
                    self.assertNotRegex(workflow, r"(?m)^\s*wait_job (?:amd64|arm64)$")

    def test_backend_and_frontend_share_one_non_cancelling_release_lock(self):
        group = "group: deploy-connect-release-${{ github.ref }}"
        for name in ("deploy.yml", "deploy-frontend.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".forgejo/workflows" / name).read_text()
                self.assertEqual(1, workflow.count(group))
                lock = re.search(
                    re.escape(group) + r"\s+cancel-in-progress:\s*(\w+)",
                    workflow,
                )
                self.assertIsNotNone(lock)
                self.assertEqual("false", lock.group(1))

    def test_backend_only_changes_do_not_trigger_a_frontend_release(self):
        workflow = (
            ROOT / ".forgejo/workflows/deploy-frontend.yml"
        ).read_text()
        trigger = workflow.split("concurrency:", 1)[0]
        for path in (
            "'.forgejo/workflows/deploy-frontend.yml'",
            "'.forgejo/scripts/**'",
            "'.dockerignore'",
            "'frontend/**'",
        ):
            self.assertIn(path, trigger)
        self.assertNotIn("'connect/**'", trigger)
        self.assertNotIn("'connectmanager/**'", trigger)

    def test_backend_release_reconciles_chart_values_without_rolling_back_frontend(self):
        workflow = (ROOT / ".forgejo/workflows/deploy.yml").read_text()
        release = workflow.split(
            "helm upgrade shizuha-connect", 1
        )[1].split("--wait --timeout=10m", 1)[0]

        self.assertIn("--reset-values", release)
        self.assertNotIn("--reuse-values", release)
        self.assertIn(
            'HELM_FE_ARGS=(--set "frontend.image.tag=${LIVE_FE_TAG}")',
            workflow,
        )


if __name__ == "__main__":
    unittest.main()

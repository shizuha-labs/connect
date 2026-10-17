import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).parents[1] / 'recovery/recover_cancelled_release.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.history = [{'revision': 183, 'status': 'deployed'}, {'revision': 184, 'status': 'pending-upgrade'}]
        self.owner = {'id': recovery.OWNER, 'commit_sha': recovery.OWNER_SHA, 'workflow_id': 'deploy.yml', 'status': 'cancelled', 'stopped': '2026-10-03T03:45:27Z'}
        self.calls = []
        self.bad_tag = False
        self.changed = False

    def helm(self, *args):
        self.calls.append(args)
        if args[0] == 'history':
            if self.changed and len([c for c in self.calls if c[0] == 'history']) > 1:
                return json.dumps([{'revision': 185, 'status': 'pending-upgrade'}])
            return json.dumps(self.history)
        if args[:2] == ('get', 'values'):
            tag = recovery.PREVIOUS_TAG if args[4] == '183' else recovery.PENDING_TAG
            if self.bad_tag: tag = 'unexpected'
            return json.dumps({'image': {'tag': tag}, 'worker': {'image': {'tag': tag}}, 'beat': {'image': {'tag': tag}}})
        if args[0] == 'rollback': return ''
        if args[0] == 'status': return json.dumps({'version': 185, 'info': {'status': 'deployed'}})
        raise AssertionError(args)

    def test_cancelled_owner_recovers_once_then_normal_next_release(self):
        self.assertTrue(recovery.recover(lambda: self.owner, self.helm))
        self.assertIn(('rollback', recovery.RELEASE, '183', '--no-hooks', '--wait', '--timeout', '10m'), self.calls)
        self.history.append({'revision': 185, 'status': 'deployed'})
        self.assertFalse(recovery.recover(lambda: self.owner, self.helm))
        self.assertEqual(sum(c[0] == 'rollback' for c in self.calls), 1)

    def test_active_owner_cannot_rollback(self):
        self.owner['status'] = 'running'
        self.refuses()

    def test_unknown_pending_revision_cannot_rollback(self):
        self.history[-1]['revision'] = 185
        self.refuses()

    def test_mismatched_owner_sha_cannot_rollback(self):
        self.owner['commit_sha'] = 'bad'
        self.refuses()

    def test_unverified_previous_image_cannot_rollback(self):
        self.bad_tag = True
        self.refuses()

    def test_concurrent_metadata_change_cannot_rollback(self):
        self.changed = True
        self.refuses()

    def test_independent_frontend_is_snapshotted_before_recovery(self):
        source = (Path(__file__).parents[1] / 'workflows/deploy.yml').read_text()
        self.assertLess(source.index('LIVE_FE_TAG="$('), source.index('python3 .forgejo/recovery/recover_cancelled_release.py'))
        self.assertEqual(source.count('LIVE_FE_TAG="$('), 1)

    def refuses(self):
        with self.assertRaises(RuntimeError): recovery.recover(lambda: self.owner, self.helm)
        self.assertFalse(any(c[0] == 'rollback' for c in self.calls))


if __name__ == '__main__': unittest.main()

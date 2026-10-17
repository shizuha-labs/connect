"""Recover the pinned cancelled Connect release while holding the CI release lock.

No general unlock: unknown pending revisions and active/unknown owners fail closed.
Run only from deploy.yml's serialized release job, never from an operator shell.
"""
import json
import os
import subprocess
import urllib.request

NAMESPACE = 'shizuha'
RELEASE = 'shizuha-connect'
OWNER = 54693
OWNER_SHA = '9164d61473a427ad5513fa38da5876858c996aa0'
PENDING = 184
PREVIOUS = 183
PREVIOUS_TAG = 'connect-c9a8537'
PENDING_TAG = 'connect-9164d61'


def helm(*args):
    return subprocess.check_output(['helm', *args, '-n', NAMESPACE], text=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def image_tags(values):
    return [values['image']['tag'], values['worker']['image']['tag'], values['beat']['image']['tag']]


def recover(read_owner, run_helm=helm):
    history = json.loads(run_helm('history', RELEASE, '-o', 'json'))
    latest = max(history, key=lambda item: int(item['revision']))
    if not latest['status'].startswith('pending-'):
        print('No pending Helm operation; normal deployment may proceed.')
        return False
    require(int(latest['revision']) == PENDING and latest['status'] == 'pending-upgrade', 'Unknown pending release; refusing recovery')
    previous = next(item for item in history if int(item['revision']) == PREVIOUS)
    require(previous['status'] == 'deployed', 'Pinned rollback revision is not deployed')
    owner = read_owner()
    require(owner['id'] == OWNER and owner['commit_sha'] == OWNER_SHA, 'Unexpected owning workflow identity')
    require(owner['workflow_id'] == 'deploy.yml' and owner['status'] == 'cancelled' and owner['stopped'], 'Owning workflow is not confirmed stopped')
    require(image_tags(json.loads(run_helm('get', 'values', RELEASE, '--revision', str(PREVIOUS), '-o', 'json'))) == [PREVIOUS_TAG] * 3, 'Rollback image mismatch')
    require(image_tags(json.loads(run_helm('get', 'values', RELEASE, '--revision', str(PENDING), '-o', 'json'))) == [PENDING_TAG] * 3, 'Pending image mismatch')
    # Re-read metadata immediately before the mutation while CI holds the shared
    # backend/frontend release lock. No hooks means no reverse data migration.
    current = max(json.loads(run_helm('history', RELEASE, '-o', 'json')), key=lambda item: int(item['revision']))
    require(current == latest, 'Release changed during recovery checks')
    print('Recovering cancelled run 54693: revision 184 -> known-good revision 183; retained data is unchanged.')
    run_helm('rollback', RELEASE, str(PREVIOUS), '--no-hooks', '--wait', '--timeout', '10m')
    recovered = json.loads(run_helm('status', RELEASE, '-o', 'json'))
    require(recovered['info']['status'] == 'deployed' and int(recovered['version']) > PENDING, 'Rollback did not converge')
    print('Rollback verified; continuing normal deployment of the tested current source.')
    return True


def read_owner():
    token = os.environ['CONNECT_RECOVERY_ORIGIN_TOKEN']
    request = urllib.request.Request(
        f'http://forgejo-http.origin.svc.cluster.local/api/v1/repos/shizuha-labs/connect/actions/runs/{OWNER}',
        headers={'Authorization': 'token ' + token, 'Host': 'origin.shizuha.com'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


if __name__ == '__main__':
    recover(read_owner)

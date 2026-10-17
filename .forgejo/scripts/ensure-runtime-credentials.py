#!/usr/bin/env python3
"""Create-once projection of effective live settings; never print credential bytes."""
import base64
import json
import subprocess
import sys

NAMESPACE = 'shizuha'
SECRET = 'connect-runtime-credentials'
DEPLOYMENTS = ('shizuha-connect', 'shizuha-connect-worker', 'shizuha-connect-beat')
KEYS = {'SECRET_KEY': 'django-secret', 'MINIO_ACCESS_KEY': 'minio-access-key',
        'MINIO_SECRET_KEY': 'minio-secret-key'}
READ_SETTINGS = "import os,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','connectmanager.settings'); from django.conf import settings; print(json.dumps({'django-secret':settings.SECRET_KEY,'minio-access-key':settings.AWS_ACCESS_KEY_ID,'minio-secret-key':settings.AWS_SECRET_ACCESS_KEY}))"


class BootstrapError(RuntimeError):
    pass


def kubectl(*args, data=None):
    try:
        result = subprocess.run(['kubectl', '-n', NAMESPACE, *args], input=data,
                                text=True, capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError('Connect credential bootstrap request failed') from None
    if result.returncode:
        raise BootstrapError('Connect credential bootstrap request rejected')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise BootstrapError('Connect credential bootstrap response invalid') from None


def deployments(call):
    return [call('get', 'deployment', name, '-o', 'json') for name in DEPLOYMENTS]


def configured(source):
    containers = source['spec']['template']['spec']['containers']
    if len(containers) != 1:
        raise BootstrapError('Unexpected Connect container topology')
    entries = {e['name']: e for e in containers[0].get('env', [])}
    return all(entries.get(env, {}).get('valueFrom', {}).get('secretKeyRef', {}) ==
               {'name': SECRET, 'key': key} for env, key in KEYS.items())


def identity(source):
    meta = source['metadata']
    return (meta['uid'], meta['resourceVersion'])


def live_pods(call, sources):
    result = []
    for source in sources:
        selector = ','.join(f'{k}={v}' for k, v in sorted(source['spec']['selector']['matchLabels'].items()))
        pods = call('get', 'pods', '-l', selector, '-o', 'json')['items']
        active = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        desired = source['spec']['replicas']
        if desired < 1 or len(active) != desired:
            raise BootstrapError('Connect rollout is not stable')
        expected_image = source['spec']['template']['spec']['containers'][0]['image']
        for pod in active:
            statuses = pod.get('status', {}).get('containerStatuses', [])
            if (pod.get('status', {}).get('phase') != 'Running' or len(statuses) != 1
                    or not statuses[0].get('ready')
                    or pod['spec']['containers'][0]['image'] != expected_image):
                raise BootstrapError('Connect source pod is not ready at the expected revision')
            result.append(pod)
    return sorted(result, key=lambda p: p['metadata']['name'])


def pod_identity(pods):
    return [(p['metadata']['name'], p['metadata']['uid'],
             p['status']['containerStatuses'][0].get('containerID'),
             p['status']['containerStatuses'][0].get('restartCount')) for p in pods]


def read_values(call, pods):
    values = []
    for pod in pods:
        value = call('exec', pod['metadata']['name'], '-c', pod['spec']['containers'][0]['name'],
                     '--', '/app/.venv/bin/python', '-c', READ_SETTINGS)
        if set(value) != set(KEYS.values()) or any(not isinstance(v, str) or not v for v in value.values()):
            raise BootstrapError('Connect live credential configuration is incomplete')
        values.append({k: base64.b64encode(v.encode()).decode() for k, v in value.items()})
    if not values or any(value != values[0] for value in values):
        raise BootstrapError('Connect runtime roles disagree on credentials; refusing projection')
    return values[0]


def ensure(call=kubectl):
    target = call('get', 'secret', SECRET, '-o', 'json')
    sources = deployments(call)
    existing = target.get('data', {})
    if all(configured(s) for s in sources):
        if target.get('immutable') is not True or set(existing) != set(KEYS.values()) or not all(existing.values()):
            raise BootstrapError('Connect configured Secret is incomplete or mutable')
        return 'immutable configured Secret verified'
    pods = live_pods(call, sources)
    expected = read_values(call, pods)
    if existing and (existing != expected or target.get('immutable') is not True):
        raise BootstrapError('Connect target differs from live values; refusing overwrite')
    current_sources = deployments(call)
    current_pods = live_pods(call, current_sources)
    if ([identity(s) for s in current_sources] != [identity(s) for s in sources]
            or pod_identity(current_pods) != pod_identity(pods)
            or read_values(call, current_pods) != expected):
        raise BootstrapError('Connect source changed during verification')
    if not existing:
        if target.get('immutable'):
            raise BootstrapError('Connect target is empty and immutable')
        patch = [
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': target['metadata']['resourceVersion']},
            {'op': 'add', 'path': '/data', 'value': expected},
            {'op': 'add', 'path': '/immutable', 'value': True},
        ]
        result = call('patch', 'secret', SECRET, '--type=json', '--patch-file=/dev/stdin',
                      '-o', 'json', data=json.dumps(patch))
        if result.get('data') != expected or result.get('immutable') is not True:
            raise BootstrapError('Connect projection result verification failed')
    verified = call('get', 'secret', SECRET, '-o', 'json')
    final_sources = deployments(call)
    if (verified.get('data') != expected or verified.get('immutable') is not True
            or [identity(s) for s in final_sources] != [identity(s) for s in sources]
            or pod_identity(live_pods(call, final_sources)) != pod_identity(pods)):
        raise BootstrapError('Connect projection source or target changed; release must stop')
    return 'all runtime roles verified; credentials projected unchanged and immutable'


if __name__ == '__main__':
    try:
        print('Connect credentials: ' + ensure())
    except (BootstrapError, KeyError, TypeError):
        print('Connect credential bootstrap failed closed; inspect deployment state without logging values', file=sys.stderr)
        raise SystemExit(1)

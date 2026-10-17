from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from connect.authentication import JWTAuthMiddleware, _Rs256Token
from connect.models import PresenceSession


class WebSocketJWTAuthMiddlewareTest(SimpleTestCase):
    def test_accepts_rs256_tokens_via_shared_shizuha_authenticator(self):
        middleware = JWTAuthMiddleware(lambda scope, receive, send: None)
        scope = {
            'query_string': b'token=rs256-agent-token',
            'headers': [],
        }
        token = _Rs256Token({
            'user_id': '21',
            'username': 'hana',
            'email': 'hana@shizuha.com',
            'is_agent': True,
            'enabled_services': ['connect', 'pulse'],
        })

        # get_validated_token now parses the JWT header (alg allowlist) BEFORE
        # _try_rs256_validate, so a non-JWT placeholder string can't reach the
        # inner validator — mock the whole validation step (still exercises the
        # middleware flow + get_user), which is what this test asserts.
        with patch(
            'connect.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value=token,
        ):
            user, validated_token = async_to_sync(middleware._authenticate)(scope)

        self.assertTrue(user.is_authenticated)
        self.assertEqual(user.raw_user_id, '21')
        self.assertEqual(user.username, 'hana')
        self.assertEqual(user.enabled_services, ['connect', 'pulse'])
        self.assertIs(validated_token, token)


class _AuthRejectLayer:
    def __init__(self):
        self.sent = []

    async def send(self, channel, event):
        self.sent.append((channel, event))


class InternalAgentAuthRejectTriggerTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _post(self, payload, service='shizuha-agent'):
        return self.client.post(
            '/api/internal/agents/auth-reject-trigger/',
            payload,
            format='json',
            HTTP_X_INTERNAL_SERVICE=service,
        )

    def test_rejects_non_internal_callers(self):
        resp = self.client.post('/api/internal/agents/auth-reject-trigger/', {}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_sends_auth_class_close_to_only_selected_live_agent_sessions(self):
        now = timezone.now()
        selected = PresenceSession.objects.create(
            user_id=132,
            session_key='userchat:specific.selected!1',
            client_id='specific.selected!1',
            client_type='agent',
            is_agent=True,
            status='online',
            channel_name='specific.selected!1',
            last_heartbeat_at=now,
        )
        PresenceSession.objects.create(
            user_id=132,
            session_key='userchat:specific.offline!1',
            client_id='specific.offline!1',
            client_type='agent',
            is_agent=True,
            status='offline',
            channel_name='specific.offline!1',
            last_heartbeat_at=now,
        )
        PresenceSession.objects.create(
            user_id=999,
            session_key='userchat:specific.other!1',
            client_id='specific.other!1',
            client_type='agent',
            is_agent=True,
            status='online',
            channel_name='specific.other!1',
            last_heartbeat_at=now,
        )
        layer = _AuthRejectLayer()

        with patch('connect.views.internal_api.get_channel_layer', return_value=layer):
            resp = self._post({
                'user_id': 132,
                'username': 'probe-qa',
                'code': 4401,
                'reason': 'PLAT-3988 stale-token live verification',
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['triggered'], 1)
        self.assertEqual(layer.sent, [(
            'specific.selected!1',
            {
                'type': 'force_auth_reject',
                'code': 4401,
                'reason': 'PLAT-3988 stale-token live verification',
                'trigger': 'internal-agent-auth-reject-test',
                'target_user_id': 132,
                'target_session_id': str(selected.id),
            },
        )])

    def test_requires_auth_class_code_and_reason(self):
        now = timezone.now()
        PresenceSession.objects.create(
            user_id=132,
            session_key='userchat:specific.selected!1',
            client_id='specific.selected!1',
            client_type='agent',
            is_agent=True,
            status='online',
            channel_name='specific.selected!1',
            last_heartbeat_at=now,
        )

        resp = self._post({'user_id': 132, 'username': 'probe-qa', 'code': 1000, 'reason': 'bad code'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'code must be one of 4401, 4403, 1008')

        resp = self._post({'user_id': 132, 'username': 'probe-qa', 'code': 4401, 'reason': 'short'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'reason must describe the verification purpose')

    def test_refuses_production_fleet_agent_targets(self):
        resp = self._post({
            'user_id': 12,
            'username': 'ni',
            'code': 4401,
            'reason': 'PLAT-3988 stale-token live verification',
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn('production fleet', resp.data['error'])

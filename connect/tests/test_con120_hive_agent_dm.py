from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from connect.models import ConversationParticipant, DirectMessage
from connect.services.hive_agents import hive_agent_user_id


HERMES_STABLE_ID = f'hive:hermes:{uuid4()}'
HERMES_ID = HERMES_STABLE_ID.rsplit(':', 1)[-1]
HERMES_AGENT = {
    'stable_id': HERMES_STABLE_ID,
    'hive_claw_id': HERMES_ID,
    'display_name': 'Hermes',
    'connect_username': 'hive-hermes-demo',
    'agent_type': 'hermes',
    'runtime_kind': 'hermes',
    'effective_model': 'DeepSeek-V4-Flash',
    'model_provider': 'Cortex',
    'runtime_backend': 'cortex',
    'status': 'running',
}


CLAW_STABLE_ID = f'hive:claw:{uuid4()}'
CLAW_ID = CLAW_STABLE_ID.rsplit(':', 1)[-1]
CLAW_AGENT = {
    'stable_id': CLAW_STABLE_ID,
    'hive_claw_id': CLAW_ID,
    'display_name': 'Claw',
    'connect_username': 'hive-claw-demo',
    'agent_type': 'claw',
    'runtime_kind': 'openclaw',
    'effective_model': 'DeepSeek-V4-Flash',
    'model_provider': 'Cortex',
    'runtime_backend': 'cortex',
    'status': 'running',
}


class ResponseStub:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.ok = status_code < 400
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f'status={self.status_code}')


def id_404_then_hive_agents(*args, **kwargs):
    url = args[0]
    if '/internal/users/by-username/' in url or '/internal/users/by-email/' in url:
        return ResponseStub(404, {'error': 'not found'})
    if '/api/v1/connect/agents/' in url:
        return ResponseStub(200, {'count': 1, 'results': [HERMES_AGENT]})
    return ResponseStub(500, {'error': 'unexpected url'})


@override_settings(CONNECT_DM_DEDUP_WINDOW_SECONDS=0)
class Con120HiveAgentDirectMessageTests(TestCase):
    def setUp(self):
        self.profile_delay_patcher = mock.patch('connect.tasks.ensure_participant_social_profile.delay')
        self.profile_delay_patcher.start()
        self.addCleanup(self.profile_delay_patcher.stop)
        self.client = APIClient()
        self.user = User(id=101, username='harshita', email='harshita@example.com', first_name='Harshita')
        self.client.force_authenticate(user=self.user, token={'is_agent': False})

    @mock.patch('connect.views.messaging.dispatch_hive_agent_message')
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('connect.services.hive_agents.requests.get', side_effect=id_404_then_hive_agents)
    def test_message_user_falls_back_to_visible_hive_hermes_alias(self, _get, _broadcast, dispatch):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_username': 'hermes', 'content': 'hello hermes'},
                format='json',
                HTTP_AUTHORIZATION='Bearer harshita-token',
            )

        self.assertEqual(response.status_code, 201)
        recipient = response.data['recipient']
        self.assertEqual(recipient['user_id'], hive_agent_user_id(HERMES_STABLE_ID))
        self.assertEqual(recipient['email'], 'hive-hermes-demo@hive-agent.local')
        self.assertIn('Hermes', recipient['name'])

        participants = ConversationParticipant.objects.filter(conversation_id=response.data['conversation_id'])
        self.assertEqual(participants.count(), 2)
        agent_participant = participants.get(participant_type='agent')
        self.assertEqual(agent_participant.nickname, HERMES_STABLE_ID[:50])
        self.assertEqual(agent_participant.agent_role, 'hermes')
        self.assertEqual(str(agent_participant.agent_id), HERMES_ID)
        self.assertEqual(DirectMessage.objects.count(), 1)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1].content, 'hello hermes')
        self.assertEqual(dispatch.call_args.args[2].nickname, HERMES_STABLE_ID[:50])

    @mock.patch('connect.views.messaging.find_hive_agent_by_lookup', return_value=None)
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('requests.get')
    def test_display_name_username_404_falls_back_to_email(self, get, _broadcast, _hive):
        # Production sequence: SCLI auto-reply sends inbound senderName
        # (jdoe) plus SHIZUHA_CONNECT_REPLY_EMAIL. Username 404 must
        # retry email or the model turn is dropped.
        def side_effect(*args, **kwargs):
            url = args[0]
            if '/internal/users/by-username/' in url:
                return ResponseStub(404, {'error': 'not found'})
            if '/internal/users/by-email/' in url:
                return ResponseStub(200, {
                    'id': 3,
                    'username': 'hritik',
                    'email': 'jdoe@example.com',
                    'first_name': 'Hritik',
                })
            return ResponseStub(500, {'error': url})
        get.side_effect = side_effect

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {
                    'recipient_username': 'jdoe',
                    'recipient_email': 'jdoe@example.com',
                    'content': 'CON-120 username fallback probe',
                },
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['recipient']['user_id'], 3)
        self.assertEqual(response.data['recipient']['email'], 'jdoe@example.com')
        urls = [call.args[0] for call in get.call_args_list]
        self.assertTrue(any('/internal/users/by-username/' in url for url in urls))
        self.assertTrue(any('/internal/users/by-email/' in url for url in urls))


    @mock.patch('connect.views.messaging.dispatch_hive_agent_message')
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('connect.services.hive_agents.requests.get')
    def test_message_user_prefers_owner_scoped_hive_claw_over_platform_human_alias(self, get, _broadcast, dispatch):
        def side_effect(*args, **kwargs):
            url = args[0]
            if '/api/v1/connect/agents/' in url:
                return ResponseStub(200, {'count': 1, 'results': [CLAW_AGENT]})
            if '/internal/users/by-username/' in url:
                return ResponseStub(200, {'id': 70, 'username': 'claw', 'email': 'claw@shizuha.com'})
            return ResponseStub(500, {'error': 'unexpected url'})
        get.side_effect = side_effect

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_username': 'claw', 'content': 'hello claw'},
                format='json',
                HTTP_AUTHORIZATION='Bearer harshita-token',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['recipient']['user_id'], hive_agent_user_id(CLAW_STABLE_ID))
        self.assertEqual(response.data['recipient']['email'], 'hive-claw-demo@hive-agent.local')
        agent_participant = ConversationParticipant.objects.get(
            conversation_id=response.data['conversation_id'],
            participant_type='agent',
        )
        self.assertEqual(agent_participant.agent_role, 'openclaw')
        self.assertEqual(agent_participant.nickname, CLAW_STABLE_ID[:50])
        dispatch.assert_called_once()
        # The platform-human shizuha-id alias must not be used when an owner-scoped
        # Hive Claw is visible; otherwise it renders as participant_type=human and
        # no runtime response is dispatched.
        called_urls = [call.args[0] for call in get.call_args_list]
        self.assertFalse(any('/internal/users/by-username/' in url for url in called_urls))


    @mock.patch('connect.views.messaging.dispatch_hive_agent_message')
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('connect.services.hive_agents.requests.get', side_effect=id_404_then_hive_agents)
    def test_message_user_hive_agent_client_message_id_replays_without_second_dispatch(self, _get, _broadcast, dispatch):
        client_message_id = str(uuid4())
        payload = {'recipient_username': 'hive-hermes-demo', 'content': 'idempotent hello', 'client_message_id': client_message_id}

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post('/api/messaging/dm/', payload, format='json', HTTP_AUTHORIZATION='Bearer harshita-token')
        with self.captureOnCommitCallbacks(execute=True):
            second = self.client.post('/api/messaging/dm/', payload, format='json', HTTP_AUTHORIZATION='Bearer harshita-token')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data['idempotent_replay'])
        self.assertEqual(first.data['message_id'], second.data['message_id'])
        self.assertEqual(DirectMessage.objects.count(), 1)
        dispatch.assert_called_once()


    @mock.patch('connect.views.messaging.dispatch_hive_agent_message')
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('connect.services.hive_agents.requests.get')
    def test_stopped_hive_agent_dm_surfaces_system_delivery_error(self, get, _broadcast, dispatch):
        stopped_agent = dict(HERMES_AGENT, status='stopped')

        def side_effect(*args, **kwargs):
            url = args[0]
            if '/api/v1/connect/agents/' in url:
                return ResponseStub(200, {'count': 1, 'results': [stopped_agent]})
            return ResponseStub(500, {'error': 'unexpected url'})
        get.side_effect = side_effect

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_username': 'hive-hermes-demo', 'content': 'hello stopped hermes'},
                format='json',
                HTTP_AUTHORIZATION='Bearer harshita-token',
            )

        self.assertEqual(response.status_code, 201)
        dispatch.assert_not_called()
        messages = list(DirectMessage.objects.order_by('created_at'))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].content, 'hello stopped hermes')
        self.assertEqual(messages[1].message_type, 'system')
        self.assertEqual(messages[1].sender_id, 0)
        self.assertEqual(messages[1].reply_to_id, messages[0].id)
        self.assertIn('currently stopped', messages[1].content)
        self.assertIn('Start it in Hive', messages[1].content)

    @mock.patch('connect.services.hive_agents.requests.get')
    def test_unknown_hive_alias_remains_404(self, get):
        def side_effect(*args, **kwargs):
            url = args[0]
            if '/internal/users/by-username/' in url:
                return ResponseStub(404, {'error': 'not found'})
            if '/api/v1/connect/agents/' in url:
                return ResponseStub(200, {'count': 0, 'results': []})
            return ResponseStub(500, {'error': 'unexpected url'})
        get.side_effect = side_effect

        response = self.client.post('/api/messaging/dm/', {'recipient_username': 'hermes', 'content': 'hello'}, format='json')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(DirectMessage.objects.count(), 0)

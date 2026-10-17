from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from connect.models import ConversationParticipant


STABLE_ID = f'hive:claw:{uuid4()}'
CLAW_ID = STABLE_ID.rsplit(':', 1)[-1]
AGENT_PAYLOAD = {
    'stable_id': STABLE_ID,
    'hive_claw_id': CLAW_ID,
    'display_name': 'Harshita Claw',
    'connect_username': 'hive-claw-test',
    'agent_type': 'claw',
    'runtime_kind': 'openclaw',
    'effective_model': 'DeepSeek-V4-Flash',
    'model_provider': 'Cortex',
    'runtime_backend': 'cortex',
    'status': 'running',
}


def response_with_agents(agents):
    resp = mock.Mock()
    resp.json.return_value = {'count': len(agents), 'results': agents}
    resp.raise_for_status.return_value = None
    resp.status_code = 200
    return resp


class Con118HiveAgentsTests(TestCase):
    def setUp(self):
        self.profile_delay_patcher = mock.patch('connect.tasks.ensure_participant_social_profile.delay')
        self.profile_delay = self.profile_delay_patcher.start()
        self.addCleanup(self.profile_delay_patcher.stop)
        self.client = APIClient()
        self.user = User(id=101, username='harshita', email='harshita@example.com', first_name='Harshita')
        self.client.force_authenticate(user=self.user)

    @mock.patch('connect.services.hive_agents.requests.get')
    def test_lists_hive_agents_visible_from_hive_owner_scope(self, get):
        get.return_value = response_with_agents([AGENT_PAYLOAD])

        response = self.client.get('/api/hive/agents/', HTTP_AUTHORIZATION='Bearer user-token')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['stable_id'], STABLE_ID)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['headers'], {'Authorization': 'Bearer user-token'})

    @mock.patch('connect.services.hive_agents.requests.get')
    def test_opens_hive_agent_conversation_idempotently(self, get):
        get.return_value = response_with_agents([AGENT_PAYLOAD])

        first = self.client.post('/api/hive/agents/conversation/', {'stable_id': STABLE_ID}, format='json')
        second = self.client.post('/api/hive/agents/conversation/', {'stable_id': STABLE_ID}, format='json')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['id'], second.data['id'])
        participants = ConversationParticipant.objects.filter(conversation_id=first.data['id'])
        self.assertEqual(participants.count(), 2)
        agent_participant = participants.get(participant_type='agent')
        self.assertEqual(agent_participant.nickname, STABLE_ID[:50])
        self.assertEqual(str(agent_participant.agent_id), CLAW_ID)
        self.assertEqual(agent_participant.agent_role, 'openclaw')
        self.assertIn('Harshita Claw', agent_participant.user_name)

    @mock.patch('connect.services.hive_agents.requests.get')
    def test_returns_404_when_hive_does_not_expose_agent_to_owner(self, get):
        get.return_value = response_with_agents([])

        response = self.client.post('/api/hive/agents/conversation/', {'stable_id': STABLE_ID}, format='json')

        self.assertEqual(response.status_code, 404)

    @mock.patch('connect.views.messaging.dispatch_hive_agent_message')
    @mock.patch('connect.views.messaging._safe_broadcast')
    @mock.patch('connect.tasks.send_dm_notification.delay')
    @mock.patch('connect.services.hive_agents.requests.get')
    def test_message_to_hive_agent_dispatches_once_to_runtime_bridge(self, get, delay, broadcast, dispatch):
        get.return_value = response_with_agents([AGENT_PAYLOAD])
        convo = self.client.post('/api/hive/agents/conversation/', {'stable_id': STABLE_ID}, format='json').data

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/conversations/{convo['id']}/messages/",
                {'content': 'hello claw', 'message_type': 'text', 'client_message_id': str(uuid4())},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(dispatch.call_count, 1)
        args = dispatch.call_args.args
        self.assertEqual(str(args[0].id), convo['id'])
        self.assertEqual(args[1].content, 'hello claw')
        self.assertEqual(args[2].nickname, STABLE_ID[:50])

"""
Tests for AI agent participation in group conversations.

Tests cover:
- Adding agent participants to groups
- @mention detection
- Internal API endpoints (agent-message, agent-typing)
- Serialization of new fields
"""

import uuid
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage,
)
from connect.views.messaging import _detect_agent_mentions
from connect.serializers.messaging import (
    ConversationParticipantSerializer, DirectMessageSerializer,
)


class FakeUser:
    """Minimal user object for tests."""

    def __init__(self, user_id=1, email='user@test.com', name='Test User'):
        self.id = user_id
        self.pk = user_id
        self.email = email
        self.username = f'test-user-{user_id}'
        self._name = name
        self.is_authenticated = True
        self.is_active = True

    def get_full_name(self):
        return self._name


class AgentParticipantModelTest(TestCase):
    """Test new model fields for agent participants."""

    def test_participant_default_type_is_human(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        p = ConversationParticipant.objects.create(
            conversation=conv, user_id=1, user_name='Human'
        )
        self.assertEqual(p.participant_type, 'human')
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.agent_role, '')

    def test_participant_agent_type(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        agent_uuid = uuid.uuid4()
        p = ConversationParticipant.objects.create(
            conversation=conv,
            user_id=100,
            user_name='Kai',
            participant_type='agent',
            agent_id=agent_uuid,
            agent_role='Engineer',
        )
        p.refresh_from_db()
        self.assertEqual(p.participant_type, 'agent')
        self.assertEqual(p.agent_id, agent_uuid)
        self.assertEqual(p.agent_role, 'Engineer')

    def test_message_default_not_agent(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        msg = DirectMessage.objects.create(
            conversation=conv, sender_id=1, content='Hello'
        )
        self.assertFalse(msg.sender_is_agent)
        self.assertIsNone(msg.agent_id)

    def test_message_agent_sender(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        agent_uuid = uuid.uuid4()
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=100,
            sender_name='Kai',
            content='Here is the fix.',
            sender_is_agent=True,
            agent_id=agent_uuid,
        )
        msg.refresh_from_db()
        self.assertTrue(msg.sender_is_agent)
        self.assertEqual(msg.agent_id, agent_uuid)


class AgentMentionDetectionTest(TestCase):
    """Test @mention detection for agent participants."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Engineering Chat'
        )
        self.agent_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv,
            user_id=100,
            user_name='Kai Chen',
            participant_type='agent',
            agent_id=self.agent_uuid,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv,
            user_id=1,
            user_name='Hritik',
            participant_type='human',
        )

    def test_detect_agent_mention(self):
        results = _detect_agent_mentions('@Kai fix the bug', self.conv.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], str(self.agent_uuid))
        self.assertEqual(results[0][1], 'kai')

    def test_no_mention(self):
        results = _detect_agent_mentions('Hey everyone, what do you think?', self.conv.id)
        self.assertEqual(len(results), 0)

    def test_mention_human_not_triggered(self):
        results = _detect_agent_mentions('@Hritik what do you think?', self.conv.id)
        self.assertEqual(len(results), 0)

    def test_case_insensitive(self):
        results = _detect_agent_mentions('@kai please review', self.conv.id)
        self.assertEqual(len(results), 1)

    def test_multiple_agents(self):
        agent2_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv,
            user_id=101,
            user_name='Ryo Tanaka',
            participant_type='agent',
            agent_id=agent2_uuid,
        )
        results = _detect_agent_mentions('@Kai and @Ryo please review', self.conv.id)
        self.assertEqual(len(results), 2)
        agent_ids = {r[0] for r in results}
        self.assertIn(str(self.agent_uuid), agent_ids)
        self.assertIn(str(agent2_uuid), agent_ids)

    def test_agent_left_group_not_triggered(self):
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=100
        ).update(has_left=True)
        results = _detect_agent_mentions('@Kai fix it', self.conv.id)
        self.assertEqual(len(results), 0)

    def test_empty_content(self):
        self.assertEqual(_detect_agent_mentions('', self.conv.id), [])
        self.assertEqual(_detect_agent_mentions(None, self.conv.id), [])


class MentionTriggerTest(TestCase):
    """Test that message send triggers notify_agent_mention task."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Test Group'
        )
        self.user = FakeUser(user_id=1, email='user@test.com', name='Test User')
        self.agent_uuid = uuid.uuid4()

        # Add human participant
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Test User',
            user_email='user@test.com', is_admin=True,
        )
        # Add agent participant
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=100, user_name='Kai',
            participant_type='agent', agent_id=self.agent_uuid,
        )

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_mention_triggers_task(self, mock_push, mock_agent):
        from connect.views.messaging import ConversationViewSet
        factory = APIRequestFactory()
        request = factory.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai fix the bug', 'message_type': 'text'},
            format='json',
        )
        force_authenticate(request, user=self.user)
        view = ConversationViewSet.as_view({'post': 'messages'})
        response = view(request, pk=str(self.conv.id))
        self.assertEqual(response.status_code, 201)
        mock_agent.assert_called_once()
        call_args = mock_agent.call_args
        self.assertEqual(call_args[0][0], str(self.conv.id))
        self.assertEqual(call_args[0][2], str(self.agent_uuid))

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_no_mention_no_task(self, mock_push, mock_agent):
        from connect.views.messaging import ConversationViewSet
        factory = APIRequestFactory()
        request = factory.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Hello everyone', 'message_type': 'text'},
            format='json',
        )
        force_authenticate(request, user=self.user)
        view = ConversationViewSet.as_view({'post': 'messages'})
        response = view(request, pk=str(self.conv.id))
        self.assertEqual(response.status_code, 201)
        mock_agent.assert_not_called()


class InternalAgentMessageAPITest(TestCase):
    """Test the internal agent-message endpoint."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Test Group'
        )
        self.agent_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Human',
            is_admin=True, participant_type='human',
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=100, user_name='Kai',
            participant_type='agent', agent_id=self.agent_uuid,
        )
        self.factory = APIRequestFactory()

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_create_agent_message(self, mock_push, mock_broadcast):
        from connect.views.internal_api import InternalAgentMessageView
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {
                'agent_id': str(self.agent_uuid),
                'agent_user_id': 100,
                'agent_name': 'Kai',
                'agent_email': 'kai@shizuha.com',
                'content': 'Here is the fix for the bug.',
            },
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 201)

        # Verify message was created
        msg = DirectMessage.objects.filter(
            conversation=self.conv, sender_is_agent=True
        ).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, 'Here is the fix for the bug.')
        self.assertEqual(msg.sender_name, 'Kai')
        self.assertTrue(msg.sender_is_agent)
        self.assertEqual(msg.agent_id, self.agent_uuid)

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_forged_inbox_pong_is_rejected(self, mock_push, mock_broadcast):
        """SCLI-756: the agent path must reject ack-only pongs like the user path.

        The forged always-on inbox example (`[hritik] Hi <agent>, please reply
        with the word: pong`) makes an agent emit a one-word pong DM — a
        role-leak hallucination answering a fabricated turn. The view must 400
        with an instructive error and persist nothing.
        """
        from connect.views.internal_api import InternalAgentMessageView

        before = DirectMessage.objects.count()
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {
                'agent_id': str(self.agent_uuid),
                'agent_user_id': 100,
                'agent_name': 'Kai',
                'agent_email': 'kai@shizuha.com',
                'content': 'pong',
            },
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        response = InternalAgentMessageView.as_view()(
            request, conversation_id=self.conv.id
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('forged-inbox pong suspected', response.data['error'])
        self.assertIn('role-leak hallucination', response.data['error'])
        self.assertEqual(DirectMessage.objects.count(), before)
        mock_push.assert_not_called()
        mock_broadcast.assert_not_called()

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_agent_message_client_message_id_replay_is_idempotent(self, mock_push, mock_broadcast):
        from connect.views.internal_api import InternalAgentMessageView

        view = InternalAgentMessageView.as_view()
        payload = {
            'agent_id': str(self.agent_uuid),
            'agent_user_id': 100,
            'agent_name': 'Kai',
            'agent_email': 'kai@shizuha.com',
            'content': 'Here is the fix for the bug.',
            'client_message_id': 'connect-runtime:stable-delivery-key',
        }

        first = view(
            self.factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                payload,
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            ),
            conversation_id=self.conv.id,
        )
        second = view(
            self.factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                payload,
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            ),
            conversation_id=self.conv.id,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['id'], second.data['id'])
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.conv, sender_is_agent=True).count(),
            1,
        )
        msg = DirectMessage.objects.get(conversation=self.conv, sender_is_agent=True)
        self.assertIsNotNone(msg.client_message_id)
        human = ConversationParticipant.objects.get(conversation=self.conv, user_id=1)
        self.assertEqual(human.unread_count, 1)
        mock_push.assert_called_once_with(str(msg.id))
        self.assertEqual(mock_broadcast.call_count, 2)  # new message + typing stop, only on first send

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_agent_message_client_message_id_conflict_rejects_different_body(self, mock_push, mock_broadcast):
        from connect.views.internal_api import InternalAgentMessageView

        view = InternalAgentMessageView.as_view()
        base_payload = {
            'agent_id': str(self.agent_uuid),
            'agent_user_id': 100,
            'agent_name': 'Kai',
            'agent_email': 'kai@shizuha.com',
            'content': 'First response',
            'client_message_id': 'connect-runtime:stable-delivery-key',
        }

        first = view(
            self.factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                base_payload,
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            ),
            conversation_id=self.conv.id,
        )
        conflict = view(
            self.factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                {**base_payload, 'content': 'Different response'},
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            ),
            conversation_id=self.conv.id,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn('client_message_id', str(conflict.data))
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.conv, sender_is_agent=True).count(),
            1,
        )
        mock_push.assert_called_once()
        self.assertEqual(mock_broadcast.call_count, 2)

    def test_rejects_without_header(self):
        from connect.views.internal_api import InternalAgentMessageView
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {'content': 'test', 'agent_user_id': 100},
            format='json',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 403)

    def test_rejects_empty_content(self):
        from connect.views.internal_api import InternalAgentMessageView
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {'agent_id': str(self.agent_uuid), 'agent_user_id': 100, 'content': ''},
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 400)

    def test_rejects_nonparticipant_agent(self):
        from connect.views.internal_api import InternalAgentMessageView
        other_agent_uuid = uuid.uuid4()
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {
                'agent_id': str(other_agent_uuid),
                'agent_user_id': 200,
                'content': 'Not in this group',
            },
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 403)


class InternalAgentTypingAPITest(TestCase):
    """Test the internal agent-typing endpoint."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Test Group'
        )
        self.factory = APIRequestFactory()

    @patch('connect.views.internal_api._safe_broadcast')
    def test_typing_indicator(self, mock_broadcast):
        from connect.views.internal_api import InternalAgentTypingView
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-typing/',
            {'agent_user_id': 100, 'agent_name': 'Kai', 'is_typing': True},
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentTypingView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 200)
        mock_broadcast.assert_called_once()

    def test_rejects_without_header(self):
        from connect.views.internal_api import InternalAgentTypingView
        request = self.factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-typing/',
            {'agent_user_id': 100, 'is_typing': True},
            format='json',
        )
        view = InternalAgentTypingView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 403)


class SerializerFieldTest(TestCase):
    """Test that new fields appear in serialized output."""

    def test_participant_serializer_includes_agent_fields(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        agent_uuid = uuid.uuid4()
        p = ConversationParticipant.objects.create(
            conversation=conv, user_id=100, user_name='Kai',
            participant_type='agent', agent_id=agent_uuid, agent_role='Engineer',
        )
        data = ConversationParticipantSerializer(p).data
        self.assertEqual(data['participant_type'], 'agent')
        self.assertEqual(data['agent_id'], str(agent_uuid))
        self.assertEqual(data['agent_role'], 'Engineer')

    def test_message_serializer_includes_agent_fields(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        agent_uuid = uuid.uuid4()
        msg = DirectMessage.objects.create(
            conversation=conv, sender_id=100, sender_name='Kai',
            content='Response', sender_is_agent=True, agent_id=agent_uuid,
        )
        data = DirectMessageSerializer(msg).data
        self.assertTrue(data['sender_is_agent'])
        self.assertEqual(data['agent_id'], str(agent_uuid))
        self.assertEqual(data['conversation_type'], 'group')

    def test_message_serializer_includes_authoritative_direct_type(self):
        conv = Conversation.objects.create(conversation_type='direct')
        msg = DirectMessage.objects.create(
            conversation=conv, sender_id=1, content='Acknowledged.'
        )

        data = DirectMessageSerializer(msg).data

        self.assertEqual(data['conversation_type'], 'direct')
        self.assertFalse(data['sender_same_org'])

    @patch('connect.services.org_tenancy.get_user_org_ids', return_value=frozenset({7, 9}))
    def test_message_serializer_stamps_recipient_relative_same_org(self, _orgs):
        conv = Conversation.objects.create(conversation_type='direct')
        msg = DirectMessage.objects.create(
            conversation=conv, sender_id=1, content='Acknowledged.'
        )

        data = DirectMessageSerializer(
            msg, context={'recipient_org_ids': frozenset({9})},
        ).data

        self.assertTrue(data['sender_same_org'])

    def test_human_message_defaults(self):
        conv = Conversation.objects.create(conversation_type='group', name='Test')
        msg = DirectMessage.objects.create(
            conversation=conv, sender_id=1, content='Hello'
        )
        data = DirectMessageSerializer(msg).data
        self.assertFalse(data['sender_is_agent'])
        self.assertIsNone(data['agent_id'])


class AddAgentParticipantTest(TestCase):
    """Test adding agent participants to group conversations."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Test Group'
        )
        self.user = FakeUser(user_id=1, email='user@test.com', name='Test User')
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Test User',
            user_email='user@test.com', is_admin=True,
        )
        self.factory = APIRequestFactory()

    @patch('connect.views.messaging._safe_broadcast')
    def test_add_agent_participant(self, mock_broadcast):
        from connect.views.messaging import ConversationViewSet
        agent_uuid = uuid.uuid4()
        request = self.factory.post(
            f'/api/conversations/{self.conv.id}/participants/',
            {
                'user_id': 100,
                'user_name': 'Kai',
                'user_email': 'kai@shizuha.com',
                'participant_type': 'agent',
                'agent_id': str(agent_uuid),
                'agent_role': 'Engineer',
            },
            format='json',
        )
        force_authenticate(request, user=self.user)
        view = ConversationViewSet.as_view({'post': 'participants'})
        response = view(request, pk=str(self.conv.id))
        self.assertEqual(response.status_code, 200)

        # Verify participant was created as agent
        p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=100
        )
        self.assertEqual(p.participant_type, 'agent')
        self.assertEqual(p.agent_id, agent_uuid)
        self.assertEqual(p.agent_role, 'Engineer')

        # The join system message names the added participant without a
        # hardcoded "(AI Agent)" suffix: agents and humans share the same
        # participant experience (messaging.py, commit 4fc0b3e). Agent-ness is
        # carried in structured data — participant_type='agent' (asserted above)
        # and the serialized participant broadcast — which the UI renders as a
        # badge, rather than being baked into a free-text string.
        sys_msg = DirectMessage.objects.filter(
            conversation=self.conv, message_type='system'
        ).order_by('-created_at').first()
        self.assertIn('added Kai', sys_msg.content)
        self.assertNotIn('(AI Agent)', sys_msg.content)

"""
E2E stress tests for group chat messaging with AI agents.

Tests cover:
- Cross-user message delivery and ordering
- Multiple concurrent senders
- Agent participant @mention triggering
- Internal agent message endpoint (simulates agent responses)
- Message ordering verification via REST API
- WebSocket delivery verification
- Client idempotency (client_message_id)
- Mixed human+agent group conversations
- Rapid-fire messaging
- Large message batches with ordering guarantees
"""

import uuid
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage,
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


def make_client(user_id, email, name):
    """Create an authenticated APIClient for a fake user."""
    client = APIClient()
    user = FakeUser(user_id=user_id, email=email, name=name)
    client.force_authenticate(user=user)
    client._test_user = user
    return client


def _no_throttle(self, request, view):
    """Bypass throttling in tests."""
    return True


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class MessageOrderingTest(TransactionTestCase):
    """Verify messages maintain proper ordering under concurrent sends."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Ordering Test'
        )
        # Add 4 human participants
        self.users = [
            (1, 'alice@test.com', 'Alice'),
            (2, 'bob@test.com', 'Bob'),
            (3, 'charlie@test.com', 'Charlie'),
            (4, 'dave@test.com', 'Dave'),
        ]
        self.clients = {}
        for uid, email, name in self.users:
            ConversationParticipant.objects.create(
                conversation=self.conv, user_id=uid, user_name=name,
                user_email=email, is_admin=(uid == 1),
            )
            self.clients[uid] = make_client(uid, email, name)

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_sequential_messages_ordered(self, mock_bc, mock_push):
        """Messages sent sequentially should be returned in order."""
        client = self.clients[1]
        message_ids = []
        for i in range(20):
            resp = client.post(
                f'/api/conversations/{self.conv.id}/messages/',
                {'content': f'Message {i:03d}', 'message_type': 'text'},
                format='json',
            )
            self.assertEqual(resp.status_code, 201, f"Failed on message {i}: {resp.data}")
            message_ids.append(resp.data['id'])

        # Fetch all messages
        resp = self.clients[2].get(
            f'/api/conversations/{self.conv.id}/messages/',
            {'limit': 50},
        )
        self.assertEqual(resp.status_code, 200)
        messages = resp.data['messages']

        # Verify ordering: messages should be chronologically sorted
        timestamps = [m['created_at'] for m in messages]
        self.assertEqual(timestamps, sorted(timestamps),
                         "Messages are not in chronological order")

        # Verify content order matches send order
        text_msgs = [m for m in messages if m['message_type'] == 'text']
        for i, msg in enumerate(text_msgs):
            self.assertEqual(msg['content'], f'Message {i:03d}')

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_concurrent_senders_ordering(self, mock_bc, mock_push):
        """Multiple users sending concurrently should produce ordered messages."""
        results = []
        errors = []

        def send_messages(uid, count):
            client = self.clients[uid]
            for i in range(count):
                try:
                    resp = client.post(
                        f'/api/conversations/{self.conv.id}/messages/',
                        {
                            'content': f'User{uid}-Msg{i:03d}',
                            'message_type': 'text',
                            'client_message_id': str(uuid.uuid4()),
                        },
                        format='json',
                    )
                    if resp.status_code == 201:
                        results.append(resp.data)
                    else:
                        errors.append(f"User {uid} msg {i}: {resp.status_code} {resp.data}")
                except Exception as e:
                    errors.append(f"User {uid} msg {i}: {e}")

        threads = []
        for uid in [1, 2, 3, 4]:
            t = threading.Thread(target=send_messages, args=(uid, 10))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(results), 40, f"Expected 40 messages, got {len(results)}")

        # Fetch all messages and verify ordering
        resp = self.clients[1].get(
            f'/api/conversations/{self.conv.id}/messages/',
            {'limit': 100},
        )
        messages = resp.data['messages']
        timestamps = [m['created_at'] for m in messages]
        self.assertEqual(timestamps, sorted(timestamps),
                         "Concurrent messages are not in chronological order")

        # Verify all 4 users' messages are present
        senders = {m['sender_id'] for m in messages if m['message_type'] == 'text'}
        self.assertEqual(senders, {1, 2, 3, 4})

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_rapid_fire_single_sender(self, mock_bc, mock_push):
        """50 messages sent as fast as possible should all persist in order."""
        client = self.clients[1]
        sent_ids = []
        for i in range(50):
            resp = client.post(
                f'/api/conversations/{self.conv.id}/messages/',
                {
                    'content': f'Rapid-{i:04d}',
                    'message_type': 'text',
                    'client_message_id': str(uuid.uuid4()),
                },
                format='json',
            )
            self.assertEqual(resp.status_code, 201)
            sent_ids.append(resp.data['id'])

        # Verify count
        total = DirectMessage.objects.filter(
            conversation=self.conv, message_type='text'
        ).count()
        self.assertEqual(total, 50)

        # Verify order in DB
        db_messages = list(
            DirectMessage.objects.filter(conversation=self.conv, message_type='text')
            .order_by('created_at')
            .values_list('content', flat=True)
        )
        expected = [f'Rapid-{i:04d}' for i in range(50)]
        self.assertEqual(db_messages, expected)


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class ClientIdempotencyTest(TestCase):
    """Test that client_message_id prevents duplicate messages."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Idempotency Test'
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Alice',
            user_email='alice@test.com', is_admin=True,
        )
        self.client = make_client(1, 'alice@test.com', 'Alice')

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_duplicate_client_message_id_returns_existing(self, mock_bc, mock_push):
        """Sending the same client_message_id twice should not create a duplicate."""
        client_id = str(uuid.uuid4())

        resp1 = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {
                'content': 'Original message',
                'message_type': 'text',
                'client_message_id': client_id,
            },
            format='json',
        )
        self.assertEqual(resp1.status_code, 201)

        resp2 = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {
                'content': 'Original message',
                'message_type': 'text',
                'client_message_id': client_id,
            },
            format='json',
        )
        # Should either return 200 (existing) or 201 (dedup)
        self.assertIn(resp2.status_code, [200, 201])

        # Either way, only 1 text message should exist
        count = DirectMessage.objects.filter(
            conversation=self.conv, message_type='text'
        ).count()
        self.assertEqual(count, 1, "Duplicate message was created")

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_different_client_ids_create_separate_messages(self, mock_bc, mock_push):
        """Different client_message_ids should create separate messages."""
        for i in range(5):
            resp = self.client.post(
                f'/api/conversations/{self.conv.id}/messages/',
                {
                    'content': f'Message {i}',
                    'message_type': 'text',
                    'client_message_id': str(uuid.uuid4()),
                },
                format='json',
            )
            self.assertEqual(resp.status_code, 201)

        count = DirectMessage.objects.filter(
            conversation=self.conv, message_type='text'
        ).count()
        self.assertEqual(count, 5)


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class AgentInGroupStressTest(TransactionTestCase):
    """Stress test agent participation in group conversations."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Agent Stress Test'
        )
        # Humans
        self.humans = [
            (1, 'alice@test.com', 'Alice'),
            (2, 'bob@test.com', 'Bob'),
            (3, 'charlie@test.com', 'Charlie'),
        ]
        self.clients = {}
        for uid, email, name in self.humans:
            ConversationParticipant.objects.create(
                conversation=self.conv, user_id=uid, user_name=name,
                user_email=email, is_admin=(uid == 1),
            )
            self.clients[uid] = make_client(uid, email, name)

        # Agents
        self.agents = [
            (100, 'kai@test.com', 'Kai', uuid.uuid4(), 'Engineer'),
            (101, 'ryo@test.com', 'Ryo', uuid.uuid4(), 'Engineer'),
            (102, 'zen@test.com', 'Zen', uuid.uuid4(), 'QA Engineer'),
        ]
        for uid, email, name, agent_uuid, role in self.agents:
            ConversationParticipant.objects.create(
                conversation=self.conv, user_id=uid, user_name=name,
                user_email=email, participant_type='agent',
                agent_id=agent_uuid, agent_role=role,
            )

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_single_mention_triggers_single_task(self, mock_bc, mock_push, mock_agent):
        """@Kai mention should trigger exactly one agent task."""
        resp = self.clients[1].post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai please review the PR', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_agent.assert_called_once()
        # Verify correct agent UUID was passed
        call_args = mock_agent.call_args
        self.assertEqual(call_args[0][2], str(self.agents[0][3]))  # Kai's UUID

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_multiple_mentions_trigger_multiple_tasks(self, mock_bc, mock_push, mock_agent):
        """@Kai and @Ryo in same message should trigger 2 tasks."""
        resp = self.clients[1].post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai and @Ryo please review', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_agent.call_count, 2)

        # Verify both agents were triggered
        called_uuids = {call[0][2] for call in mock_agent.call_args_list}
        expected_uuids = {str(self.agents[0][3]), str(self.agents[1][3])}
        self.assertEqual(called_uuids, expected_uuids)

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_mention_all_agents(self, mock_bc, mock_push, mock_agent):
        """Mentioning all 3 agents should trigger 3 tasks."""
        resp = self.clients[2].post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai @Ryo @Zen review please', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_agent.call_count, 3)

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_human_mention_not_triggered(self, mock_bc, mock_push, mock_agent):
        """@Alice (human) should NOT trigger agent task."""
        resp = self.clients[2].post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Alice what do you think?', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_agent.assert_not_called()

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_rapid_mentions_all_trigger(self, mock_bc, mock_push, mock_agent):
        """10 rapid @Kai mentions should trigger 10 tasks."""
        for i in range(10):
            resp = self.clients[1].post(
                f'/api/conversations/{self.conv.id}/messages/',
                {'content': f'@Kai task #{i}', 'message_type': 'text'},
                format='json',
            )
            self.assertEqual(resp.status_code, 201)

        self.assertEqual(mock_agent.call_count, 10)

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_removed_agent_not_triggered(self, mock_bc, mock_push, mock_agent):
        """Agent that left the group should not be triggered."""
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=100  # Kai
        ).update(has_left=True)

        resp = self.clients[1].post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai are you still here?', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_agent.assert_not_called()

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_concurrent_mentions_from_different_users(self, mock_bc, mock_push, mock_agent):
        """Multiple users mentioning agents concurrently."""
        errors = []

        def send_mention(uid, agent_name):
            try:
                resp = self.clients[uid].post(
                    f'/api/conversations/{self.conv.id}/messages/',
                    {
                        'content': f'@{agent_name} urgent task',
                        'message_type': 'text',
                        'client_message_id': str(uuid.uuid4()),
                    },
                    format='json',
                )
                if resp.status_code != 201:
                    errors.append(f"User {uid}: {resp.status_code}")
            except Exception as e:
                errors.append(str(e))

        threads = []
        # 3 humans each mention a different agent
        for uid, agent_name in [(1, 'Kai'), (2, 'Ryo'), (3, 'Zen')]:
            t = threading.Thread(target=send_mention, args=(uid, agent_name))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(mock_agent.call_count, 3)


class InternalAgentMessageStressTest(TransactionTestCase):
    """Stress test internal agent message delivery."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Agent Response Test'
        )
        self.agent_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Alice',
            user_email='alice@test.com', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=100, user_name='Kai',
            user_email='kai@test.com', participant_type='agent',
            agent_id=self.agent_uuid,
        )
        self.int_client = APIClient()  # No auth needed for internal API

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_agent_message_created_correctly(self, mock_push, mock_bc):
        """Agent message via internal API should have correct fields."""
        from connect.views.internal_api import InternalAgentMessageView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {
                'agent_id': str(self.agent_uuid),
                'agent_user_id': 100,
                'agent_name': 'Kai',
                'agent_email': 'kai@test.com',
                'content': 'Here is my analysis of the code.',
            },
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 201)

        msg = DirectMessage.objects.filter(
            conversation=self.conv, sender_is_agent=True
        ).first()
        self.assertIsNotNone(msg)
        self.assertTrue(msg.sender_is_agent)
        self.assertEqual(msg.agent_id, self.agent_uuid)
        self.assertEqual(msg.sender_id, 100)
        self.assertEqual(msg.sender_name, 'Kai')
        self.assertEqual(msg.content, 'Here is my analysis of the code.')

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_multiple_agent_responses(self, mock_push, mock_bc):
        """Multiple agent responses should all be created and ordered."""
        from connect.views.internal_api import InternalAgentMessageView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        view = InternalAgentMessageView.as_view()

        for i in range(10):
            request = factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                {
                    'agent_id': str(self.agent_uuid),
                    'agent_user_id': 100,
                    'agent_name': 'Kai',
                    'agent_email': 'kai@test.com',
                    'content': f'Response {i:03d}',
                },
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            )
            response = view(request, conversation_id=self.conv.id)
            self.assertEqual(response.status_code, 201)

        # Verify all 10 messages exist and are ordered
        agent_msgs = list(
            DirectMessage.objects.filter(
                conversation=self.conv, sender_is_agent=True
            ).order_by('created_at').values_list('content', flat=True)
        )
        expected = [f'Response {i:03d}' for i in range(10)]
        self.assertEqual(agent_msgs, expected)

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_interleaved_human_and_agent_messages(self, mock_push, mock_bc):
        """Human messages and agent responses interleaved should maintain order."""
        from connect.views.internal_api import InternalAgentMessageView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        int_view = InternalAgentMessageView.as_view()
        human_client = make_client(1, 'alice@test.com', 'Alice')

        expected_sequence = []

        for i in range(5):
            # Human sends
            resp = human_client.post(
                f'/api/conversations/{self.conv.id}/messages/',
                {'content': f'Human-{i}', 'message_type': 'text'},
                format='json',
            )
            self.assertEqual(resp.status_code, 201)
            expected_sequence.append(f'Human-{i}')

            # Agent responds
            request = factory.post(
                f'/api/internal/conversations/{self.conv.id}/agent-message/',
                {
                    'agent_id': str(self.agent_uuid),
                    'agent_user_id': 100,
                    'agent_name': 'Kai',
                    'agent_email': 'kai@test.com',
                    'content': f'Agent-{i}',
                },
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-agent',
            )
            response = int_view(request, conversation_id=self.conv.id)
            self.assertEqual(response.status_code, 201)
            expected_sequence.append(f'Agent-{i}')

        # Verify interleaved order
        all_msgs = list(
            DirectMessage.objects.filter(
                conversation=self.conv, message_type='text'
            ).order_by('created_at').values_list('content', flat=True)
        )
        self.assertEqual(all_msgs, expected_sequence)


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class MultiGroupStressTest(TransactionTestCase):
    """Test messaging across multiple groups simultaneously."""

    def setUp(self):
        self.groups = []
        self.clients = {}

        # Create 3 groups with overlapping membership
        for g in range(3):
            conv = Conversation.objects.create(
                conversation_type='group', name=f'Group {g}'
            )
            self.groups.append(conv)

        # 4 users, each in different combinations of groups
        users = [
            (1, 'alice@test.com', 'Alice'),
            (2, 'bob@test.com', 'Bob'),
            (3, 'charlie@test.com', 'Charlie'),
            (4, 'dave@test.com', 'Dave'),
        ]

        # Group 0: Alice, Bob, Charlie
        # Group 1: Bob, Charlie, Dave
        # Group 2: Alice, Dave (+ agent Kai)
        memberships = {
            0: [1, 2, 3],
            1: [2, 3, 4],
            2: [1, 4],
        }

        for uid, email, name in users:
            self.clients[uid] = make_client(uid, email, name)

        for gidx, member_ids in memberships.items():
            for uid in member_ids:
                _, email, name = users[uid - 1]
                ConversationParticipant.objects.create(
                    conversation=self.groups[gidx], user_id=uid,
                    user_name=name, user_email=email, is_admin=(uid == member_ids[0]),
                )

        # Add agent to group 2
        self.agent_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.groups[2], user_id=100, user_name='Kai',
            user_email='kai@test.com', participant_type='agent',
            agent_id=self.agent_uuid, agent_role='Engineer',
        )

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_messages_stay_in_correct_groups(self, mock_bc, mock_push, mock_agent):
        """Messages sent to different groups should not cross-contaminate."""
        # Send 5 messages to each group
        for gidx, conv in enumerate(self.groups):
            members = ConversationParticipant.objects.filter(
                conversation=conv
            ).values_list('user_id', flat=True)
            # Pick first human member
            sender_id = [m for m in members if m < 100][0]
            for i in range(5):
                resp = self.clients[sender_id].post(
                    f'/api/conversations/{conv.id}/messages/',
                    {'content': f'Group{gidx}-Msg{i}', 'message_type': 'text'},
                    format='json',
                )
                self.assertEqual(resp.status_code, 201)

        # Verify each group has exactly 5 text messages
        for gidx, conv in enumerate(self.groups):
            msgs = DirectMessage.objects.filter(
                conversation=conv, message_type='text'
            )
            self.assertEqual(msgs.count(), 5, f"Group {gidx} has wrong message count")
            for msg in msgs:
                self.assertTrue(
                    msg.content.startswith(f'Group{gidx}'),
                    f"Message '{msg.content}' leaked into group {gidx}"
                )

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_concurrent_messages_across_groups(self, mock_bc, mock_push, mock_agent):
        """Concurrent sends to different groups should be isolated."""
        errors = []

        def send_to_group(gidx, sender_id, count):
            for i in range(count):
                try:
                    resp = self.clients[sender_id].post(
                        f'/api/conversations/{self.groups[gidx].id}/messages/',
                        {
                            'content': f'G{gidx}-U{sender_id}-{i:03d}',
                            'message_type': 'text',
                            'client_message_id': str(uuid.uuid4()),
                        },
                        format='json',
                    )
                    if resp.status_code != 201:
                        errors.append(f"G{gidx} U{sender_id}: {resp.status_code}")
                except Exception as e:
                    errors.append(str(e))

        threads = [
            threading.Thread(target=send_to_group, args=(0, 1, 10)),
            threading.Thread(target=send_to_group, args=(0, 2, 10)),
            threading.Thread(target=send_to_group, args=(1, 3, 10)),
            threading.Thread(target=send_to_group, args=(1, 4, 10)),
            threading.Thread(target=send_to_group, args=(2, 1, 10)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")

        # Group 0: 20 msgs (Alice + Bob)
        # Group 1: 20 msgs (Charlie + Dave)
        # Group 2: 10 msgs (Alice)
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.groups[0], message_type='text').count(), 20
        )
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.groups[1], message_type='text').count(), 20
        )
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.groups[2], message_type='text').count(), 10
        )

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_agent_mention_only_in_agent_group(self, mock_bc, mock_push, mock_agent):
        """@Kai mention in group without agent should not trigger task."""
        # Group 0 has no agents
        resp = self.clients[1].post(
            f'/api/conversations/{self.groups[0].id}/messages/',
            {'content': '@Kai help me', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_agent.assert_not_called()

        # Group 2 has Kai
        resp = self.clients[1].post(
            f'/api/conversations/{self.groups[2].id}/messages/',
            {'content': '@Kai help me', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_agent.assert_called_once()


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class WebSocketBroadcastTest(TestCase):
    """Test that message sends trigger correct WebSocket broadcasts."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='WS Test'
        )
        for uid, name in [(1, 'Alice'), (2, 'Bob')]:
            ConversationParticipant.objects.create(
                conversation=self.conv, user_id=uid, user_name=name,
                user_email=f'{name.lower()}@test.com', is_admin=(uid == 1),
            )
        self.agent_uuid = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=100, user_name='Kai',
            participant_type='agent', agent_id=self.agent_uuid,
        )
        self.client = make_client(1, 'alice@test.com', 'Alice')

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_broadcast_called_with_correct_group(self, mock_bc, mock_push, mock_agent):
        """Broadcast should target the conversation's chat group."""
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Hello', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        mock_bc.assert_called_once()
        call_args = mock_bc.call_args
        # First arg should be the group name
        self.assertEqual(call_args[0][0], f'chat_{self.conv.id}')

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_broadcast_contains_message_data(self, mock_bc, mock_push, mock_agent):
        """Broadcast event should contain the serialized message."""
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Test broadcast', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        call_args = mock_bc.call_args
        event = call_args[0][1]
        self.assertEqual(event['type'], 'new_message')
        self.assertEqual(event['message_data']['content'], 'Test broadcast')

    @patch('connect.views.internal_api._safe_broadcast')
    @patch('connect.tasks.send_dm_notification.delay')
    def test_agent_message_broadcast(self, mock_push, mock_bc):
        """Agent message should trigger broadcast (message + typing stop)."""
        from connect.views.internal_api import InternalAgentMessageView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-message/',
            {
                'agent_id': str(self.agent_uuid),
                'agent_user_id': 100,
                'agent_name': 'Kai',
                'agent_email': 'kai@test.com',
                'content': 'Agent response here',
            },
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentMessageView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 201)

        # Agent message view calls _safe_broadcast twice: message + typing stop
        self.assertEqual(mock_bc.call_count, 2)

        # First call: new_message broadcast
        msg_event = mock_bc.call_args_list[0][0][1]
        self.assertEqual(msg_event['type'], 'new_message')
        self.assertTrue(msg_event['message_data']['sender_is_agent'])
        self.assertEqual(msg_event['message_data']['content'], 'Agent response here')

        # Second call: typing stop
        typing_event = mock_bc.call_args_list[1][0][1]
        self.assertEqual(typing_event['type'], 'typing_indicator')
        self.assertFalse(typing_event['is_typing'])


class MessageFetchPaginationTest(TestCase):
    """Test message pagination with large datasets."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Pagination Test'
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Alice',
            user_email='alice@test.com', is_admin=True,
        )
        self.client = make_client(1, 'alice@test.com', 'Alice')

        # Create 100 messages directly in DB for speed
        msgs = []
        for i in range(100):
            msgs.append(DirectMessage(
                conversation=self.conv, sender_id=1, sender_name='Alice',
                content=f'Msg-{i:04d}', message_type='text',
            ))
        DirectMessage.objects.bulk_create(msgs)

    def test_default_pagination(self):
        """Default fetch should return limited messages."""
        resp = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        self.assertEqual(resp.status_code, 200)
        messages = resp.data['messages']
        self.assertLessEqual(len(messages), 50)  # Default limit
        self.assertTrue(resp.data['has_more'])

    def test_cursor_pagination_ordering(self):
        """Cursor pagination should maintain consistent ordering."""
        all_messages = []
        cursor = None

        for _ in range(10):  # Max 10 pages to prevent infinite loop
            params = {'limit': 20}
            if cursor:
                params['before'] = cursor
            resp = self.client.get(
                f'/api/conversations/{self.conv.id}/messages/',
                params,
            )
            self.assertEqual(resp.status_code, 200)
            batch = resp.data['messages']
            if not batch:
                break
            # Prepend older messages (they come before current ones)
            all_messages = batch + all_messages
            # Use oldest message in batch as next cursor (to fetch even older)
            cursor = batch[0]['id']
            if not resp.data['has_more']:
                break

        # Verify we got all 100 messages
        self.assertEqual(len(all_messages), 100)

        # Verify ordering consistency
        timestamps = [m['created_at'] for m in all_messages]
        self.assertEqual(timestamps, sorted(timestamps))


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class NonParticipantAccessTest(TestCase):
    """Test that non-participants cannot access conversation messages."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Private Group'
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Alice',
            user_email='alice@test.com', is_admin=True,
        )
        # User 99 is NOT a participant
        self.outsider = make_client(99, 'outsider@test.com', 'Outsider')

    def test_outsider_cannot_send_message(self):
        """Non-participant should not be able to send messages."""
        resp = self.outsider.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Sneaky message', 'message_type': 'text'},
            format='json',
        )
        self.assertIn(resp.status_code, [403, 404])

    def test_outsider_cannot_fetch_messages(self):
        """Non-participant should not be able to read messages."""
        resp = self.outsider.get(f'/api/conversations/{self.conv.id}/messages/')
        self.assertIn(resp.status_code, [403, 404])

    def test_outsider_cannot_add_participants(self):
        """Non-participant should not be able to add people to the group."""
        resp = self.outsider.post(
            f'/api/conversations/{self.conv.id}/participants/',
            {'user_id': 50, 'user_name': 'Intruder', 'user_email': 'intruder@test.com'},
            format='json',
        )
        self.assertIn(resp.status_code, [403, 404])


class AgentTypingIndicatorTest(TestCase):
    """Test agent typing indicator internal API."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Typing Test'
        )

    @patch('connect.views.internal_api._safe_broadcast')
    def test_typing_start(self, mock_bc):
        from connect.views.internal_api import InternalAgentTypingView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-typing/',
            {'agent_user_id': 100, 'agent_name': 'Kai', 'is_typing': True},
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentTypingView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 200)
        mock_bc.assert_called_once()

        event = mock_bc.call_args[0][1]
        self.assertEqual(event['type'], 'typing_indicator')
        self.assertTrue(event['is_typing'])
        self.assertEqual(event['user_id'], 100)

    @patch('connect.views.internal_api._safe_broadcast')
    def test_typing_stop(self, mock_bc):
        from connect.views.internal_api import InternalAgentTypingView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-typing/',
            {'agent_user_id': 100, 'agent_name': 'Kai', 'is_typing': False},
            format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        view = InternalAgentTypingView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 200)

        event = mock_bc.call_args[0][1]
        self.assertFalse(event['is_typing'])

    def test_typing_rejects_bad_header(self):
        from connect.views.internal_api import InternalAgentTypingView
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post(
            f'/api/internal/conversations/{self.conv.id}/agent-typing/',
            {'agent_user_id': 100, 'is_typing': True},
            format='json',
            HTTP_X_INTERNAL_SERVICE='some-random-service',
        )
        view = InternalAgentTypingView.as_view()
        response = view(request, conversation_id=self.conv.id)
        self.assertEqual(response.status_code, 403)


@patch('connect.views.messaging.MessageSendThrottle.allow_request', _no_throttle)
class DirectConversationTest(TestCase):
    """Test that agent features work correctly in 1:1 DMs too."""

    def setUp(self):
        self.conv = Conversation.objects.create(
            conversation_type='direct', name=''
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, user_name='Alice',
            user_email='alice@test.com',
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=2, user_name='Bob',
            user_email='bob@test.com',
        )
        self.client = make_client(1, 'alice@test.com', 'Alice')

    @patch('connect.tasks.notify_agent_mention.delay')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.messaging._safe_broadcast')
    def test_mention_in_direct_does_not_trigger(self, mock_bc, mock_push, mock_agent):
        """@mentions in direct (1:1) conversations should not trigger agent tasks."""
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': '@Kai help me', 'message_type': 'text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        # Agent mention detection only runs in group conversations
        mock_agent.assert_not_called()

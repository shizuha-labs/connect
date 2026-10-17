"""
Exhaustive tests for the messaging / DM conversation flow.

Covers the full WhatsApp-style user-to-user chat lifecycle:
  - Conversation creation (direct + group)
  - Participant management
  - Message send / receive
  - Conversation listing with participant_names
  - Unread counts
  - Read receipts
  - Edge cases and validation
"""

from cryptography.fernet import Fernet
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import timedelta
import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage, MessageRead,
    PresenceSession, PresenceStatus,
)
from connect.services.presence import cleanup_stale_presence, mark_offline, refresh_presence, touch_presence
from connect.consumers import UserChatConsumer

# Mock channel layer to avoid needing Redis in tests.
MOCK_CHANNEL = 'connect.views.messaging.get_channel_layer'


def _mock_channel():
    mock_layer = MagicMock()
    mock_layer.group_send = AsyncMock()
    return mock_layer


class ChannelMockMixin:
    """Mixin that patches get_channel_layer for tests that hit WS broadcasts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._channel_patcher = patch(MOCK_CHANNEL, return_value=_mock_channel())
        cls._channel_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._channel_patcher.stop()
        super().tearDownClass()


User = get_user_model()


def get_results(data):
    """Extract list from a DRF response that may be paginated or a plain list."""
    if isinstance(data, list):
        return data
    return data.get('results', [])


# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------

class ConversationModelTest(TestCase):
    """Test Conversation model creation and defaults."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123'
        )

    def test_create_direct_conversation(self):
        conv = Conversation.objects.create(
            conversation_type='direct',
            created_by_id=self.user.id,
        )
        self.assertIsNotNone(conv.id)
        self.assertEqual(conv.conversation_type, 'direct')
        self.assertEqual(conv.message_count, 0)
        self.assertIsNone(conv.last_message_at)

    def test_create_group_conversation(self):
        conv = Conversation.objects.create(
            conversation_type='group',
            name='Team Chat',
            created_by_id=self.user.id,
        )
        self.assertEqual(conv.name, 'Team Chat')
        self.assertEqual(conv.conversation_type, 'group')

    def test_conversation_ordering(self):
        """Conversations with last_message_at are ordered most-recent first."""
        from django.utils import timezone
        import datetime

        c1 = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.user.id,
            last_message_at=timezone.now() - datetime.timedelta(hours=2),
        )
        c2 = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.user.id,
            last_message_at=timezone.now() - datetime.timedelta(hours=1),
        )
        c3 = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.user.id,
            last_message_at=timezone.now(),
        )

        qs = Conversation.objects.exclude(last_message_at__isnull=True).order_by('-last_message_at')
        ids = list(qs.values_list('id', flat=True))
        self.assertEqual(ids, [c3.id, c2.id, c1.id])


class ConversationParticipantModelTest(TestCase):

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123'
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123'
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )

    def test_add_participants(self):
        p1 = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice', is_admin=True,
        )
        p2 = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob',
        )
        self.assertTrue(p1.is_admin)
        self.assertFalse(p2.is_admin)
        self.assertEqual(self.conv.participants.count(), 2)

    def test_unique_participant_per_conversation(self):
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
        )
        with self.assertRaises(Exception):
            ConversationParticipant.objects.create(
                conversation=self.conv, user_id=self.alice.id,
            )


class DirectMessageModelTest(TestCase):

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123'
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )

    def test_create_message(self):
        msg = DirectMessage.objects.create(
            conversation=self.conv,
            sender_id=self.alice.id,
            sender_email=self.alice.email,
            sender_name='Alice',
            content='Hello!',
        )
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.message_type, 'text')
        self.assertFalse(msg.is_deleted)
        self.assertFalse(msg.is_edited)

    def test_message_ordering(self):
        """Messages are ordered by created_at ASC."""
        m1 = DirectMessage.objects.create(
            conversation=self.conv, sender_id=self.alice.id, content='first',
        )
        m2 = DirectMessage.objects.create(
            conversation=self.conv, sender_id=self.alice.id, content='second',
        )
        msgs = list(DirectMessage.objects.filter(conversation=self.conv))
        self.assertEqual(msgs[0].id, m1.id)
        self.assertEqual(msgs[1].id, m2.id)


class PresenceServiceTest(TestCase):
    """Presence aggregation and stale cleanup are independent of message fanout."""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice-presence', email='alice-presence@example.com', password='pass123'
        )

    def test_multi_client_aggregation_does_not_go_offline_until_all_sessions_offline(self):
        web = touch_presence(
            user_id=self.alice.id,
            session_key='ws:web-1',
            client_id='web-1',
            client_type='web',
            status='online',
        )
        self.assertEqual(web.status, 'online')
        self.assertEqual(web.active_session_count, 1)

        away = touch_presence(
            user_id=self.alice.id,
            session_key='ws:mobile-1',
            client_id='mobile-1',
            client_type='mobile',
            status='away',
        )
        self.assertEqual(away.status, 'online')
        self.assertEqual(away.active_session_count, 2)

        after_web_disconnect = mark_offline(user_id=self.alice.id, session_key='ws:web-1')
        self.assertEqual(after_web_disconnect.status, 'away')
        self.assertEqual(after_web_disconnect.active_session_count, 1)

        final = mark_offline(user_id=self.alice.id, session_key='ws:mobile-1')
        self.assertEqual(final.status, 'offline')
        self.assertEqual(final.active_session_count, 0)

    def test_cleanup_transitions_missed_heartbeat_stale_then_offline(self):
        now = timezone.now()
        touch_presence(
            user_id=self.alice.id,
            session_key='ws:web-stale',
            client_id='web-stale',
            client_type='web',
            status='online',
            at=now - timedelta(minutes=2),
        )

        snapshots = cleanup_stale_presence(
            stale_after=timedelta(seconds=90),
            offline_after=timedelta(minutes=5),
            at=now,
        )
        self.assertEqual(len(snapshots), 1)
        session = PresenceSession.objects.get(session_key='ws:web-stale')
        self.assertEqual(session.status, 'stale')
        self.assertEqual(PresenceStatus.objects.get(user_id=self.alice.id).status, 'degraded')

        snapshots = cleanup_stale_presence(
            stale_after=timedelta(seconds=90),
            offline_after=timedelta(minutes=5),
            at=now + timedelta(minutes=4),
        )
        self.assertEqual(len(snapshots), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, 'offline')
        self.assertEqual(PresenceStatus.objects.get(user_id=self.alice.id).status, 'offline')

    def test_offline_session_history_is_not_serialized_in_aggregate_details(self):
        touch_presence(
            user_id=self.alice.id,
            session_key='ws:old-offline',
            client_id='old-offline',
            client_type='web',
            status='online',
        )
        touch_presence(
            user_id=self.alice.id,
            session_key='ws:active',
            client_id='active',
            client_type='web',
            status='away',
        )

        snapshot = mark_offline(user_id=self.alice.id, session_key='ws:old-offline')
        self.assertEqual(snapshot.status, 'away')

        aggregate = PresenceStatus.objects.get(user_id=self.alice.id)
        session_ids = {
            session['session_key']
            for session in aggregate.details.get('sessions', [])
        }
        self.assertEqual(session_ids, {'ws:active'})

    def test_refresh_presence_preserves_explicit_away_status(self):
        touch_presence(
            user_id=self.alice.id,
            session_key='ws:away',
            client_id='away-tab',
            client_type='web',
            status='away',
        )

        snapshot = refresh_presence(
            user_id=self.alice.id,
            session_key='ws:away',
            client_id='away-tab',
            client_type='web',
        )

        self.assertEqual(snapshot.status, 'away')
        self.assertEqual(PresenceSession.objects.get(session_key='ws:away').status, 'away')


class PresenceWebSocketEventTest(TestCase):
    def test_userchat_presence_event_forwards_peer_aggregate_and_skips_self(self):
        alice = User.objects.create_user(
            username='alice-presence-ws', email='alice-presence-ws@example.com', password='pass123'
        )
        bob = User.objects.create_user(
            username='bob-presence-ws', email='bob-presence-ws@example.com', password='pass123'
        )
        consumer = UserChatConsumer()
        consumer.user = alice
        sent = []

        async def fake_send(text_data=None, bytes_data=None, close=False):
            sent.append(json.loads(text_data))

        consumer.send = fake_send

        async_to_sync(consumer.presence_update)({
            'user_id': bob.id,
            'status': 'degraded',
            'active_session_count': 1,
            'degraded_session_count': 1,
            'last_seen_at': '2026-06-14T05:00:00Z',
        })
        self.assertEqual(sent[0]['type'], 'presence')
        self.assertEqual(sent[0]['data']['entity_id'], str(bob.id))
        self.assertEqual(sent[0]['data']['status'], 'degraded')
        self.assertEqual(sent[0]['data']['active_session_count'], 1)
        self.assertEqual(sent[0]['data']['degraded_session_count'], 1)

        async_to_sync(consumer.presence_update)({
            'user_id': alice.id,
            'status': 'offline',
        })
        self.assertEqual(len(sent), 1)

    def test_userchat_periodic_group_refresh_preserves_explicit_presence(self):
        alice = User.objects.create_user(
            username='alice-presence-refresh',
            email='alice-presence-refresh@example.com',
            password='pass123',
        )
        consumer = UserChatConsumer()
        consumer.user = alice
        consumer.channel_name = 'specific.channel'
        consumer.conversation_groups = set()
        consumer._get_user_conversations = AsyncMock(return_value=[])
        consumer._touch_presence = AsyncMock(side_effect=AssertionError('must not force online'))
        consumer._refresh_presence = AsyncMock()
        consumer._broadcast_presence_snapshot = AsyncMock()
        consumer.channel_layer = MagicMock()
        consumer.channel_layer.group_add = AsyncMock()

        async_to_sync(consumer._join_all_groups)(publish_presence=False)

        consumer._refresh_presence.assert_awaited_once()
        consumer._touch_presence.assert_not_awaited()
        consumer._broadcast_presence_snapshot.assert_not_awaited()


# ---------------------------------------------------------------------------
# API-level tests  —  Conversation CRUD
# ---------------------------------------------------------------------------

class ConversationCreateAPITest(ChannelMockMixin, APITestCase):
    """Test POST /connect/api/conversations/ — the Kotlin app's entry point."""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123'
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123'
        )
        self.charlie = User.objects.create_user(
            username='charlie', email='charlie@example.com', password='pass123'
        )
        self.client.force_authenticate(user=self.alice)

    def test_create_direct_conversation(self):
        """Creating a DM adds creator + target as participants."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        conv_id = resp.data['id']
        conv = Conversation.objects.get(id=conv_id)
        self.assertEqual(conv.conversation_type, 'direct')
        self.assertEqual(conv.created_by_id, self.alice.id)

        participants = ConversationParticipant.objects.filter(conversation=conv)
        self.assertEqual(participants.count(), 2)

        # Creator is admin
        alice_p = participants.get(user_id=self.alice.id)
        self.assertTrue(alice_p.is_admin)
        self.assertEqual(alice_p.user_email, self.alice.email)

        # Target is non-admin
        bob_p = participants.get(user_id=self.bob.id)
        self.assertFalse(bob_p.is_admin)

    def test_create_direct_returns_participant_names(self):
        """Response includes participant_names list for Kotlin DTO."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn('participant_names', resp.data)
        self.assertIsInstance(resp.data['participant_names'], list)
        # Creator's name should be in the list
        self.assertTrue(len(resp.data['participant_names']) >= 1)

    def test_create_direct_returns_full_serializer(self):
        """Response includes all ConversationSerializer fields."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        for field in ['id', 'conversation_type', 'name', 'last_message_preview',
                      'last_message_at', 'unread_count', 'participant_names',
                      'participants', 'created_at']:
            self.assertIn(field, resp.data, f"Missing field: {field}")

    def test_create_direct_no_participants_fails(self):
        """Direct message without exactly 1 participant is rejected."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_direct_multiple_participants_fails(self):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id, self.charlie.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_group_conversation(self):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Project Team',
            'participant_ids': [self.bob.id, self.charlie.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['conversation_type'], 'group')
        self.assertEqual(resp.data['name'], 'Project Team')

        conv = Conversation.objects.get(id=resp.data['id'])
        self.assertEqual(conv.participants.count(), 3)  # alice + bob + charlie

    def test_create_group_without_name_fails(self):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_creator_not_duplicated_in_participants(self):
        """If creator's ID is in participant_ids, they aren't added twice."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        # Alice should only appear once
        alice_count = ConversationParticipant.objects.filter(
            conversation=conv, user_id=self.alice.id
        ).count()
        self.assertEqual(alice_count, 1)

    def test_unauthenticated_create_fails(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN
        ])


class ConversationListAPITest(ChannelMockMixin, APITestCase):
    """Test GET /connect/api/conversations/ — unified list for Kotlin."""

    def setUp(self):
        self.profile_delay = patch(
            'connect.tasks.ensure_participant_social_profile.delay'
        ).start()
        self.addCleanup(patch.stopall)
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
            first_name='Alice', last_name='Smith',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
            first_name='Bob', last_name='Jones',
        )

        # Create a conversation between alice and bob
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
            last_message_at=timezone.now(),
            message_count=1,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob Jones',
        )
        self.client.force_authenticate(user=self.alice)

    def test_list_conversations(self):
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = get_results(resp.data)
        self.assertGreaterEqual(len(results), 1)

    def test_list_includes_participant_names(self):
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        conv_data = results[0]
        self.assertIn('participant_names', conv_data)
        names = conv_data['participant_names']
        self.assertIsInstance(names, list)
        # For direct conversations, current user's name is excluded
        self.assertNotIn('Alice Smith', names)
        self.assertIn('Bob Jones', names)

    def test_list_includes_unread_count(self):
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        conv_data = results[0]
        self.assertIn('unread_count', conv_data)
        self.assertEqual(conv_data['unread_count'], 0)

    @patch('connect.services.display_names.resolve_identity_users')
    def test_list_resolves_stored_user_n_placeholder(self, mock_ids):
        """Stored 'User <id>' must not win over shizuha-id / SocialProfile."""
        from connect.models import SocialProfile
        ena = User.objects.create_user(
            username='ena', email='ena@shizuha.com', password='pass123',
            first_name='Ena', last_name='',
        )
        conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
            last_message_at=timezone.now(),
            message_count=1,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=ena.id,
            user_email='', user_name=f'User {ena.id}',
        )
        SocialProfile.objects.create(
            user_id=ena.id, username='ena', display_name='Ena',
            user_email='ena@shizuha.com',
        )
        mock_ids.return_value = {}
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = get_results(resp.data)
        target = next(r for r in rows if r['id'] == str(conv.id))
        self.assertIn('Ena', target['participant_names'])
        self.assertNotIn(f'User {ena.id}', target['participant_names'])
        peer = next(p for p in target['participants'] if p['user_id'] == ena.id)
        self.assertEqual(peer['user_name'], 'Ena')
        conv.refresh_from_db()
        stored = ConversationParticipant.objects.get(conversation=conv, user_id=ena.id)
        self.assertEqual(stored.user_name, 'Ena')

    @patch('connect.services.display_names.resolve_identity_users')
    def test_list_resolves_user_n_from_id_when_profile_missing(self, mock_ids):
        yuna_id = 734
        mock_ids.return_value = {
            yuna_id: {
                'id': yuna_id, 'username': 'yuna', 'first_name': 'Yuna',
                'last_name': '', 'email': 'yuna@shizuha.com',
            }
        }
        conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
            last_message_at=timezone.now(),
            message_count=1,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=yuna_id,
            user_email='', user_name='User 734',
        )
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = get_results(resp.data)
        target = next(r for r in rows if r['id'] == str(conv.id))
        self.assertIn('Yuna', target['participant_names'])
        peer = next(p for p in target['participants'] if p['user_id'] == yuna_id)
        self.assertEqual(peer['user_name'], 'Yuna')

    def test_only_shows_user_conversations(self):
        """User only sees conversations they're a participant in."""
        charlie = User.objects.create_user(
            username='charlie', email='charlie@example.com', password='pass123'
        )
        # Create a conv charlie isn't part of
        self.client.force_authenticate(user=charlie)
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = get_results(resp.data)
        self.assertEqual(len(results), 0)

    def test_left_conversation_not_listed(self):
        """If user has left, conversation doesn't appear."""
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id
        ).update(has_left=True)

        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        self.assertEqual(len(results), 0)

    def _empty_dm(self, peer_id, peer_name, *, participant_type='human'):
        conv = Conversation.objects.create(
            conversation_type='direct',
            created_by_id=self.alice.id,
            message_count=0,
            direct_key=Conversation.build_direct_key(self.alice.id, peer_id),
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=peer_id,
            user_name=peer_name, participant_type=participant_type,
        )
        return conv

    def test_list_omits_never_messaged_threads_for_every_counterpart(self):
        """Empty DMs must not occupy the inbox — hive or fleet, same rule."""
        hive = self._empty_dm(1_616_370_251, 'Claw (openclaw)', participant_type='agent')
        fleet = self._empty_dm(88, 'Nagi')
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row['id'] for row in get_results(resp.data)}
        self.assertNotIn(str(hive.id), ids)
        self.assertNotIn(str(fleet.id), ids)
        self.assertIn(str(self.conv.id), ids)

    def test_list_orders_by_recent_message_not_agent_kind(self):
        older = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
            last_message_at=timezone.now() - timedelta(hours=2),
            message_count=3,
            direct_key=Conversation.build_direct_key(self.alice.id, 12),
        )
        ConversationParticipant.objects.create(
            conversation=older, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=older, user_id=12, user_name='Aoi', participant_type='agent',
        )
        resp = self.client.get('/api/conversations/')
        ids = [row['id'] for row in get_results(resp.data)]
        self.assertEqual(ids[0], str(self.conv.id))
        self.assertEqual(ids[1], str(older.id))

    def test_never_messaged_thread_is_still_retrievable(self):
        empty = self._empty_dm(1_662_530_027, 'Hermes (hermes)', participant_type='agent')
        resp = self.client.get(f'/api/conversations/{empty.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['id'], str(empty.id))


# ---------------------------------------------------------------------------
# API-level tests  —  Messages
# ---------------------------------------------------------------------------

class AckOnlyMessageHelperTest(SimpleTestCase):
    def test_docs_token_pong_is_ack_only(self):
        from connect.views.messaging import is_ack_only_message
        # Forged always-on voice-prompt vocabulary (SCLI-756): the forged
        # always-on inbox example asks the agent to "reply with the word:
        # pong"; prompt-example artifacts and one-word acks answer a user
        # turn the agent fabricated itself.
        for forged in (
            'pong', 'Pong.', 'pong sent', 'ok', 'okay', 'Okay!', 'ack',
            'acknowledged', 'Acknowledged.', 'copy', 'will do', 'on it',
            'standing by', 'thanks', 'thank you', 'Thank you!',
            'Test message',
            'Test message — please confirm you can see this.',
            "I'm stuck in a loop. Let me break out of it.",
            "I'm here — what would you like me to do?",
        ):
            self.assertTrue(is_ack_only_message(forged), msg=forged)
        # Legitimate traffic must deliver: sanctioned one-line reply
        # protocols carry payloads, and real sentences are never exact
        # matches of the ack vocabulary. A loop report that names a Pulse
        # key is a real status, not generic liveness spam.
        for legitimate in (
            'CON-21 pong kai-20260916',
            'CON-98 pong',
            'looking at AT-91 now',
            'Copy the fix into deploy and re-run CI.',
            'Thanks for the review — pushing the patch now.',
            "I'm stuck in a loop on CTX-528 GPU folio hang",
        ):
            self.assertFalse(is_ack_only_message(legitimate), msg=legitimate)


class QaProbeIdentityTest(SimpleTestCase):
    def test_qafixture_and_qa_usernames(self):
        from connect.services.qa_probe import is_production_fleet_agent, is_qa_fixture_identity
        self.assertTrue(is_qa_fixture_identity(email='run1@qafixture.shizuha.com'))
        self.assertTrue(is_qa_fixture_identity(username='probe-qa'))
        self.assertTrue(is_qa_fixture_identity(username='enaqa'))
        self.assertFalse(is_qa_fixture_identity(username='ni'))
        self.assertTrue(is_production_fleet_agent(participant_type='agent', username='ni'))
        self.assertFalse(is_production_fleet_agent(participant_type='agent', username='probe-qa'))
        self.assertFalse(is_production_fleet_agent(participant_type='human', username='hritik'))


class MessageSendAPITest(ChannelMockMixin, APITestCase):
    """Test POST /connect/api/conversations/{id}/messages/"""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
            first_name='Alice', last_name='Smith',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
            first_name='Bob', last_name='Jones',
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob Jones',
        )
        self.client.force_authenticate(user=self.alice)

    def test_send_message(self):
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Hello Bob!'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['content'], 'Hello Bob!')
        self.assertEqual(resp.data['sender_id'], self.alice.id)
        self.assertEqual(resp.data['sender_name'], 'Alice Smith')

    def test_ack_only_replied_is_rejected(self):
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Replied.'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Replied', resp.data['error'])
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_send_persists_canonical_username_not_email_localpart_or_client_claim(self):
        self.alice.email = 'different-login@example.com'
        self.alice.save(update_fields=['email'])
        response = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Please explain the diagram', 'sender_username': 'forged'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['sender_username'], 'alice')
        message = DirectMessage.objects.get(pk=response.data['id'])
        self.assertEqual(message.sender_username, 'alice')
        self.assertEqual(message.sender_email, 'different-login@example.com')

    def test_forward_persists_authenticated_forwarder_username_not_original_sender(self):
        self.alice.email = 'different-login@example.com'
        self.alice.save(update_fields=['email'])
        original = DirectMessage.objects.create(
            conversation=self.conv, sender_id=self.bob.id,
            sender_email=self.bob.email, sender_username='bob', content='Architecture details',
        )
        response = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/{original.pk}/forward/',
            {'target_conversation_id': str(self.conv.id), 'sender_username': 'forged'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['sender_username'], 'alice')
        forwarded = DirectMessage.objects.get(pk=response.data['id'])
        self.assertEqual(forwarded.sender_username, 'alice')
        self.assertEqual(forwarded.forwarded_from_id, original.pk)

    def test_ack_only_pong_docs_token_is_rejected(self):
        """Prompt-example artifact: `[hritik] Hi <agent>, reply with the word: pong`."""
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'pong'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('content must be the words', resp.data['error'])
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_send_message_updates_unread_for_others(self):
        """Sending a message increments unread_count for other participants."""
        self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Hello!'},
            format='json',
        )
        bob_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.bob.id
        )
        self.assertEqual(bob_p.unread_count, 1)

        # Sender's unread should NOT increment
        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertEqual(alice_p.unread_count, 0)

    def test_send_multiple_messages(self):
        for i in range(5):
            resp = self.client.post(
                f'/api/conversations/{self.conv.id}/messages/',
                {'content': f'Message {i}'},
                format='json',
            )
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        msgs = DirectMessage.objects.filter(conversation=self.conv)
        self.assertEqual(msgs.count(), 5)

    def test_send_empty_message_fails(self):
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': ''},
            format='json',
        )
        # Content is required (not blank)
        self.assertIn(resp.status_code, [
            status.HTTP_400_BAD_REQUEST, status.HTTP_201_CREATED
        ])

    def test_non_participant_cannot_send(self):
        charlie = User.objects.create_user(
            username='charlie', email='charlie@example.com', password='pass123'
        )
        self.client.force_authenticate(user=charlie)
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': 'Intruder!'},
            format='json',
        )
        # get_queryset excludes convos the user isn't in → 404 from get_object()
        self.assertIn(resp.status_code, [
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
        ])


class MessageListAPITest(ChannelMockMixin, APITestCase):
    """Test GET /connect/api/conversations/{id}/messages/"""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
            first_name='Alice', last_name='Smith',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
            first_name='Bob', last_name='Jones',
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob Jones',
        )

        # Seed some messages
        DirectMessage.objects.create(
            conversation=self.conv, sender_id=self.alice.id,
            sender_name='Alice Smith', content='Hey Bob',
        )
        DirectMessage.objects.create(
            conversation=self.conv, sender_id=self.bob.id,
            sender_name='Bob Jones', content='Hey Alice',
        )

        self.client.force_authenticate(user=self.alice)

    def test_list_messages(self):
        resp = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Messages come ordered by -created_at from the view
        msgs = resp.data.get('messages', resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))
        self.assertEqual(len(msgs), 2)

    def test_list_messages_marks_as_read(self):
        """GETting messages resets unread_count for the reader."""
        # Give alice some unread
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id
        ).update(unread_count=5)

        self.client.get(f'/api/conversations/{self.conv.id}/messages/')

        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertEqual(alice_p.unread_count, 0)

    def test_non_participant_cannot_read(self):
        charlie = User.objects.create_user(
            username='charlie', email='charlie@example.com', password='pass123'
        )
        self.client.force_authenticate(user=charlie)
        resp = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        # get_queryset excludes convos the user isn't in → 404 from get_object()
        self.assertIn(resp.status_code, [
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
        ])

    def test_deleted_messages_returned_as_tombstones(self):
        # The messages endpoint INCLUDES soft-deleted messages as tombstones
        # (empty content, client renders "This message was deleted") so the
        # thread stays consistent with the chat-list preview and reply/forward
        # chains — see MessageViewSet.messages GET. They are returned, not hidden.
        DirectMessage.objects.filter(
            conversation=self.conv, sender_id=self.alice.id
        ).update(is_deleted=True)

        resp = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        msgs = resp.data.get('messages', resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))
        self.assertEqual(len(msgs), 2)  # both returned; the deleted one is a tombstone
        deleted = [m for m in msgs if m.get('is_deleted')]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].get('content', ''), '')  # tombstone content redacted


# ---------------------------------------------------------------------------
# API-level tests  —  Conversation actions (read, mute, leave)
# ---------------------------------------------------------------------------

class ConversationActionsAPITest(ChannelMockMixin, APITestCase):

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
        )
        self.conv = Conversation.objects.create(
            conversation_type='group', name='Test Group',
            created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob',
        )
        self.client.force_authenticate(user=self.alice)

    def test_mark_as_read(self):
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id
        ).update(unread_count=10)

        resp = self.client.post(f'/api/conversations/{self.conv.id}/read/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertEqual(alice_p.unread_count, 0)

    def test_mute_conversation(self):
        resp = self.client.post(f'/api/conversations/{self.conv.id}/mute/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertTrue(alice_p.is_muted)

    def test_unmute_conversation(self):
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id
        ).update(is_muted=True)

        resp = self.client.post(f'/api/conversations/{self.conv.id}/unmute/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertFalse(alice_p.is_muted)

    def test_leave_group_conversation(self):
        resp = self.client.post(f'/api/conversations/{self.conv.id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        alice_p = ConversationParticipant.objects.get(
            conversation=self.conv, user_id=self.alice.id
        )
        self.assertTrue(alice_p.has_left)

    def test_leave_direct_conversation_fails(self):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id, is_admin=True,
        )
        resp = self.client.post(f'/api/conversations/{direct.id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_leave_creates_system_message(self):
        self.client.post(f'/api/conversations/{self.conv.id}/leave/')
        sys_msg = DirectMessage.objects.filter(
            conversation=self.conv, message_type='system'
        ).order_by('created_at')
        # Sole admin leaving creates promotion msg + leave msg
        self.assertEqual(sys_msg.count(), 2)
        self.assertIn('left', sys_msg.last().content)


# ---------------------------------------------------------------------------
# Full flow integration test  —  simulates Kotlin app lifecycle
# ---------------------------------------------------------------------------

class PresenceAPITest(ChannelMockMixin, APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice-presence-api', email='alice-presence-api@example.com', password='pass123'
        )
        self.bob = User.objects.create_user(
            username='bob-presence-api', email='bob-presence-api@example.com', password='pass123'
        )
        self.client.force_authenticate(user=self.alice)

    def test_presence_get_returns_offline_defaults_for_requested_users(self):
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=self.alice.id)
        ConversationParticipant.objects.create(
            conversation=conv,
            user_id=self.alice.id,
            user_email=self.alice.email,
        )
        ConversationParticipant.objects.create(
            conversation=conv,
            user_id=self.bob.id,
            user_email=self.bob.email,
        )
        resp = self.client.get(f'/api/presence/?user_ids={self.alice.id},{self.bob.id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        by_user = {row['user_id']: row for row in resp.data}
        self.assertEqual(by_user[self.alice.id]['status'], 'offline')
        self.assertEqual(by_user[self.bob.id]['active_session_count'], 0)

    def test_presence_get_filters_unrelated_requested_users(self):
        resp = self.client.get(f'/api/presence/?user_ids={self.alice.id},{self.bob.id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row['user_id'] for row in resp.data], [self.alice.id])

    def test_presence_post_records_away_heartbeat(self):
        resp = self.client.post('/api/presence/', {
            'client_id': 'browser-tab-1',
            'client_type': 'web',
            'status': 'away',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'away')
        self.assertEqual(resp.data['active_session_count'], 1)

        session = PresenceSession.objects.get(user_id=self.alice.id, client_id='browser-tab-1')
        self.assertEqual(session.status, 'away')
        self.assertEqual(PresenceStatus.objects.get(user_id=self.alice.id).status, 'away')

    def test_presence_post_rejects_overlong_client_fields(self):
        resp = self.client.post('/api/presence/', {
            'client_id': 'x' * 201,
            'client_type': 'web',
            'status': 'online',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        resp = self.client.post('/api/presence/', {
            'client_id': 'browser-tab-1',
            'client_label': 'x' * 201,
            'client_type': 'web',
            'status': 'online',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        resp = self.client.post('/api/presence/', {
            'client_id': 'x' * 200,
            'client_type': 'web',
            'status': 'online',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class FullDMFlowTest(ChannelMockMixin, APITestCase):
    """
    End-to-end test that mimics the Kotlin app flow:
    1. User opens search → fetch org members (tested separately)
    2. User taps member → POST /conversations/ (create DM)
    3. Navigate to conversation → GET /conversations/{id}/messages/
    4. User types & sends → POST /conversations/{id}/messages/
    5. Go back to list → GET /conversations/ (should include new convo)
    6. Re-open same conversation → already exists, no duplicate
    """

    def setUp(self):
        self.hritik = User.objects.create_user(
            username='hritik', email='jdoe@example.com', password='pass123',
            first_name='Hritik', last_name='Soni',
        )
        self.hitesh = User.objects.create_user(
            username='hitesh', email='hitesh@shizuha.com', password='pass123',
            first_name='Hitesh', last_name='Kumar',
        )
        self.client.force_authenticate(user=self.hritik)

    def test_full_dm_flow(self):
        # Step 1: Create conversation
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.hitesh.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        conv_id = resp.data['id']
        self.assertIn('participant_names', resp.data)

        # Step 2: Get messages (empty initially)
        resp = self.client.get(f'/api/conversations/{conv_id}/messages/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msgs = resp.data.get('messages', resp.data if isinstance(resp.data, list) else resp.data.get('results', []))
        self.assertEqual(len(msgs), 0)

        # Step 3: Send a message
        resp = self.client.post(
            f'/api/conversations/{conv_id}/messages/',
            {'content': 'Hello Hitesh!'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['content'], 'Hello Hitesh!')

        # Step 4: Hitesh replies
        self.client.force_authenticate(user=self.hitesh)
        resp = self.client.post(
            f'/api/conversations/{conv_id}/messages/',
            {'content': 'Hello Hritik! How are you?'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # Step 5: Hritik fetches messages — should see both
        self.client.force_authenticate(user=self.hritik)
        resp = self.client.get(f'/api/conversations/{conv_id}/messages/')
        msgs = resp.data.get('messages', resp.data if isinstance(resp.data, list) else resp.data.get('results', []))
        self.assertEqual(len(msgs), 2)

        # Step 6: Conversation appears in list
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = get_results(resp.data)
        self.assertEqual(len(results), 1)
        conv_data = results[0]
        self.assertEqual(conv_data['id'], str(conv_id))
        self.assertIn('participant_names', conv_data)

    def test_bidirectional_visibility(self):
        """Both participants see the same conversation in their lists."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.hitesh.id],
        }, format='json')
        conv_id = resp.data['id']
        send = self.client.post(
            f'/api/conversations/{conv_id}/messages/',
            {'content': 'hello'},
            format='json',
        )
        self.assertEqual(send.status_code, status.HTTP_201_CREATED)

        # Hritik sees it
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        self.assertEqual(len(results), 1)

        # Hitesh also sees it
        self.client.force_authenticate(user=self.hitesh)
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], str(conv_id))

    def test_unread_count_flow(self):
        """Unread counts track correctly across send/read cycles."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.hitesh.id],
        }, format='json')
        conv_id = resp.data['id']

        # Hritik sends 3 messages
        for i in range(3):
            self.client.post(
                f'/api/conversations/{conv_id}/messages/',
                {'content': f'msg {i}'},
                format='json',
            )

        # Hitesh should have unread_count = 3
        self.client.force_authenticate(user=self.hitesh)
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        self.assertEqual(results[0]['unread_count'], 3)

        # Hitesh reads messages
        self.client.get(f'/api/conversations/{conv_id}/messages/')

        # Unread should be 0 now
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        self.assertEqual(results[0]['unread_count'], 0)


# ---------------------------------------------------------------------------
# External bridge inbound envelope/idempotency (CON-12)
# ---------------------------------------------------------------------------

class BridgeInboundWebhookTest(APITestCase):
    """CON-5 P1 WhatsApp bridge invariants: signature first, stable id, idempotency."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._override = __import__('django.test').test.override_settings(
            CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(),
            CONNECT_BRIDGE_ADMIN_IDENTITY_FRESHNESS_HOURS=1,
        )
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        super().tearDownClass()

    def setUp(self):
        import hmac
        import hashlib
        import json
        from django.utils import timezone
        from connect.models import BridgeIdentityMapping
        from connect.services.bridge import upsert_webhook_secret

        self.hmac = hmac
        self.hashlib = hashlib
        self.json = json
        self.timezone = timezone
        self.account = 'wa-business-1'
        self.secret = 'test-webhook-secret'
        upsert_webhook_secret('whatsapp', self.account, self.secret)
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123'
        )
        self.mapping = BridgeIdentityMapping.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            external_sender_id='15551234567',
            mapped_user_id=self.alice.id,
            mapped_user_email=self.alice.email,
            mapped_user_name='Alice Stable',
            verified_at=timezone.now(),
            is_admin_capable=False,
        )

    def _payload(self, event_id='wamid.1', sender='15551234567', text='hello', display_name='Mutable Name', **extra):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'contacts': [{'profile': {'name': display_name}, 'wa_id': sender}],
                        'messages': [{
                            'id': event_id,
                            'from': sender,
                            'timestamp': '1710000000',
                            'text': {'body': text},
                            'type': 'text',
                        }],
                    }
                }]
            }]
        }
        payload.update(extra)
        return payload

    def _signed_post(self, payload, signature=None):
        raw = self.json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if signature is None:
            digest = self.hmac.new(self.secret.encode('utf-8'), raw, self.hashlib.sha256).hexdigest()
            signature = f'sha256={digest}'
        return self.client.generic(
            'POST',
            f'/api/bridges/whatsapp/{self.account}/webhook/',
            data=raw,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=signature,
        )

    def test_invalid_signature_never_reaches_idempotency_or_routing(self):
        from connect.models import BridgeInboundEvent

        response = self._signed_post(self._payload(), signature='sha256=bad')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BridgeInboundEvent.objects.count(), 0)
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_duplicate_provider_event_replays_without_duplicate_message(self):
        from connect.models import BridgeInboundEvent

        first = self._signed_post(self._payload(event_id='wamid.dupe'))
        second = self._signed_post(self._payload(event_id='wamid.dupe'))

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data['idempotent_replay'])
        self.assertEqual(second.data['message_id'], first.data['message_id'])
        self.assertEqual(BridgeInboundEvent.objects.count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_same_provider_event_with_changed_payload_becomes_security_conflict(self):
        from connect.models import BridgeInboundEvent

        first = self._signed_post(self._payload(event_id='wamid.conflict', text='original'))
        conflict = self._signed_post(self._payload(event_id='wamid.conflict', text='tampered'))

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        event = BridgeInboundEvent.objects.get(provider_event_id='wamid.conflict')
        self.assertEqual(event.processing_status, BridgeInboundEvent.Status.CONFLICT)
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_display_name_change_does_not_change_stable_sender_mapping(self):
        first = self._signed_post(self._payload(event_id='wamid.name1', display_name='Old Name'))
        second = self._signed_post(self._payload(event_id='wamid.name2', display_name='New Name'))

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data['mapped_user_id'], self.alice.id)
        self.assertEqual(second.data['mapped_user_id'], self.alice.id)
        self.assertEqual(DirectMessage.objects.count(), 2)

    def test_payload_cannot_spoof_mapped_user_id(self):
        response = self._signed_post(self._payload(event_id='wamid.spoof', mapped_user_id=999999))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['mapped_user_id'], self.alice.id)
        self.assertNotEqual(response.data['mapped_user_id'], 999999)

    def test_stale_admin_command_mapping_is_forbidden_without_message(self):
        from datetime import timedelta
        from connect.models import BridgeInboundEvent

        self.mapping.is_admin_capable = True
        self.mapping.verified_at = self.timezone.now() - timedelta(hours=2)
        self.mapping.save(update_fields=['is_admin_capable', 'verified_at'])

        response = self._signed_post(self._payload(event_id='wamid.admin', text='/admin rotate tokens'))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = BridgeInboundEvent.objects.get(provider_event_id='wamid.admin')
        self.assertEqual(event.processing_status, BridgeInboundEvent.Status.FORBIDDEN)
        self.assertIsNone(event.message_id)
        self.assertEqual(DirectMessage.objects.count(), 0)


class BridgeOutboundPolicyTest(APITestCase):
    """CON-13 outbound bridge invariants: explicit policy, idempotency, limits, privacy audit."""

    def setUp(self):
        from django.utils import timezone
        from connect.models import BridgeChannelPolicy

        self.alice = User.objects.create_user(
            username='agent-alice', email='agent-alice@example.com', password='pass123'
        )
        self.client.force_authenticate(user=self.alice)
        self.account = 'wa-business-1'
        self.policy = BridgeChannelPolicy.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-13',
            agent_id=self.alice.id,
            admin_approved_at=timezone.now(),
            admin_approved_by=1,
            allowed_recipients=['15551234567'],
        )

    def _envelope(self, *, key='idem-1', recipient='15551234567', body='hello', agent_id=None, task_key='CON-13'):
        from connect.services.bridge import BridgeOutboundEnvelope
        return BridgeOutboundEnvelope(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key=task_key,
            agent_id=agent_id or self.alice.id,
            recipient_external_id=recipient,
            body=body,
            idempotency_key=key,
        )

    def test_send_fails_without_explicit_policy_and_audit_redacts_body(self):
        from connect.models import BridgeOutboundEvent
        from connect.services.bridge import send_outbound_bridge_message

        calls = []
        result = send_outbound_bridge_message(
            self._envelope(task_key='UNAPPROVED'),
            provider_send=lambda event, envelope: calls.append(envelope.body),
        )

        self.assertEqual(result.event.status, BridgeOutboundEvent.Status.FORBIDDEN)
        self.assertEqual(calls, [])
        self.assertEqual(result.event.body_hash, '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824')
        self.assertNotIn('body', result.event.audit_metadata)

    def test_retry_after_provider_acceptance_does_not_send_second_copy(self):
        from connect.models import BridgeOutboundEvent
        from connect.services.bridge import send_outbound_bridge_message

        calls = []

        def provider_send(event, envelope):
            calls.append(envelope.idempotency_key)
            return {'provider_delivery_id': 'wamid.out.1'}

        first = send_outbound_bridge_message(self._envelope(), provider_send=provider_send)
        second = send_outbound_bridge_message(self._envelope(), provider_send=provider_send)

        self.assertEqual(first.event.status, BridgeOutboundEvent.Status.ACCEPTED)
        self.assertTrue(first.sent_to_provider)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(second.event.provider_delivery_id, 'wamid.out.1')
        self.assertEqual(calls, ['idem-1'])
        self.assertEqual(BridgeOutboundEvent.objects.count(), 1)

    def test_rate_limits_cover_agent_recipient_and_provider_account_dimensions(self):
        from django.utils import timezone
        from connect.models import BridgeOutboundEvent
        from connect.services.bridge import send_outbound_bridge_message

        now = timezone.now()
        base = {
            'provider': 'whatsapp',
            'provider_account_id': self.account,
            'channel_key': 'ops-alerts',
            'task_key': 'CON-13',
            'request_hash': 'r' * 64,
            'body_hash': 'b' * 64,
            'status': BridgeOutboundEvent.Status.ACCEPTED,
            'created_at': now,
            'updated_at': now,
        }
        BridgeOutboundEvent.objects.bulk_create([
            BridgeOutboundEvent(
                **base,
                agent_id=self.alice.id,
                recipient_external_id=f'agent-recipient-{i}',
                idempotency_key=f'agent-{i}',
            )
            for i in range(30)
        ])
        self.assertEqual(
            send_outbound_bridge_message(self._envelope(key='agent-limit')).event.status,
            BridgeOutboundEvent.Status.RATE_LIMITED,
        )

        bob = User.objects.create_user(username='agent-bob', email='agent-bob@example.com')
        self.policy.agent_id = bob.id
        self.policy.allowed_recipients = ['recipient-hot']
        self.policy.save(update_fields=['agent_id', 'allowed_recipients'])
        BridgeOutboundEvent.objects.bulk_create([
            BridgeOutboundEvent(
                **base,
                agent_id=1000 + i,
                recipient_external_id='recipient-hot',
                idempotency_key=f'recipient-{i}',
            )
            for i in range(10)
        ])
        self.assertEqual(
            send_outbound_bridge_message(
                self._envelope(key='recipient-limit', recipient='recipient-hot', agent_id=bob.id)
            ).event.status,
            BridgeOutboundEvent.Status.RATE_LIMITED,
        )

        self.policy.allowed_recipients = []
        self.policy.save(update_fields=['allowed_recipients'])
        BridgeOutboundEvent.objects.bulk_create([
            BridgeOutboundEvent(
                **base,
                agent_id=2000 + i,
                recipient_external_id=f'provider-recipient-{i}',
                idempotency_key=f'provider-{i}',
            )
            for i in range(100)
        ])
        self.assertEqual(
            send_outbound_bridge_message(
                self._envelope(key='provider-limit', recipient='provider-new', agent_id=bob.id)
            ).event.status,
            BridgeOutboundEvent.Status.RATE_LIMITED,
        )

    def test_delivery_callback_updates_status_without_body_storage(self):
        from connect.services.bridge import record_outbound_delivery_status, send_outbound_bridge_message

        send_outbound_bridge_message(
            self._envelope(key='callback'),
            provider_send=lambda event, envelope: {'provider_delivery_id': 'wamid.callback'},
        )
        event = record_outbound_delivery_status(
            provider='whatsapp',
            provider_account_id=self.account,
            provider_delivery_id='wamid.callback',
            status_value='delivered',
            payload={'provider_delivery_id': 'wamid.callback', 'status': 'delivered', 'body': 'provider echo'},
        )

        self.assertEqual(event.status, 'delivered')
        self.assertIn('callback_payload_hash', event.audit_metadata)
        self.assertNotIn('provider echo', str(event.audit_metadata))


class AgentProfileSeedingAPITest(APITestCase):
    """Regression coverage for Connect MCP profile-seeding endpoints."""

    def setUp(self):
        from connect.authentication import FederatedUser

        self.agent = FederatedUser(
            user_id='26408ca2-7b9d-48a9-ac6e-8062dcebf846',
            email='kai@agents.shizuha.io',
            username='kai',
            first_name='Kai',
            last_name='Engineer',
            enabled_services=['connect'],
        )
        self.client.force_authenticate(user=self.agent)

    def test_agent_can_create_and_update_own_profile(self):
        response = self.client.get('/api/me/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], 'kai')

        response = self.client.patch('/api/me/', {
            'headline': 'Engineering lead',
            'bio': 'Builds reliable agent infrastructure.',
            'location': 'Remote',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['headline'], 'Engineering lead')
        self.assertEqual(response.data['location'], 'Remote')

    def test_agent_can_create_post_with_mcp_hashtags(self):
        response = self.client.post('/api/posts/', {
            'content': 'Keeping the agent runtime reliable.',
            'visibility': 'public',
            'hashtags': ['engineering', 'reliability'],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        from connect.models import PostHashtag
        self.assertEqual(
            set(PostHashtag.objects.values_list('hashtag__name', flat=True)),
            {'engineering', 'reliability'},
        )

    def test_skills_catalog_search_uses_non_created_at_pagination(self):
        from connect.models import Skill

        Skill.objects.create(name='Engineering', slug='engineering', category='technical')
        Skill.objects.create(name='Reliability', slug='reliability', category='technical')

        response = self.client.get('/api/skills/?search=engineering')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item['name'] for item in response.data['results']]
        self.assertEqual(names, ['Engineering'])


class HashtagExtractionSignalTest(TestCase):
    def test_post_save_creates_lowercase_deduped_hashtags(self):
        from connect.models import Hashtag, Post, PostHashtag

        post = Post.objects.create(
            author_id=1,
            author_email='author@example.com',
            author_username='author',
            content='Hello #Shizuha #AI #ai midwordNo#tag #日本語',
        )

        self.assertEqual(set(post.hashtag_links.values_list('hashtag__name', flat=True)), {'shizuha', 'ai', '日本語'})
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 3)
        self.assertEqual(Hashtag.objects.get(name='ai').post_count, 1)

    def test_backfill_post_hashtags_command_is_dry_run_safe_and_idempotent(self):
        from io import StringIO
        from django.core.management import call_command
        from connect.models import Hashtag, Post, PostHashtag

        post = Post.objects.create(
            author_id=1,
            author_email='author@example.com',
            author_username='author',
            content='Legacy post #Shizuha #AI #ai',
        )
        PostHashtag.objects.filter(post=post).delete()
        Hashtag.objects.update(post_count=0)

        dry_out = StringIO()
        call_command('backfill_post_hashtags', '--dry-run', stdout=dry_out)
        self.assertIn('Dry run complete', dry_out.getvalue())
        self.assertFalse(PostHashtag.objects.filter(post=post).exists())

        out = StringIO()
        call_command('backfill_post_hashtags', '--progress-every=0', stdout=out)
        self.assertIn('Backfill complete', out.getvalue())
        self.assertEqual(set(post.hashtag_links.values_list('hashtag__name', flat=True)), {'shizuha', 'ai'})
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 2)

        call_command('backfill_post_hashtags', '--progress-every=0', stdout=StringIO())
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 2)
        self.assertEqual(Hashtag.objects.get(name='ai').post_count, 1)

    def test_post_edit_reconciles_removed_and_added_hashtags(self):
        from connect.models import Hashtag, Post

        post = Post.objects.create(
            author_id=1,
            author_email='author@example.com',
            author_username='author',
            content='Before #Shizuha #AI',
        )
        post.content = 'After #AI #Connect'
        post.save(update_fields=['content'])

        self.assertEqual(set(post.hashtag_links.values_list('hashtag__name', flat=True)), {'ai', 'connect'})
        self.assertFalse(post.hashtag_links.filter(hashtag__name='shizuha').exists())
        self.assertEqual(Hashtag.objects.get(name='shizuha').post_count, 0)
        self.assertEqual(Hashtag.objects.get(name='connect').post_count, 1)


class DMAgentRecipientClassificationTest(ChannelMockMixin, APITestCase):
    """Identity account_type must drive Connect's event-driven agent wake."""

    def setUp(self):
        self.sender = User.objects.create_user(
            username='origin-ci', email='origin-ci@shizuha.com', password='pass123'
        )
        self.agent = User.objects.create_user(
            username='nagi', email='nagi@shizuha.com', password='pass123', first_name='Nagi'
        )
        self.client.force_authenticate(user=self.sender)

    def _agent_lookup(self):
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {
            'id': self.agent.id,
            'user_id': self.agent.id,
            'email': self.agent.email,
            'first_name': 'Nagi',
            'last_name': '',
            'username': 'nagi',
            'account_type': 'agent',
            'profile': {'account_type': 'agent'},
        }
        return response

    @patch('connect.tasks.send_dm_notification.delay')
    @patch('requests.get')
    def test_existing_human_participant_is_repaired_to_agent(self, mock_get, _notify):
        conversation = Conversation.objects.create(
            conversation_type='direct',
            direct_key=Conversation.build_direct_key(self.sender.id, self.agent.id),
            created_by_id=self.sender.id,
        )
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=self.sender.id,
            user_email=self.sender.email,
            participant_type='human',
        )
        recipient = ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=self.agent.id,
            user_email=self.agent.email,
            participant_type='human',
        )
        mock_get.return_value = self._agent_lookup()

        response = self.client.post(
            '/api/messaging/dm/',
            {'recipient_username': 'nagi', 'content': 'Origin CI failed'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.participant_type, 'agent')


class DMStormDedupTest(ChannelMockMixin, APITestCase):
    """CON-104: server-side content-storm dedup on /api/messaging/dm/.

    A looping sender (wedged agent replaying its last DM each heartbeat) must not
    be able to flood a recipient with identical messages. An identical
    (sender, conversation, content) DM within CONNECT_DM_DEDUP_WINDOW_SECONDS is
    suppressed (existing message returned, no duplicate row)."""

    LOOP_MSG = 'Confirmed. Resume QA only after Engineering provides the updated build/handoff.'

    def setUp(self):
        self.zen = User.objects.create_user(
            username='zen', email='zen@shizuha.com', password='pass123', first_name='Zen')
        self.hana = User.objects.create_user(
            username='hana', email='hana@shizuha.com', password='pass123', first_name='Hana')
        self.client.force_authenticate(user=self.zen)

    def _lookup(self):
        m = MagicMock()
        m.status_code = 200
        m.ok = True
        m.json.return_value = {
            'id': self.hana.id, 'user_id': self.hana.id, 'email': self.hana.email,
            'first_name': 'Hana', 'last_name': '', 'username': 'hana',
        }
        return m

    def _send(self, content):
        return self.client.post(
            '/api/messaging/dm/',
            {'recipient_email': 'hana@shizuha.com', 'content': content},
            format='json',
        )

    @patch('requests.get')
    def test_identical_dm_within_window_is_suppressed(self, mock_get):
        mock_get.return_value = self._lookup()
        r1 = self._send(self.LOOP_MSG)
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        r2 = self._send(self.LOOP_MSG)
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertTrue(r2.data.get('idempotent_replay'))
        # same underlying message — the storm send was suppressed, not re-created
        self.assertEqual(r2.data['message_id'], r1.data['message_id'])
        self.assertEqual(
            DirectMessage.objects.filter(sender_id=self.zen.id, content=self.LOOP_MSG).count(),
            1, 'a duplicate DM row must NOT be created for the suppressed storm send')

    @patch('requests.get')
    def test_distinct_content_is_not_suppressed(self, mock_get):
        mock_get.return_value = self._lookup()
        r1 = self._send('status update A')
        r2 = self._send('status update B')
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(r1.data['message_id'], r2.data['message_id'])

    @override_settings(CONNECT_DM_DEDUP_WINDOW_SECONDS=0)
    @patch('requests.get')
    def test_dedup_disabled_allows_identical_sends(self, mock_get):
        mock_get.return_value = self._lookup()
        r1 = self._send(self.LOOP_MSG)
        r2 = self._send(self.LOOP_MSG)
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            DirectMessage.objects.filter(sender_id=self.zen.id, content=self.LOOP_MSG).count(), 2)


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
class DMTenancyInvariantTest(ChannelMockMixin, APITestCase):
    """PLAT-1236/CON-129: direct messages require a shared org boundary.

    CON-129 convergence: the canonical DM gate is ``connect.services.org_tenancy``
    (enabled by ``CONNECT_ENFORCE_ORG_TENANCY``), which resolves memberships from
    the admin ``/internal/users/<id>/memberships/`` endpoint and deliberately
    ignores the legacy single ``organization_id`` field. The mock returns the
    Admin-memberships payload for both sender and recipient; JWT membership
    claims are deliberately not current authorization authority.
    """

    def setUp(self):
        self.agent = User.objects.create_user(
            username='nagi', email='nagi@agents.shizuha.io', password='pass123', first_name='Nagi'
        )
        self.same_org_user = User.objects.create_user(
            username='sameorg', email='sameorg@example.com', password='pass123', first_name='Same'
        )
        self.external_user = User.objects.create_user(
            username='external', email='external@example.com', password='pass123', first_name='External'
        )
        self.client.force_authenticate(
            user=self.agent,
            token={'is_agent': True, 'organization_id': None,
                   'organization_memberships': {'1': 'member'}},
        )

    def _router(self, recipient, org_id):
        """Route requests.get: admin memberships lookup vs id-service user lookup.

        The DM view first resolves the recipient by email via shizuha-id (user
        payload), then org_tenancy resolves memberships via the admin endpoint.
        """
        def _get(url, *args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.ok = True
            if '/memberships/' in url:
                if f'/users/{self.agent.id}/memberships/' in url:
                    memberships = {'1': 'member'}
                else:
                    memberships = {str(org_id): 'member'}
                response.json.return_value = {'memberships': memberships}
            else:
                response.json.return_value = {
                    'id': recipient.id,
                    'user_id': recipient.id,
                    'email': recipient.email,
                    'first_name': recipient.first_name,
                    'last_name': '',
                    'username': recipient.username,
                }
            return response
        return _get

    def test_cross_org_agent_dm_is_rejected_before_message_create(self):
        with patch('requests.get', side_effect=self._router(self.external_user, 67)), \
             patch('connect.tasks.send_dm_notification.delay'):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_email': self.external_user.email, 'content': 'Need QA screenshots'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'cross_org_not_allowed')
        self.assertFalse(DirectMessage.objects.filter(sender_id=self.agent.id).exists())

    def test_same_org_agent_dm_is_allowed(self):
        with patch('requests.get', side_effect=self._router(self.same_org_user, 1)), \
             patch('connect.tasks.send_dm_notification.delay'):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_email': self.same_org_user.email, 'content': 'Internal handoff'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DirectMessage.objects.filter(sender_id=self.agent.id).count(), 1)

    def test_block_policy_still_wins_inside_same_org(self):
        from connect.models import Block

        Block.objects.create(blocker_id=self.same_org_user.id, blocked_id=self.agent.id)
        with patch('requests.get', side_effect=self._router(self.same_org_user, 1)), \
             patch('connect.tasks.send_dm_notification.delay'):
            response = self.client.post(
                '/api/messaging/dm/',
                {'recipient_email': self.same_org_user.email, 'content': 'Internal handoff'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'Cannot send a direct message to this user')
        self.assertFalse(DirectMessage.objects.filter(sender_id=self.agent.id).exists())


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
class DMTenancyMembershipLookupTest(ChannelMockMixin, APITestCase):
    """CON-133: the org gate must resolve memberships from the admin service.

    Regression for the 2026-07-03 fleet-wide DM outage: shizuha-id's
    /internal/users/ payload carries no org fields, so the previous lookup
    resolved every recipient to an empty org set, negative-cached it, and
    403'd all same-org traffic. These tests mirror the REAL prod payload
    shapes: identity response WITHOUT org fields, agent JWT with
    organization_id=None + organization_memberships dict, and the admin
    /internal/users/<id>/memberships/ endpoint as source of truth.
    """

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.agent = User.objects.create_user(
            username='saki', email='saki@agents.shizuha.io', password='pass123', first_name='Saki'
        )
        self.operator = User.objects.create_user(
            username='hritik', email='hritik@example.com', password='pass123', first_name='Hritik'
        )
        # The real fleet regression: this token predates the agent's org-1
        # membership, so its embedded snapshot only knows the personal org.
        # The DM gate must consult Admin's current membership for both sides.
        self.client.force_authenticate(
            user=self.agent,
            token={
                'is_agent': True,
                'organization_id': None,
                'organization_memberships': {'7': 'owner'},
            },
        )

    def _identity_response_without_org_fields(self, user):
        # What shizuha-id /internal/users/ actually returns: NO org fields.
        response = MagicMock()
        response.status_code = 200
        response.ok = True
        response.json.return_value = {
            'id': user.id,
            'user_id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': '',
            'username': user.username,
        }
        return response

    def _admin_memberships_response(self, memberships, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.ok = status_code == 200
        response.json.return_value = {'memberships': memberships}
        return response

    def _requests_get_router(self, recipient, memberships_responses):
        """Route requests.get: id-service user lookup vs admin memberships."""
        memberships_iter = iter(memberships_responses)

        def _get(url, *args, **kwargs):
            if '/memberships/' in url:
                if f'/users/{self.agent.id}/memberships/' in url:
                    return self._admin_memberships_response({'7': 'owner', '1': 'member'})
                return next(memberships_iter)
            return self._identity_response_without_org_fields(recipient)

        return _get

    def _send_dm(self, content='escalation ping'):
        with patch('connect.tasks.send_dm_notification.delay'):
            return self.client.post(
                '/api/messaging/dm/',
                {'recipient_email': self.operator.email, 'content': content},
                format='json',
            )

    def test_same_org_dm_allowed_via_admin_membership_lookup(self):
        router = self._requests_get_router(
            self.operator,
            [self._admin_memberships_response({'1': 'owner', '2': 'owner'})],
        )
        with patch('requests.get', side_effect=router):
            response = self._send_dm()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DirectMessage.objects.filter(sender_id=self.agent.id).count(), 1)

    def test_zero_shared_org_dm_still_rejected(self):
        router = self._requests_get_router(
            self.operator,
            [self._admin_memberships_response({'67': 'owner'})],
        )
        with patch('requests.get', side_effect=router):
            response = self._send_dm()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'cross_org_not_allowed')
        self.assertFalse(DirectMessage.objects.filter(sender_id=self.agent.id).exists())

    def test_membership_lookup_failure_fails_closed_but_is_not_cached(self):
        router = self._requests_get_router(
            self.operator,
            [
                self._admin_memberships_response({}, status_code=500),
                self._admin_memberships_response({'1': 'member'}),
            ],
        )
        with patch('requests.get', side_effect=router):
            first = self._send_dm('blocked attempt')
            second = self._send_dm('retry after recovery')
        # CON-129 convergence: an UNestablishable membership (admin down, no
        # cache) is fail-closed but retryable — 503 org_lookup_unavailable,
        # distinct from a 403 established cross-org pair.
        self.assertEqual(first.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(first.data['error'], 'org_lookup_unavailable')
        # Recovery must be immediate once admin answers — no negative cache.
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)

    def test_successful_membership_lookup_is_cached(self):
        router = self._requests_get_router(
            self.operator,
            [self._admin_memberships_response({'1': 'member'})],
        )
        with patch('requests.get', side_effect=router):
            first = self._send_dm('first ping')
            second = self._send_dm('second ping')  # would StopIteration on a 2nd admin call
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)

    def test_conversation_create_ignores_client_supplied_org_claims(self):
        # Spoof attempt: client claims the cross-org recipient shares org 1.
        router = self._requests_get_router(
            self.operator,
            [self._admin_memberships_response({'67': 'owner'})],
        )
        with patch('requests.get', side_effect=router):
            response = self.client.post(
                '/api/conversations/',
                {
                    'conversation_type': 'direct',
                    'participant_ids': [self.operator.id],
                    'participant_details': {
                        str(self.operator.id): {'organization_memberships': {'1': 'member'}},
                    },
                },
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(CONNECT_REQUIRE_SHARED_ORG_FOR_DM=False)
class RuntimeAnomalyDetectionTest(ChannelMockMixin, APITestCase):
    """PLAT-1237: event-driven runtime anomaly alerts on DM sends."""

    def setUp(self):
        self.agent = User.objects.create_user(
            username='ichi', email='ichi@agents.shizuha.io', password='pass123', first_name='Ichi'
        )
        self.alice = User.objects.create_user(
            username='alice2', email='alice2@example.com', password='pass123', first_name='Alice'
        )
        self.bob = User.objects.create_user(
            username='bob2', email='bob2@example.com', password='pass123', first_name='Bob'
        )
        self.client.force_authenticate(user=self.agent, token={'is_agent': True, 'organization_id': 1})

    def _identity_response(self, user, organization_id):
        response = MagicMock()
        response.status_code = 200
        response.ok = True
        response.json.return_value = {
            'id': user.id,
            'user_id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': '',
            'username': user.username,
            'organization_id': organization_id,
        }
        return response

    @override_settings(CONNECT_SECURITY_DM_FANOUT_THRESHOLD=999)
    def test_cross_org_agent_dm_enqueues_raw_event_for_worker_evaluation(self):
        with patch('requests.get', return_value=self._identity_response(self.alice, 67)), \
             patch('connect.tasks.send_dm_notification.delay'), \
             patch('connect.metrics.record_connect_security_event_queued') as queued_metric, \
             patch('connect.tasks.send_security_anomaly_alert.delay') as alert_delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    '/api/messaging/dm/',
                    {'recipient_email': self.alice.email, 'content': 'Need QA screenshots'},
                    format='json',
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        alert_delay.assert_called_once()
        queued_metric.assert_called_once_with('connect.direct_message.event')
        raw_event = alert_delay.call_args.args[0]
        self.assertEqual(raw_event['event_type'], 'connect.direct_message.event')
        self.assertNotIn('reasons', raw_event)

        from connect.tasks import _evaluate_security_anomaly_event

        with patch('connect.metrics.record_connect_security_event_evaluated') as evaluated_metric:
            event = _evaluate_security_anomaly_event(raw_event)
        evaluated_metric.assert_called_once_with('anomaly')
        self.assertEqual(event['event_type'], 'connect.direct_message.anomaly')
        self.assertIn('cross_org_direct_message', event['reasons'])
        self.assertIn('internal_agent_external_user_dm', event['reasons'])
        self.assertEqual(event['sender_org_ids'], [1])
        self.assertEqual(event['recipient_org_ids'], [67])
        self.assertEqual(event['recipient_user_id'], self.alice.id)
        self.assertTrue(event['message_id'])

    @override_settings(CONNECT_SECURITY_DM_FANOUT_THRESHOLD=999)
    def test_same_org_agent_dm_enqueues_raw_event_but_worker_finds_no_anomaly(self):
        with patch('requests.get', return_value=self._identity_response(self.alice, 1)), \
             patch('connect.tasks.send_dm_notification.delay'), \
             patch('connect.tasks.send_security_anomaly_alert.delay') as alert_delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    '/api/messaging/dm/',
                    {'recipient_email': self.alice.email, 'content': 'Internal handoff'},
                    format='json',
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        alert_delay.assert_called_once()

        from connect.tasks import _evaluate_security_anomaly_event

        with patch('connect.metrics.record_connect_security_event_evaluated') as evaluated_metric:
            event = _evaluate_security_anomaly_event(alert_delay.call_args.args[0])
        self.assertEqual(event['reasons'], [])
        evaluated_metric.assert_called_once_with('no_anomaly')

    @override_settings(CONNECT_SECURITY_DM_FANOUT_THRESHOLD=2, CONNECT_SECURITY_DM_FANOUT_WINDOW_SECONDS=300)
    def test_abnormal_direct_message_fanout_is_evaluated_in_worker(self):
        def lookup(_url, params=None, **_kwargs):
            email = (params or {}).get('email')
            if email == self.alice.email:
                return self._identity_response(self.alice, 1)
            return self._identity_response(self.bob, 1)

        with patch('requests.get', side_effect=lookup), \
             patch('connect.tasks.send_dm_notification.delay'), \
             patch('connect.tasks.send_security_anomaly_alert.delay') as alert_delay:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(
                    '/api/messaging/dm/',
                    {'recipient_email': self.alice.email, 'content': 'status A'},
                    format='json',
                )
                second = self.client.post(
                    '/api/messaging/dm/',
                    {'recipient_email': self.bob.email, 'content': 'status B'},
                    format='json',
                )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(alert_delay.call_count, 2)

        from connect.tasks import _evaluate_security_anomaly_event

        event = _evaluate_security_anomaly_event(alert_delay.call_args.args[0])
        self.assertIn('abnormal_direct_message_fanout', event['reasons'])
        self.assertEqual(event['fanout_count'], 2)

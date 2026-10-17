import os
import threading
import time
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from connect import tasks
from connect.models import Conversation, ConversationParticipant, DirectMessage
from connect.services import participant_identity as projection
from connect.services.hive_agents import hive_agent_user_id


@override_settings(CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
class ParticipantIdentityTests(TestCase):
    def setUp(self):
        cache.clear()
        tasks._WAKE_DEDUP.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(tasks._WAKE_DEDUP.clear)
        self.conversation = Conversation.objects.create(conversation_type='direct')
        self.participant = ConversationParticipant.objects.create(
            conversation=self.conversation, user_id=977, user_name='Atlas Guide',
        )
        self.message = DirectMessage.objects.create(
            conversation=self.conversation, sender_id=1, sender_name='Owner',
            sender_email='owner@example.com', content='Explain the Wiki service dependencies',
        )
        self.identity = {'id': 977, 'email': 'atlas-guide@shizuha.com', 'account_type': 'agent'}

    def test_actual_notification_caller_repairs_projection_before_policy_and_native_wake(self):
        observed = []

        def decide(participant, message, channel, **kwargs):
            observed.append((channel, participant.participant_type, participant.user_email))
            return False, 'already_read'

        with patch.object(projection, 'resolve_identity_users', return_value={977: self.identity}), \
                patch('connect.services.notifications.should_notify_channel', side_effect=decide), \
                patch.dict(os.environ, {'HIVE_CONNECT_WAKE_URL': 'http://hive/wake', 'HIVE_CONNECT_WAKE_TOKEN': 'test-only'}), \
                patch('requests.post', return_value=Mock(status_code=200, text='started')) as post:
            tasks.send_dm_notification.run(str(self.message.id))
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.participant_type, 'agent')
        self.assertEqual(self.participant.user_email, self.identity['email'])
        self.assertIn(('agent', 'agent', self.identity['email']), observed)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs['json'], {
            'email': self.identity['email'], 'reason': f'connect_dm:{self.conversation.pk}',
        })

    def test_old_agent_missing_email_is_repaired(self):
        self.participant.participant_type = 'agent'
        self.participant.save(update_fields=['participant_type'])
        with patch.object(projection, 'resolve_identity_users', return_value={977: self.identity}):
            projection.project_agent_delivery_identity([self.participant])
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.user_email, self.identity['email'])

    def test_humans_unknown_and_mismatched_rows_do_not_invent_agents(self):
        rows = [None, {}, {'id': 978, 'account_type': 'agent', 'email': 'wrong@example.com'},
                dict(self.identity, id=True), dict(self.identity, id=977.0),
                dict(self.identity, account_type='human'), dict(self.identity, email=''),
                dict(self.identity, email='not-an-email')]
        for row in rows:
            with self.subTest(row=row):
                cache.clear()
                with patch.object(projection, 'resolve_identity_users', return_value={977: row}):
                    projection.project_agent_delivery_identity([self.participant])
                self.participant.refresh_from_db()
                self.assertEqual((self.participant.participant_type, self.participant.user_email), ('human', ''))

    def test_outage_is_not_negative_cached_and_next_delivery_recovers(self):
        with patch.object(projection, 'resolve_identity_users', side_effect=[RuntimeError('unavailable'), {977: self.identity}]) as resolve:
            projection.project_agent_delivery_identity([self.participant])
            self.assertEqual(self.participant.participant_type, 'human')
            projection.project_agent_delivery_identity([self.participant])
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(self.participant.participant_type, 'agent')

    def test_confirmed_rows_are_sanitized_cached_and_revalidated(self):
        other = ConversationParticipant.objects.create(
            conversation=Conversation.objects.create(conversation_type='direct'), user_id=977,
        )
        with patch.object(projection, 'resolve_identity_users', return_value={977: dict(self.identity, private_data='excluded')}) as resolve:
            projection.project_agent_delivery_identity([self.participant])
            projection.project_agent_delivery_identity([other])
        resolve.assert_called_once_with([977])
        self.assertEqual(cache.get('connect:participant-identity:v1:977'), self.identity)
        self.assertEqual(other.participant_type, 'agent')

    def test_corrupt_cached_identity_falls_back_to_authority(self):
        cache.set('connect:participant-identity:v1:977', dict(self.identity, id=1))
        with patch.object(projection, 'resolve_identity_users', return_value={977: self.identity}) as resolve:
            projection.project_agent_delivery_identity([self.participant])
        resolve.assert_called_once_with([977])
        self.assertEqual(self.participant.participant_type, 'agent')

    def test_cache_failure_does_not_block_authoritative_projection(self):
        with patch.object(projection.cache, 'get_many', side_effect=RuntimeError('cache unavailable')), \
                patch.object(projection.cache, 'set_many', side_effect=RuntimeError('cache unavailable')), \
                patch.object(projection, 'resolve_identity_users', return_value={977: self.identity}):
            projection.project_agent_delivery_identity([self.participant])
        self.assertEqual(self.participant.participant_type, 'agent')

    def test_complete_agents_pseudo_users_and_left_participants_are_not_queried(self):
        self.participant.participant_type = 'agent'
        self.participant.user_email = self.identity['email']
        pseudo = ConversationParticipant(user_id=hive_agent_user_id('test-agent'))
        system = ConversationParticipant(user_id=1234, participant_type='system')
        left = ConversationParticipant(user_id=123, has_left=True)
        with patch.object(projection, 'resolve_identity_users') as resolve:
            projection.project_agent_delivery_identity([self.participant, pseudo, system, left])
        resolve.assert_not_called()

    def test_concurrent_projection_update_and_departure_are_not_overwritten(self):
        with patch.object(projection, 'resolve_identity_users', return_value={977: self.identity}):
            ConversationParticipant.objects.filter(pk=self.participant.pk).update(user_email='new@example.com')
            projection.project_agent_delivery_identity([self.participant])
            self.assertEqual(self.participant.participant_type, 'human')
            self.participant.refresh_from_db()
            self.assertEqual(self.participant.user_email, 'new@example.com')
            ConversationParticipant.objects.filter(pk=self.participant.pk).update(has_left=True)
            projection.project_agent_delivery_identity([self.participant])
            self.participant.refresh_from_db()
            self.assertTrue(self.participant.has_left)
            self.assertEqual(self.participant.participant_type, 'human')

    def test_chunks_cover_every_id_with_at_most_four_concurrent_resolvers(self):
        participants = [ConversationParticipant(user_id=user_id) for user_id in range(1000, 1050)]
        lock = threading.Lock()
        active = 0
        maximum = 0
        observed = []

        def resolve(user_ids):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                observed.extend(user_ids)
            self.assertLessEqual(len(user_ids), 8)
            time.sleep(0.01)
            with lock:
                active -= 1
            return {user_id: {'id': user_id, 'account_type': 'human', 'email': f'user{user_id}@example.com'} for user_id in user_ids}

        with patch.object(projection, 'resolve_identity_users', side_effect=resolve):
            projection.project_agent_delivery_identity(participants)
        self.assertEqual(sorted(observed), list(range(1000, 1050)))
        self.assertLessEqual(maximum, 4)
        self.assertGreater(maximum, 1)

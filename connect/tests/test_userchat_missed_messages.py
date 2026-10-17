"""
Regression tests for PLAT-6: UserChatConsumer must replay DMs that arrived
while the user was disconnected.

Targets `UserChatConsumer._fetch_missed_messages` — the synchronous DB query
that powers the WS-side `_push_missed_messages_all_conversations` flush. We
exercise the unwrapped sync function directly to keep the test focused on
SQL semantics (cutoff, sender exclusion, already-delivered exclusion, cap)
without spinning up a full Channels test harness.
"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, TransactionTestCase
from django.utils import timezone

from connect.consumers import MissedMessageBatch, UserChatConsumer
from connect.metrics import DM_REPLAY_PER_RECONNECT_COUNT
from connect.models import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    MessageDelivery,
    MessageDeletion,
)

User = get_user_model()


def _unwrap(method):
    """Reach the original sync function inside @database_sync_to_async.

    `database_sync_to_async(fn)` returns a partial wrapping a
    DatabaseSyncToAsync instance whose `.func` is the original sync
    function. We dig two layers down so the test calls the SQL directly
    instead of returning a coroutine.
    """
    sync_to_async_inst = getattr(method.func, '__self__', None)
    if sync_to_async_inst is not None and hasattr(sync_to_async_inst, 'func'):
        return sync_to_async_inst.func
    return method


def _make_consumer_for(user, *, is_agent=False):
    consumer = UserChatConsumer.__new__(UserChatConsumer)
    consumer.user = user
    consumer.scope = {'user': user}
    consumer.channel_layer = MagicMock()
    consumer.channel_layer.group_add = AsyncMock()
    consumer.channel_name = 'test.channel'
    consumer.conversation_groups = set()
    consumer.is_agent = is_agent
    consumer.processing_ack_capable = False
    consumer._delivered_message_ids_this_connection = set()
    consumer._announced_conversation_ids_this_connection = set()
    return consumer


class UserChatConsumerReplayFlowTests(TransactionTestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice-flow', email='alice-flow@example.com', password='pw'
        )

    def test_connect_uses_single_authoritative_reconnect_replay_path(self):
        consumer = _make_consumer_for(self.alice)
        consumer.scope = {
            'user': self.alice,
            'token': {},
            'query_string': b'token=opaque&processing_ack=1',
        }
        consumer.accept = AsyncMock()
        consumer.close = AsyncMock()
        consumer._join_all_groups = AsyncMock()
        consumer._push_missed_messages_all_conversations = AsyncMock()
        consumer._push_unprocessed_agent_messages = AsyncMock()
        consumer._push_missed_messages = AsyncMock()
        consumer._keepalive_loop = MagicMock(return_value=object())

        with patch('asyncio.create_task', return_value=object()):
            async_to_sync(consumer.connect)()

        consumer.accept.assert_awaited_once()
        consumer._join_all_groups.assert_awaited_once()
        consumer._push_missed_messages_all_conversations.assert_awaited_once()
        consumer._push_unprocessed_agent_messages.assert_awaited_once()
        consumer._push_missed_messages.assert_not_called()
        self.assertTrue(consumer.processing_ack_capable)

    def test_connect_quarantines_missed_delivery_for_legacy_agent_with_pending_ack(self):
        consumer = _make_consumer_for(self.alice, is_agent=True)
        consumer.scope = {
            'user': self.alice,
            'token': {'is_agent': True},
            'query_string': b'token=opaque',
        }
        consumer.accept = AsyncMock()
        consumer.close = AsyncMock()
        consumer._join_all_groups = AsyncMock()
        consumer._legacy_agent_processing_quarantined = AsyncMock(return_value=True)
        consumer._push_missed_messages_all_conversations = AsyncMock()
        consumer._push_unprocessed_agent_messages = AsyncMock()
        consumer._keepalive_loop = MagicMock(return_value=object())

        with patch('asyncio.create_task', return_value=object()):
            async_to_sync(consumer.connect)()

        consumer._push_missed_messages_all_conversations.assert_not_awaited()
        consumer._push_unprocessed_agent_messages.assert_awaited_once()

    def test_explicit_cursor_sync_keeps_cursor_replay_and_sync_complete(self):
        consumer = _make_consumer_for(self.alice)
        consumer._push_missed_messages_all_conversations = AsyncMock()
        consumer._push_missed_messages = AsyncMock()
        sent_payloads = []

        async def send(text_data):
            sent_payloads.append(json.loads(text_data))

        consumer.send = send

        async_to_sync(consumer.receive)(
            text_data=json.dumps({'type': 'sync', 'since_event_id': 123})
        )

        consumer._push_missed_messages.assert_awaited_once_with(
            since_event_id=123, record_reconnect_metric=True
        )
        consumer._push_missed_messages_all_conversations.assert_not_called()
        self.assertEqual(sent_payloads, [{'type': 'sync_complete', 'since_event_id': 123}])

    def test_invalid_processing_ack_id_is_ignored_without_querying_the_database(self):
        consumer = _make_consumer_for(self.alice, is_agent=True)
        consumer._record_message_processed = AsyncMock()

        async_to_sync(consumer.receive)(text_data=json.dumps({
            'type': 'agent.message_processed',
            'message_id': 'not-a-uuid',
        }))

        consumer._record_message_processed.assert_not_awaited()


class FetchMissedMessagesTest(TransactionTestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pw'
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pw'
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id, user_name='alice',
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id, user_name='bob',
        )
        self.consumer = _make_consumer_for(self.alice)
        self.fetch = _unwrap(UserChatConsumer._fetch_missed_messages)
        self.fetch_conversations = _unwrap(UserChatConsumer._fetch_conversation_replay_payloads)

    def _send(self, sender_id, content='hi', is_deleted=False):
        return DirectMessage.objects.create(
            conversation=self.conv,
            sender_id=sender_id,
            sender_email='x@example.com',
            content=content,
            is_deleted=is_deleted,
        )

    def test_replays_messages_from_other_users(self):
        msg = self._send(sender_id=self.bob.id, content='hello alice')
        result = self.fetch(self.consumer)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], str(msg.id))

    def test_excludes_own_sent_messages(self):
        self._send(sender_id=self.alice.id, content='self note')
        self._send(sender_id=self.bob.id, content='from bob')
        result = self.fetch(self.consumer)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['content'], 'from bob')

    def test_excludes_deleted_messages(self):
        self._send(sender_id=self.bob.id, is_deleted=True)
        result = self.fetch(self.consumer)
        self.assertEqual(result, [])

    def test_excludes_already_delivered_messages(self):
        msg = self._send(sender_id=self.bob.id, content='already seen')
        MessageDelivery.objects.create(message=msg, user_id=self.alice.id)
        result = self.fetch(self.consumer)
        self.assertEqual(result, [])

    def test_processing_ack_replay_includes_delivered_but_unprocessed_agent_message(self):
        msg = self._send(sender_id=self.bob.id, content='must finish processing')
        MessageDelivery.objects.create(
            message=msg,
            user_id=self.alice.id,
            processing_ack_required=True,
        )
        consumer = _make_consumer_for(self.alice, is_agent=True)
        fetch = _unwrap(UserChatConsumer._fetch_unprocessed_agent_messages)

        result = fetch(consumer)

        self.assertEqual([row['id'] for row in result], [str(msg.id)])

    def test_processing_ack_removes_message_from_replay(self):
        msg = self._send(sender_id=self.bob.id, content='processed')
        MessageDelivery.objects.create(
            message=msg,
            user_id=self.alice.id,
            processing_ack_required=True,
        )
        consumer = _make_consumer_for(self.alice, is_agent=True)
        ack = _unwrap(UserChatConsumer._record_message_processed)
        fetch = _unwrap(UserChatConsumer._fetch_unprocessed_agent_messages)

        self.assertEqual(ack(consumer, str(msg.id)), 1)
        self.assertEqual(fetch(consumer), [])

    def test_processing_ack_can_arrive_before_post_send_delivery_receipt(self):
        msg = self._send(sender_id=self.bob.id, content='fast processing')
        consumer = _make_consumer_for(self.alice, is_agent=True)
        ack = _unwrap(UserChatConsumer._record_message_processed)

        self.assertEqual(ack(consumer, str(msg.id)), 1)

        receipt = MessageDelivery.objects.get(message=msg, user_id=self.alice.id)
        self.assertTrue(receipt.processing_ack_required)
        self.assertIsNotNone(receipt.processed_at)

    def test_capable_agent_delivery_is_processing_ack_required(self):
        msg = self._send(sender_id=self.bob.id, content='handshake-classified')
        consumer = _make_consumer_for(self.alice, is_agent=True)
        consumer.processing_ack_capable = True
        record_delivery = _unwrap(UserChatConsumer._record_bulk_delivery)

        self.assertEqual(record_delivery(consumer, [msg.id]), 1)

        receipt = MessageDelivery.objects.get(message=msg, user_id=self.alice.id)
        self.assertTrue(receipt.processing_ack_required)

    def test_pending_processing_ack_quarantines_only_legacy_agent(self):
        msg = self._send(sender_id=self.bob.id, content='requires successor runtime')
        MessageDelivery.objects.create(
            message=msg,
            user_id=self.alice.id,
            processing_ack_required=True,
        )
        quarantine = _unwrap(UserChatConsumer._legacy_agent_processing_quarantined)
        legacy_agent = _make_consumer_for(self.alice, is_agent=True)
        capable_agent = _make_consumer_for(self.alice, is_agent=True)
        capable_agent.processing_ack_capable = True
        human = _make_consumer_for(self.alice, is_agent=False)

        self.assertTrue(quarantine(legacy_agent))
        self.assertFalse(quarantine(capable_agent))
        self.assertFalse(quarantine(human))

    def test_processing_replay_dead_letters_at_bound_and_ack_can_clear_it(self):
        msg = self._send(sender_id=self.bob.id, content='bounded retry')
        receipt = MessageDelivery.objects.create(
            message=msg,
            user_id=self.alice.id,
            processing_ack_required=True,
        )
        consumer = _make_consumer_for(self.alice, is_agent=True)
        attempt = _unwrap(UserChatConsumer._record_processing_attempt)
        ack = _unwrap(UserChatConsumer._record_message_processed)

        for _ in range(5):
            attempt(consumer, str(msg.id))

        receipt.refresh_from_db()
        self.assertEqual(receipt.processing_attempts, 5)
        self.assertIsNotNone(receipt.processing_dead_lettered_at)
        self.assertEqual(ack(consumer, str(msg.id)), 1)
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.processed_at)
        self.assertIsNone(receipt.processing_dead_lettered_at)

    def test_respects_seven_day_floor(self):
        old = self._send(sender_id=self.bob.id, content='ancient')
        DirectMessage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10),
        )
        recent = self._send(sender_id=self.bob.id, content='fresh')
        result = self.fetch(self.consumer)
        ids = [r['id'] for r in result]
        self.assertIn(str(recent.id), ids)
        self.assertNotIn(str(old.id), ids)

    def test_read_marker_does_not_suppress_undelivered_replay(self):
        # PLAT-9268 (reverses the old `test_respects_last_read_at_cutoff`):
        # last_read_at is a READ marker, not a DELIVERY boundary. A message
        # sent before the last read but never delivered (no receipt — stored
        # during a WS outage) MUST still replay; only delivered rows are
        # excluded (CON-110). The old assertion (older-than-last-read rows
        # never replay) was the silent-loss mechanism.
        cutoff = timezone.now() - timedelta(minutes=30)
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id,
        ).update(last_read_at=cutoff)

        before = self._send(sender_id=self.bob.id, content='before')
        DirectMessage.objects.filter(pk=before.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
        )
        after = self._send(sender_id=self.bob.id, content='after')

        result = self.fetch(self.consumer)
        ids = [r['id'] for r in result]
        self.assertIn(str(after.id), ids)
        # The undelivered older row replays despite last_read_at being newer…
        self.assertIn(str(before.id), ids)
        # …and a DELIVERED older row stays excluded (no re-send).
        delivered_old = self._send(sender_id=self.bob.id, content='delivered-old')
        DirectMessage.objects.filter(pk=delivered_old.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
        )
        MessageDelivery.objects.get_or_create(
            message=delivered_old, user_id=self.alice.id)
        result2 = self.fetch(self.consumer)
        ids2 = [r['id'] for r in result2]
        self.assertNotIn(str(delivered_old.id), ids2)

    def test_respects_cleared_at_cutoff_when_later_than_last_read_at(self):
        last_read_at = timezone.now() - timedelta(hours=3)
        cleared_at = timezone.now() - timedelta(minutes=30)
        ConversationParticipant.objects.filter(
            conversation=self.conv, user_id=self.alice.id,
        ).update(last_read_at=last_read_at, cleared_at=cleared_at)

        pre_clear = self._send(sender_id=self.bob.id, content='pre-clear')
        DirectMessage.objects.filter(pk=pre_clear.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
        )
        post_clear = self._send(sender_id=self.bob.id, content='post-clear')

        result = self.fetch(self.consumer)
        ids = [r['id'] for r in result]
        self.assertIn(str(post_clear.id), ids)
        self.assertNotIn(str(pre_clear.id), ids)

    def test_excludes_per_user_deleted_messages(self):
        deleted_for_alice = self._send(sender_id=self.bob.id, content='deleted for alice')
        MessageDeletion.objects.create(message=deleted_for_alice, user_id=self.alice.id)
        visible = self._send(sender_id=self.bob.id, content='still visible')

        result = self.fetch(self.consumer)
        ids = [r['id'] for r in result]
        self.assertIn(str(visible.id), ids)
        self.assertNotIn(str(deleted_for_alice.id), ids)


    def test_cap_applies_after_delivery_filter(self):
        base = timezone.now()
        older_undelivered = self._send(sender_id=self.bob.id, content='older undelivered')
        DirectMessage.objects.filter(pk=older_undelivered.pk).update(
            created_at=base - timedelta(minutes=10),
        )

        # Newest 200 messages were already delivered live. The replay cap must
        # apply after filtering these rows so older undelivered messages inside
        # the 7-day window are not permanently starved.
        delivered = []
        for i in range(200):
            msg = self._send(sender_id=self.bob.id, content=f'delivered {i}')
            DirectMessage.objects.filter(pk=msg.pk).update(
                created_at=base + timedelta(seconds=i),
            )
            delivered.append(MessageDelivery(message=msg, user_id=self.alice.id))
        MessageDelivery.objects.bulk_create(delivered)

        result = self.fetch(self.consumer)
        self.assertEqual([r['id'] for r in result], [str(older_undelivered.id)])

    @override_settings(CONNECT_AGENT_DM_REPLAY_LIMIT=2, CONNECT_AGENT_DM_REPLAY_WINDOW_SECONDS=60)
    def test_agent_replay_uses_short_window_and_cap(self):
        self.consumer.is_agent = True
        base = timezone.now()

        stale = self._send(sender_id=self.bob.id, content='stale agent replay')
        DirectMessage.objects.filter(pk=stale.pk).update(
            created_at=base - timedelta(minutes=2),
        )
        recent = []
        for i in range(3):
            msg = self._send(sender_id=self.bob.id, content=f'recent {i}')
            DirectMessage.objects.filter(pk=msg.pk).update(
                created_at=base - timedelta(seconds=3 - i),
            )
            recent.append(msg)

        result = self.fetch(self.consumer)

        self.assertIsInstance(result, MissedMessageBatch)
        self.assertTrue(result.capped)
        self.assertEqual(
            [row['id'] for row in result],
            [str(recent[1].id), str(recent[2].id)],
        )
        self.assertNotIn(str(stale.id), [row['id'] for row in result])

    @override_settings(CONNECT_AGENT_DM_REPLAY_LIMIT=25, CONNECT_AGENT_DM_REPLAY_WINDOW_SECONDS=3600)
    def test_agent_replay_excludes_delivery_receipts(self):
        self.consumer.is_agent = True
        delivered = self._send(sender_id=self.bob.id, content='already delivered to agent')
        MessageDelivery.objects.create(message=delivered, user_id=self.alice.id)
        visible = self._send(sender_id=self.bob.id, content='new for agent')

        result = self.fetch(self.consumer)
        ids = [row['id'] for row in result]

        self.assertEqual(ids, [str(visible.id)])
        self.assertNotIn(str(delivered.id), ids)

    def test_replay_conversation_payloads_include_offline_created_thread(self):
        payloads = self.fetch_conversations(self.consumer, {str(self.conv.id)})
        self.assertIn(str(self.conv.id), payloads)
        self.assertEqual(payloads[str(self.conv.id)]['id'], str(self.conv.id))

    def test_replay_conversation_payloads_preserve_current_user_flags(self):
        ConversationParticipant.objects.filter(
            conversation=self.conv,
            user_id=self.alice.id,
        ).update(
            unread_count=3,
            is_muted=True,
            is_pinned=True,
            is_archived=True,
        )

        payloads = self.fetch_conversations(self.consumer, {str(self.conv.id)})
        payload = payloads[str(self.conv.id)]

        self.assertEqual(payload['unread_count'], 3)
        self.assertTrue(payload['is_muted'])
        self.assertTrue(payload['is_pinned'])
        self.assertTrue(payload['is_archived'])
        self.assertEqual(payload['participant_names'], ['bob'])

    def test_human_push_replays_conversation_created_without_persisting_delivery(self):
        msg = self._send(sender_id=self.bob.id, content='offline first dm')
        sent = []
        recorded = []

        async def fake_fetch_missed():
            return [{'id': str(msg.id), 'conversation_id': str(self.conv.id), 'content': msg.content}]

        async def fake_fetch_conversations(conversation_ids):
            return {str(self.conv.id): {'id': str(self.conv.id), 'conversation_type': 'direct'}}

        async def fake_record_bulk_delivery(message_ids):
            recorded.extend(message_ids)

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        self.consumer._fetch_missed_messages = fake_fetch_missed
        self.consumer._fetch_conversation_replay_payloads = fake_fetch_conversations
        self.consumer._record_bulk_delivery = fake_record_bulk_delivery
        self.consumer.send = fake_send

        async_to_sync(self.consumer._push_missed_messages_all_conversations)()

        self.consumer.channel_layer.group_add.assert_awaited_once_with(
            f'chat_{self.conv.id}', self.consumer.channel_name,
        )
        self.assertIn(f'chat_{self.conv.id}', self.consumer.conversation_groups)
        self.assertEqual([frame['type'] for frame in sent], ['conversation_created', 'missed_message'])
        self.assertEqual(sent[0]['conversation_id'], str(self.conv.id))
        self.assertTrue(sent[0]['replayed'])
        self.assertEqual(recorded, [])
        offline_same_user_socket = _make_consumer_for(self.alice)
        replay = self.fetch(offline_same_user_socket)
        self.assertIn(str(msg.id), [row['id'] for row in replay])

    def test_agent_push_replays_and_persists_delivery(self):
        msg = self._send(sender_id=self.bob.id, content='agent offline dm')
        sent = []
        recorded = []

        async def fake_fetch_missed():
            return [{'id': str(msg.id), 'conversation_id': str(self.conv.id), 'content': msg.content}]

        async def fake_fetch_conversations(conversation_ids):
            return {str(self.conv.id): {'id': str(self.conv.id), 'conversation_type': 'direct'}}

        async def fake_record_bulk_delivery(message_ids):
            recorded.extend(message_ids)

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        self.consumer.is_agent = True
        self.consumer._fetch_missed_messages = fake_fetch_missed
        self.consumer._fetch_conversation_replay_payloads = fake_fetch_conversations
        self.consumer._record_bulk_delivery = fake_record_bulk_delivery
        self.consumer.send = fake_send

        async_to_sync(self.consumer._push_missed_messages_all_conversations)()

        self.assertEqual([frame['type'] for frame in sent], ['conversation_created', 'missed_message'])
        self.assertEqual(recorded, [str(msg.id)])

    def test_push_skips_replay_when_live_delivery_won_same_connection_race(self):
        msg = self._send(sender_id=self.bob.id, content='live won')
        self.consumer._delivered_message_ids_this_connection.add(str(msg.id))
        sent = []

        async def fake_fetch_missed():
            return [{'id': str(msg.id), 'conversation_id': str(self.conv.id), 'content': msg.content}]

        async def fake_fetch_conversations(conversation_ids):
            raise AssertionError('conversation payloads should not be fetched after dedupe')

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        self.consumer._fetch_missed_messages = fake_fetch_missed
        self.consumer._fetch_conversation_replay_payloads = fake_fetch_conversations
        self.consumer.send = fake_send

        async_to_sync(self.consumer._push_missed_messages_all_conversations)()

        self.assertEqual(sent, [])

    def test_new_message_skips_rows_replayed_on_same_connection(self):
        msg = self._send(sender_id=self.bob.id, content='already replayed')
        self.consumer._delivered_message_ids_this_connection.add(str(msg.id))
        sent = []

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        self.consumer.send = fake_send

        async_to_sync(self.consumer.new_message)({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.bob.id,
                'content': msg.content,
            }
        })

        self.assertEqual(sent, [])

    def test_human_live_delivery_does_not_persist_user_delivery_receipt(self):
        msg = self._send(sender_id=self.bob.id, content='human multi device')
        sent = []
        recorded = []

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        async def fake_record_bulk_delivery(message_ids):
            recorded.extend(message_ids)

        self.consumer.send = fake_send
        self.consumer._record_bulk_delivery = fake_record_bulk_delivery
        self.consumer.is_agent = False

        async_to_sync(self.consumer.new_message)({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.bob.id,
                'content': msg.content,
            }
        })

        self.assertEqual([frame['type'] for frame in sent], ['new_message'])
        self.assertIn(str(msg.id), self.consumer._delivered_message_ids_this_connection)
        self.assertEqual(recorded, [])
        self.assertFalse(
            MessageDelivery.objects.filter(message=msg, user_id=self.alice.id).exists()
        )
        offline_same_user_socket = _make_consumer_for(self.alice)
        replay = self.fetch(offline_same_user_socket)
        self.assertIn(str(msg.id), [row['id'] for row in replay])

    def test_agent_live_delivery_records_user_delivery_receipt(self):
        msg = self._send(sender_id=self.bob.id, content='agent restart dedupe')
        sent = []
        recorded = []

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        async def fake_record_bulk_delivery(message_ids):
            recorded.extend(message_ids)

        self.consumer.send = fake_send
        self.consumer._record_bulk_delivery = fake_record_bulk_delivery
        self.consumer.is_agent = True

        async_to_sync(self.consumer.new_message)({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.bob.id,
                'content': msg.content,
            }
        })

        self.assertEqual([frame['type'] for frame in sent], ['new_message'])
        self.assertIn(str(msg.id), self.consumer._delivered_message_ids_this_connection)
        self.assertEqual(recorded, [str(msg.id)])

    def test_legacy_agent_quarantine_preserves_live_message_as_undelivered(self):
        pending = self._send(sender_id=self.bob.id, content='already transport delivered')
        MessageDelivery.objects.create(
            message=pending,
            user_id=self.alice.id,
            processing_ack_required=True,
        )
        msg = self._send(sender_id=self.bob.id, content='wait for successor')
        consumer = _make_consumer_for(self.alice, is_agent=True)
        sent = []

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        consumer.send = fake_send

        async_to_sync(consumer.new_message)({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.bob.id,
                'content': msg.content,
            }
        })

        self.assertEqual(sent, [])
        self.assertFalse(
            MessageDelivery.objects.filter(message=msg, user_id=self.alice.id).exists()
        )

    def test_returns_empty_for_user_with_no_conversations(self):
        loner = User.objects.create_user(
            username='loner', email='loner@example.com', password='pw'
        )
        consumer = _make_consumer_for(loner)
        result = self.fetch(consumer)
        self.assertEqual(result, [])

    def test_chronological_order(self):
        a = self._send(sender_id=self.bob.id, content='first')
        b = self._send(sender_id=self.bob.id, content='second')
        c = self._send(sender_id=self.bob.id, content='third')
        result = self.fetch(self.consumer)
        self.assertEqual(
            [r['id'] for r in result],
            [str(a.id), str(b.id), str(c.id)],
        )


class MissedMessageMetricsTest(TransactionTestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username='metrics-alice', email='metrics-alice@example.com', password='pw'
        )
        self.bob = User.objects.create_user(
            username='metrics-bob', email='metrics-bob@example.com', password='pw'
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
        )
        self.consumer = _make_consumer_for(self.alice)
        self.consumer.is_agent = False
        self.fetch = _unwrap(UserChatConsumer._fetch_missed_messages)
        self.record_bulk_delivery = _unwrap(UserChatConsumer._record_bulk_delivery)

    def _send(self, sender_id, content='hi'):
        return DirectMessage.objects.create(
            conversation=self.conv,
            sender_id=sender_id,
            sender_email='x@example.com',
            content=content,
        )

    def test_fetch_marks_batch_capped_only_when_candidate_cap_has_more_rows(self):
        for i in range(201):
            self._send(sender_id=self.bob.id, content=f'msg {i}')

        result = self.fetch(self.consumer)

        self.assertIsInstance(result, MissedMessageBatch)
        self.assertTrue(result.capped)
        self.assertEqual(len(result), 200)

    def test_fetch_does_not_mark_exact_cap_as_truncated(self):
        for i in range(200):
            self._send(sender_id=self.bob.id, content=f'exact {i}')

        result = self.fetch(self.consumer)

        self.assertIsInstance(result, MissedMessageBatch)
        self.assertFalse(result.capped)
        self.assertEqual(len(result), 200)

    def test_fetch_marks_batch_uncapped_when_below_candidate_cap(self):
        self._send(sender_id=self.bob.id, content='below cap')

        result = self.fetch(self.consumer)

        self.assertIsInstance(result, MissedMessageBatch)
        self.assertFalse(result.capped)
        self.assertEqual(len(result), 1)

    def test_record_bulk_delivery_returns_new_delivery_write_count(self):
        msg = self._send(sender_id=self.bob.id, content='delivered once')

        created = self.record_bulk_delivery(self.consumer, [msg.id])
        duplicate = self.record_bulk_delivery(self.consumer, [msg.id])

        self.assertEqual(created, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(
            MessageDelivery.objects.filter(message=msg, user_id=self.alice.id).count(),
            1,
        )

    def test_over_cap_backlog_advances_on_subsequent_replay_with_no_skipped_rows(self):
        """CON-231 item 4: >200 eligible DMs across conversations cap-hit, then
        subsequent replay drains the rest — no eligible row is permanently skipped.

        The cap is applied AFTER excluding already-delivered rows (the
        PLAT-6 fix), so delivering the first 200 must let the next replay
        surface the remaining eligible rows rather than re-serving the same
        newest 200 forever.
        """
        # Two conversations so the >200 candidate set spans multiple
        # conversations (the acceptance's "across multiple conversations").
        conv2 = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=conv2, user_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=conv2, user_id=self.bob.id,
        )

        total = 250
        for i in range(130):
            DirectMessage.objects.create(
                conversation=self.conv, sender_id=self.bob.id,
                sender_email='x@example.com', content=f'conv1 msg {i}',
            )
        for i in range(total - 130):
            DirectMessage.objects.create(
                conversation=conv2, sender_id=self.bob.id,
                sender_email='x@example.com', content=f'conv2 msg {i}',
            )

        # First reconnect: capped at 200, cap flag set.
        first = self.fetch(self.consumer)
        self.assertTrue(first.capped)
        self.assertEqual(len(first), 200)

        # Deliver exactly what the first replay returned (simulates the client
        # applying the 200 missed_message frames).
        for row in first:
            MessageDelivery.objects.create(
                message_id=row['id'], user_id=self.alice.id,
            )

        # Second reconnect: the remaining 50 eligible rows must surface — the
        # already-delivered 200 are excluded BEFORE the cap is applied, so no
        # eligible row is permanently skipped.
        second = self.fetch(self.consumer)
        self.assertFalse(second.capped)
        self.assertEqual(len(second), total - 200)

        # Union of both replays covers every eligible row exactly once.
        delivered_ids = {row['id'] for row in first}
        second_ids = {row['id'] for row in second}
        self.assertEqual(len(delivered_ids & second_ids), 0)
        self.assertEqual(len(delivered_ids | second_ids), total)

    def test_replay_observes_zero_count_and_cap_hit_without_messages(self):
        consumer = _make_consumer_for(self.alice)
        consumer.is_agent = True

        async def fetch():
            return MissedMessageBatch([], capped=True)

        consumer._fetch_missed_messages = fetch

        with patch('connect.consumers.observe_dm_replay_per_reconnect') as observe, \
             patch('connect.consumers.record_dm_replay_capped') as capped, \
             patch('connect.consumers.record_dm_replay_fanout') as fanout:
            async_to_sync(consumer._push_missed_messages_all_conversations)()

        observe.assert_called_once_with(0)
        # CON-231: a cap hit now carries bounded attribution. An unlabeled tick
        # told an operator only that SOME reconnect capped, which is what made
        # the 01:51 burst un-triageable.
        capped.assert_called_once_with('agent', 'no_candidates')
        fanout.assert_not_called()

    def test_cursor_replay_records_replay_metrics(self):
        msg = self._send(sender_id=self.bob.id, content='cursor replay')
        consumer = _make_consumer_for(self.alice)
        sent_payloads = []

        async def send(text_data):
            sent_payloads.append(json.loads(text_data))

        consumer.send = send

        with patch('connect.consumers.observe_dm_replay_per_reconnect') as observe, \
             patch('connect.consumers.record_dm_replay_fanout') as fanout:
            async_to_sync(consumer._push_missed_messages)(
                since_event_id=None,
                record_reconnect_metric=True,
            )

        self.assertEqual([frame['message']['id'] for frame in sent_payloads], [str(msg.id)])
        fanout.assert_called_once_with('human', 1)
        observe.assert_called_once_with(1)

    def test_connect_time_cursor_fallback_records_fanout_without_second_reconnect_sample(self):
        msg = self._send(sender_id=self.bob.id, content='connect fallback replay')
        consumer = _make_consumer_for(self.alice)
        sent_payloads = []

        async def send(text_data):
            sent_payloads.append(json.loads(text_data))

        consumer.send = send

        with patch('connect.consumers.observe_dm_replay_per_reconnect') as observe, \
             patch('connect.consumers.record_dm_replay_fanout') as fanout:
            async_to_sync(consumer._push_missed_messages)(since_event_id=None)

        self.assertEqual([frame['message']['id'] for frame in sent_payloads], [str(msg.id)])
        fanout.assert_called_once_with('human', 1)
        observe.assert_not_called()

    def test_replay_histogram_has_cursor_sync_500_row_bucket(self):
        self.assertIn(500.0, DM_REPLAY_PER_RECONNECT_COUNT._upper_bounds)

    def test_live_delivery_metric_counts_new_message_delivery_write(self):
        consumer = _make_consumer_for(self.alice)
        consumer.is_agent = True
        sent_payloads = []

        async def send(text_data):
            sent_payloads.append(text_data)

        async def record_bulk_delivery(message_ids):
            self.assertEqual(message_ids, ['msg-1'])
            return 1

        consumer.send = send
        consumer._record_bulk_delivery = record_bulk_delivery

        with patch('connect.consumers.record_dm_live_delivery') as live_delivery:
            async_to_sync(consumer.new_message)({
                'message_data': {
                    'id': 'msg-1',
                    'conversation_id': str(self.conv.id),
                    'sender_id': self.bob.id,
                    'content': 'live',
                },
            })

        self.assertEqual(len(sent_payloads), 1)
        live_delivery.assert_called_once_with('agent', 1)


class SelfEchoSuppressionGateTest(TransactionTestCase):
    """PLAT-965: the sender_id self-echo suppression in UserChatConsumer.new_message
    must be gated on the ABSENCE of origin_channel, so a human's OTHER devices still
    receive their own WS-posted messages live (multi-device sync), while HTTP-posted
    self-echoes (agents, no origin_channel) stay suppressed (the CON-106 fix)."""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='echo-alice', email='echo-alice@example.com', password='pw'
        )
        self.bob = User.objects.create_user(
            username='echo-bob', email='echo-bob@example.com', password='pw'
        )
        self.conv = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id, user_name='alice',
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id, user_name='bob',
        )
        # This consumer represents alice's SECOND device on channel 'test.channel'.
        self.consumer = _make_consumer_for(self.alice)

    def _send(self, sender_id, content='hi'):
        return DirectMessage.objects.create(
            conversation=self.conv, sender_id=sender_id,
            sender_email='x@example.com', content=content,
        )

    def _deliver(self, event):
        sent = []

        async def fake_send(text_data):
            sent.append(json.loads(text_data))

        self.consumer.send = fake_send
        async_to_sync(self.consumer.new_message)(event)
        return sent

    def test_ws_posted_self_message_reaches_other_device(self):
        # alice sent this from her FIRST device (origin_channel != this channel).
        # Her second device (this consumer) MUST still receive it live.
        msg = self._send(sender_id=self.alice.id, content='from my phone')
        sent = self._deliver({
            'origin_channel': 'alice.other.device',
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.alice.id,
                'content': msg.content,
            },
        })
        self.assertEqual([f['type'] for f in sent], ['new_message'])

    def test_ws_posted_self_message_suppressed_on_originating_device(self):
        # The originating device (origin_channel == this channel) is still suppressed.
        msg = self._send(sender_id=self.alice.id, content='echo to self')
        sent = self._deliver({
            'origin_channel': self.consumer.channel_name,
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.alice.id,
                'content': msg.content,
            },
        })
        self.assertEqual(sent, [])

    def test_http_posted_self_echo_still_suppressed(self):
        # No origin_channel (HTTP/agent path) → self-echo stays suppressed (CON-106).
        msg = self._send(sender_id=self.alice.id, content='agent self echo')
        sent = self._deliver({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.alice.id,
                'content': msg.content,
            },
        })
        self.assertEqual(sent, [])

    def test_voice_turn_self_stt_reaches_caller(self):
        # Voice persist is HTTP but the speaker never optimistic-inserted.
        # origin_channel=voice-turn takes the PLAT-965 path so Home history
        # shows the caller's own STT (not only the agent's).
        msg = self._send(sender_id=self.alice.id, content='Can you hear me?')
        sent = self._deliver({
            'origin_channel': 'voice-turn',
            'skip_agent_notify': True,
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.alice.id,
                'content': msg.content,
            },
        })
        self.assertEqual([frame['type'] for frame in sent], ['new_message'])
        self.assertEqual(sent[0]['message']['content'], 'Can you hear me?')

    def test_other_users_message_always_delivered(self):
        msg = self._send(sender_id=self.bob.id, content='hi alice')
        sent = self._deliver({
            'message_data': {
                'id': str(msg.id),
                'conversation_id': str(self.conv.id),
                'sender_id': self.bob.id,
                'content': msg.content,
            },
        })
        self.assertEqual([f['type'] for f in sent], ['new_message'])

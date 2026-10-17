"""HIVE-628/PLAT-4490 Connect state for Hive lifecycle decisions."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from connect.models import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    MessageDelivery,
)


class InternalUnreadRecipientsTests(TestCase):
    URL = '/api/internal/unread-recipients/'

    def _mk(self, user_id, email, unread, has_left=False):
        conv = Conversation.objects.create(conversation_type='direct')
        participant = ConversationParticipant.objects.create(
            conversation=conv, user_id=user_id, user_email=email,
            unread_count=unread, has_left=has_left,
        )
        return conv, participant

    def _message(self, conv, sender_id, created_at):
        message = DirectMessage.objects.create(
            conversation=conv,
            sender_id=sender_id,
            sender_email=f'user-{sender_id}@shizuha.com',
            content='test message',
        )
        DirectMessage.objects.filter(pk=message.pk).update(created_at=created_at)
        return message

    def test_forbidden_without_internal_service_header(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 403)

    def test_unknown_service_forbidden(self):
        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='random-service')
        self.assertEqual(resp.status_code, 403)

    def test_aggregates_unread_per_user_for_hive(self):
        self._mk(13, 'kai@shizuha.com', 3)
        self._mk(13, 'kai@shizuha.com', 2)
        self._mk(4, 'sara@shizuha.com', 1)
        self._mk(4, 'sara@shizuha.com', 0)          # read — contributes zero
        self._mk(84, 'revi@shizuha.com', 5, True)   # left — excluded
        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='shizuha-hive')
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()['complete'], True)
        self.assertIs(resp.json()['lifecycle_delivery_complete'], True)
        rows = {r['user_id']: r for r in resp.json()['recipients']}
        self.assertEqual(rows[13]['unread_total'], 5)
        self.assertEqual(rows[13]['conversations'], 2)
        self.assertEqual(rows[4]['unread_total'], 1)
        self.assertNotIn(84, rows)

    def test_lifecycle_pending_counts_delivery_not_unread_badge(self):
        conv, _ = self._mk(13, 'kai@shizuha.com', 7)
        delivered = self._message(conv, sender_id=4, created_at=timezone.now())
        MessageDelivery.objects.create(message=delivered, user_id=13)
        self._message(conv, sender_id=4, created_at=timezone.now())

        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='shizuha-hive')

        row = {r['user_id']: r for r in resp.json()['recipients']}[13]
        self.assertEqual(row['unread_total'], 7)
        self.assertEqual(row['pending_delivery_total'], 1)

    def test_lifecycle_pending_excludes_messages_outside_agent_replay_window(self):
        conv, _ = self._mk(13, 'kai@shizuha.com', 1)
        self._message(
            conv,
            sender_id=4,
            created_at=timezone.now() - timedelta(minutes=16),
        )

        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='shizuha-hive')

        row = {r['user_id']: r for r in resp.json()['recipients']}[13]
        self.assertEqual(row['unread_total'], 1)
        self.assertEqual(row['pending_delivery_total'], 0)

    def test_reports_latest_inbound_dm_after_recipient_reads_it(self):
        conv, _ = self._mk(13, 'kai@shizuha.com', 0)
        old = timezone.now() - timedelta(minutes=10)
        latest_inbound = timezone.now() - timedelta(minutes=2)
        later_outbound = timezone.now() - timedelta(minutes=1)
        self._message(conv, sender_id=4, created_at=old)
        self._message(conv, sender_id=4, created_at=latest_inbound)
        self._message(conv, sender_id=13, created_at=later_outbound)

        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='shizuha-hive')

        self.assertEqual(resp.status_code, 200)
        rows = {r['user_id']: r for r in resp.json()['recipients']}
        self.assertEqual(rows[13]['unread_total'], 0)
        self.assertEqual(
            rows[13]['last_dm_at'],
            latest_inbound.isoformat().replace('+00:00', 'Z'),
        )

    def test_snapshot_is_complete_beyond_previous_500_recipient_cap(self):
        """A protected fleet recipient beyond the old top-500 slice cannot be
        omitted and misread by Hive as unread=0/no-recent-DM."""
        conversations = [Conversation(conversation_type='direct') for _ in range(501)]
        Conversation.objects.bulk_create(conversations)
        participants = [
            ConversationParticipant(
                conversation=conversation,
                user_id=index,
                user_email=f'agent-{index}@shizuha.com',
                unread_count=1 if index < 501 else 0,
            )
            for index, conversation in enumerate(conversations, start=1)
        ]
        ConversationParticipant.objects.bulk_create(participants)

        resp = self.client.get(self.URL, HTTP_X_INTERNAL_SERVICE='shizuha-hive')

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIs(payload['complete'], True)
        self.assertEqual(len(payload['recipients']), 501)
        rows = {row['user_id']: row for row in payload['recipients']}
        self.assertIn(501, rows)
        self.assertEqual(rows[501]['user_email'], 'agent-501@shizuha.com')

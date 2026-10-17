"""CON-425 S1: bulk mark-as-read + unread triage filter.

The System conversation accumulates unbounded unread notifications with no
bulk triage affordance. These tests pin the read-side core:
  - POST /api/conversations/{id}/mark-read  (mark all, or older_than cutoff)
  - GET  /api/conversations/?unread=true    (unread-only triage view)

Semantics mirror ack_message type=read: monotonic cursor advance, MessageRead
receipts for covered non-self messages, unread_count recomputed under lock.
"""

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from connect.models import Conversation, ConversationParticipant, DirectMessage, MessageRead

from .test_messaging import ChannelMockMixin

User = get_user_model()


class BulkMarkReadTest(ChannelMockMixin, APITestCase):
    """POST /api/conversations/{id}/mark-read — CON-425 S1."""

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
            last_message_at=timezone.now(), message_count=0,
        )
        self.p_alice = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob Jones',
        )
        self.client.force_authenticate(user=self.alice)
        # APPEND_SLASH is False in settings; the DefaultRouter route carries a
        # trailing slash, so the URL must too (run 736: 7x "404 != 200").
        self.url = f'/api/conversations/{self.conv.id}/mark-read/'

    def _msg(self, i, *, sender=None, created_at=None, event_id=None):
        m = DirectMessage.objects.create(
            conversation=self.conv, sender_id=sender or self.bob.id,
            content=f'msg {i}', event_id=event_id,
        )
        # created_at is auto_now_add — the create() kwarg is silently ignored,
        # so backdate via queryset update (bypasses auto_now_add). Run 736:
        # every message looked brand-new, the older_than covered set was empty.
        if created_at is not None:
            DirectMessage.objects.filter(pk=m.pk).update(created_at=created_at)
            m.refresh_from_db()
        return m

    def test_marks_all_read_and_zeroes_unread(self):
        now = timezone.now()
        for i in range(5):
            self._msg(i, event_id=100 + i, created_at=now - timedelta_minutes(5 - i))
        self.p_alice.unread_count = 5
        self.p_alice.save(update_fields=['unread_count'])

        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'ok')
        self.assertTrue(resp.data['bulk'])
        self.assertEqual(resp.data['marked_read'], 5)
        self.assertEqual(resp.data['unread_count'], 0)

        self.p_alice.refresh_from_db()
        self.assertEqual(self.p_alice.unread_count, 0)
        self.assertEqual(self.p_alice.last_read_event_id, 104)
        self.assertEqual(
            MessageRead.objects.filter(user_id=self.alice.id, message__conversation=self.conv).count(),
            5,
        )

    def test_older_than_cutoff_leaves_newer_unread(self):
        now = timezone.now()
        old1 = self._msg(0, event_id=100, created_at=now - timedelta_minutes(30))
        old2 = self._msg(1, event_id=101, created_at=now - timedelta_minutes(20))
        new1 = self._msg(2, event_id=102, created_at=now - timedelta_minutes(1))
        self.p_alice.unread_count = 3
        self.p_alice.save(update_fields=['unread_count'])

        resp = self.client.post(
            self.url, {'older_than': (now - timedelta_minutes(10)).isoformat()}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 2)
        self.assertEqual(resp.data['unread_count'], 1)

        self.p_alice.refresh_from_db()
        self.assertEqual(self.p_alice.last_read_event_id, 101)
        self.assertEqual(self.p_alice.unread_count, 1)
        read_ids = set(MessageRead.objects.filter(user_id=self.alice.id).values_list('message_id', flat=True))
        self.assertEqual(read_ids, {old1.id, old2.id})
        self.assertNotIn(new1.id, read_ids)

    def test_cursor_never_regresses(self):
        now = timezone.now()
        self._msg(0, event_id=100, created_at=now - timedelta_minutes(30))
        self._msg(1, event_id=101, created_at=now - timedelta_minutes(20))
        # Simulate the client having already read past event 101.
        self.p_alice.last_read_event_id = 101
        self.p_alice.unread_count = 0
        self.p_alice.save(update_fields=['last_read_event_id', 'unread_count'])

        resp = self.client.post(
            self.url, {'older_than': (now - timedelta_minutes(25)).isoformat()}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 0)
        self.p_alice.refresh_from_db()
        self.assertEqual(self.p_alice.last_read_event_id, 101)
        self.assertEqual(self.p_alice.unread_count, 0)

    def test_own_messages_are_not_marked_or_counted(self):
        now = timezone.now()
        own = self._msg(0, sender=self.alice.id, event_id=100, created_at=now - timedelta_minutes(5))
        theirs = self._msg(1, event_id=101, created_at=now - timedelta_minutes(4))

        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Cursor advances to the latest covered non-self message.
        self.assertEqual(resp.data['last_read_event_id'], 101)
        self.assertEqual(resp.data['marked_read'], 1)
        self.assertFalse(MessageRead.objects.filter(message_id=own.id, user_id=self.alice.id).exists())
        self.assertTrue(MessageRead.objects.filter(message_id=theirs.id, user_id=self.alice.id).exists())

    def test_foreign_conversation_is_existence_hidden(self):
        # get_queryset narrows to the caller's visible conversations
        # (privacy.py:311 filters has_left=False), so BOTH a never-participant
        # and a participant who LEFT get 404 from get_object before the
        # explicit 403 checks — existence hiding (run 740: live behavior).
        # The view's explicit 403 paths stay as defense-in-depth for
        # non-standard entry paths.
        mallory = User.objects.create_user(
            username='mallory', email='mallory@example.com', password='pass123',
        )
        self.client.force_authenticate(user=mallory)
        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        leaver = User.objects.create_user(
            username='leaver', email='leaver@example.com', password='pass123',
        )
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=leaver.id,
            user_email=leaver.email, user_name='Lea Ver', has_left=True,
        )
        self.client.force_authenticate(user=leaver)
        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_older_than_gets_400(self):
        resp = self.client.post(self.url, {'older_than': 'not-a-timestamp'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_conversation_is_idempotent_ok(self):
        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['marked_read'], 0)
        self.assertIsNone(resp.data['last_read_event_id'])


def timedelta_minutes(n):
    from datetime import timedelta
    return timedelta(minutes=n)


class UnreadTriageFilterTest(ChannelMockMixin, APITestCase):
    """GET /api/conversations/?unread=true — CON-425 S1 triage view."""

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
            first_name='Alice', last_name='Smith',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
            first_name='Bob', last_name='Jones',
        )
        self.client.force_authenticate(user=self.alice)

        def _conv(unread):
            c = Conversation.objects.create(
                conversation_type='direct', created_by_id=self.alice.id,
                last_message_at=timezone.now(), message_count=1,
            )
            ConversationParticipant.objects.create(
                conversation=c, user_id=self.alice.id,
                user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
                unread_count=unread,
            )
            ConversationParticipant.objects.create(
                conversation=c, user_id=self.bob.id,
                user_email=self.bob.email, user_name='Bob Jones',
            )
            return c

        self.conv_unread = _conv(3)
        self.conv_read = _conv(0)

    def test_unread_filter_returns_only_unread(self):
        resp = self.client.get('/api/conversations/', {'unread': 'true'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data.get('conversations', []))
        # ids are serializer strings; conv ids are UUID objects — compare as
        # strings (run 736: "UUID(...) not found in {'...'}" despite equality).
        ids = {str(c['id']) for c in results}
        self.assertIn(str(self.conv_unread.id), ids)
        self.assertNotIn(str(self.conv_read.id), ids)

    def test_no_filter_returns_both(self):
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data.get('conversations', []))
        ids = {str(c['id']) for c in results}
        self.assertIn(str(self.conv_unread.id), ids)
        self.assertIn(str(self.conv_read.id), ids)

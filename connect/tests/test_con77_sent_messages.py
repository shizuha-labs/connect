"""
CON-77: GET /api/messaging/sent/ — an agent's own outbox.

Lets agents detect whether they already sent an equivalent message (e.g.
replying to the same standup ping multiple times across heartbeats / session
restarts) without tracking message IDs externally.

Acceptance:
- returns the caller's last N sent DMs, newest-first (default 20)
- filterable by recipient_username and/or since
- exposes only the caller's own sent messages (no other user's content)
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from connect.models import Conversation, ConversationParticipant, DirectMessage

User = get_user_model()

URL = '/api/messaging/sent/'
# shizuha-id is mocked: username<->id resolution without the live service.
_USERNAME_TO_ID = {'bob': 200, 'carol': 300}
_ID_TO_USERNAME = {200: 'bob', 300: 'carol'}


def _fake_id_by_username(username):
    return _USERNAME_TO_ID.get(username)


def _fake_usernames_by_ids(ids):
    return {i: _ID_TO_USERNAME.get(i, '') for i in ids}


@patch('connect.views.messaging._resolve_usernames_by_ids', side_effect=_fake_usernames_by_ids)
@patch('connect.views.messaging._resolve_user_id_by_username', side_effect=_fake_id_by_username)
class SentMessagesTests(APITestCase):
    def setUp(self):
        self.me = User.objects.create_user(username='ryo-con77', email='ryo-con77@example.com')
        self.client.force_authenticate(user=self.me)
        self.now = timezone.now()
        self.cb = self._conv(200, 'bob')
        self.cc = self._conv(300, 'carol')
        self._dm(self.cb, 'old to bob', self.now - timedelta(hours=3))
        self._dm(self.cb, 'newest to bob', self.now - timedelta(minutes=2))
        self._dm(self.cc, 'hi carol', self.now - timedelta(hours=1))
        # A message someone ELSE sent in my conversation — must never appear.
        self._dm(self.cb, 'reply from bob', self.now - timedelta(minutes=1), sender=200)

    def _conv(self, other_id, other_name):
        c = Conversation.objects.create()
        ConversationParticipant.objects.create(
            conversation=c, user_id=self.me.id, user_email=self.me.email, user_name='Ryo')
        ConversationParticipant.objects.create(
            conversation=c, user_id=other_id, user_email=f'{other_name}@x.com', user_name=other_name.title())
        return c

    def _dm(self, conv, content, when, sender=None):
        m = DirectMessage.objects.create(
            conversation=conv, sender_id=sender or self.me.id,
            sender_email=self.me.email, content=content)
        DirectMessage.objects.filter(pk=m.pk).update(created_at=when)
        return m

    def test_outbox_newest_first_only_own(self, *_):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        contents = [m['content_preview'] for m in resp.data['messages']]
        self.assertNotIn('reply from bob', contents)  # other user's message excluded
        self.assertEqual(contents[0], 'newest to bob')  # newest-first
        self.assertEqual(set(contents), {'old to bob', 'newest to bob', 'hi carol'})

    def test_recipient_fields_resolved(self, *_):
        resp = self.client.get(URL)
        bob_msg = next(m for m in resp.data['messages'] if m['content_preview'] == 'newest to bob')
        self.assertEqual(bob_msg['recipient_user_id'], 200)
        self.assertEqual(bob_msg['recipient_username'], 'bob')
        self.assertTrue(bob_msg['message_id'] and bob_msg['conversation_id'] and bob_msg['sent_at'])

    def test_limit(self, *_):
        resp = self.client.get(URL, {'limit': 1})
        self.assertEqual(len(resp.data['messages']), 1)
        self.assertEqual(resp.data['messages'][0]['content_preview'], 'newest to bob')

    def test_since_filter(self, *_):
        resp = self.client.get(URL, {'since': (self.now - timedelta(minutes=5)).isoformat()})
        self.assertEqual([m['content_preview'] for m in resp.data['messages']], ['newest to bob'])

    def test_recipient_username_filter(self, *_):
        resp = self.client.get(URL, {'recipient_username': 'bob'})
        self.assertEqual(
            {m['content_preview'] for m in resp.data['messages']},
            {'old to bob', 'newest to bob'},
        )

    def test_unknown_recipient_username_404(self, *_):
        resp = self.client.get(URL, {'recipient_username': 'nobody'})
        self.assertEqual(resp.status_code, 404)

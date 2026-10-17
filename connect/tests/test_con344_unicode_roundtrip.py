"""CON-344 regression — message composer Unicode + newline round-trip.

Reproduces the reported corruption: a valid supplementary-plane emoji (U+1F9EA
TEST TUBE) and a typed newline must round-trip browser/API → storage → API
without replacement characters or silent newline collapse.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from connect.models import Conversation, ConversationParticipant, DirectMessage

User = get_user_model()

PROBE = (
    'CON-334 live QA probe — Unicode \U0001F9EA newline follows:\n'
    'second line; symbols <> & " \' — 2026-08-12T06:06Z'
)


class MessageUnicodeRoundTripTests(APITestCase):
    """Supplementary-plane Unicode + newline survive persistence and read-back."""

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
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def test_emoji_and_newline_round_trip_through_api(self):
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': PROBE},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIn('\U0001F9EA', resp.data['content'])
        self.assertNotIn('\ufffd', resp.data['content'])
        self.assertIn('\n', resp.data['content'])

        # Read back through the API — byte-faithful.
        read = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()['messages'][0]['content'], PROBE)

        # Persisted row is byte-faithful.
        msg = DirectMessage.objects.get(conversation=self.conv)
        self.assertEqual(msg.content, PROBE)
        self.assertNotIn('\ufffd', msg.content)

    def test_ascii_punctuation_unchanged(self):
        ascii_probe = 'symbols <> & " \' — 2026-08-12T06:06Z'
        resp = self.client.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': ascii_probe},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['content'], ascii_probe)

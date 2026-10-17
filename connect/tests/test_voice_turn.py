from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from connect.models import Conversation, ConversationParticipant, DirectMessage, MessageDelivery


class VoiceTurnViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.human = User(id=101, username='hritik', email='jdoe@example.com', first_name='Hritik')
        self.client.force_authenticate(user=self.human, token={'is_agent': False})
        self.conv = Conversation.objects.create(conversation_type='direct', direct_key='101:698')
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=101, user_name='Hritik',
            user_email='jdoe@example.com', participant_type='human',
        )
        self.agent_id = uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=698, user_name='Hina',
            user_email='hina@shizuha.com', participant_type='agent',
            agent_id=self.agent_id,
        )

    @patch('connect.views.messaging._safe_broadcast')
    def test_persists_both_sides_and_marks_agent_delivered(self, broadcast):
        response = self.client.post(
            '/api/messaging/voice-turn/',
            {
                'conversation_id': str(self.conv.id),
                'user_text': 'Hi, what is on my queue?',
                'assistant_text': 'Queue is clean.',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        texts = list(DirectMessage.objects.filter(conversation=self.conv).values_list('content', 'sender_id', 'sender_is_agent'))
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[0], ('Hi, what is on my queue?', 101, False))
        self.assertEqual(texts[1], ('Queue is clean.', 698, True))
        self.assertEqual(MessageDelivery.objects.filter(user_id=698).count(), 2)
        self.assertTrue(all(args[1]['skip_agent_notify'] for args, _kwargs in broadcast.call_args_list))
        self.assertTrue(all(args[1]['origin_channel'] == 'voice-turn' for args, _kwargs in broadcast.call_args_list))

    @patch('connect.views.messaging._safe_broadcast')
    def test_rejects_human_peer(self, _broadcast):
        other = Conversation.objects.create(conversation_type='direct', direct_key='101:202')
        ConversationParticipant.objects.create(
            conversation=other, user_id=101, user_name='Hritik', participant_type='human',
        )
        ConversationParticipant.objects.create(
            conversation=other, user_id=202, user_name='Friend', participant_type='human',
        )
        response = self.client.post(
            '/api/messaging/voice-turn/',
            {
                'conversation_id': str(other.id),
                'user_text': 'hello',
                'assistant_text': 'hi',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(DirectMessage.objects.filter(conversation=other).count(), 0)

    @patch('connect.views.messaging._safe_broadcast')
    def test_skips_near_duplicate_voice_lines(self, _broadcast):
        first = self.client.post(
            '/api/messaging/voice-turn/',
            {
                'conversation_id': str(self.conv.id),
                'user_text': 'Can you hear me?',
                'assistant_text': 'Yes, I can hear you. What\'s on your mind?',
            },
            format='json',
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            '/api/messaging/voice-turn/',
            {
                'conversation_id': str(self.conv.id),
                'user_text': 'Can you hear me?',
                'assistant_text': 'Yes, I can hear you. What\'s on your mind?',
            },
            format='json',
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json().get('deduped'))
        self.assertEqual(DirectMessage.objects.filter(conversation=self.conv).count(), 2)

    def test_requires_auth(self):
        anon = APIClient()
        response = anon.post('/api/messaging/voice-turn/', {'conversation_id': str(self.conv.id), 'user_text': 'x'}, format='json')
        self.assertIn(response.status_code, (401, 403))

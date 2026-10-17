from unittest.mock import patch

from django.test import TestCase, override_settings

from connect.models import Conversation, ConversationParticipant, DirectMessage
from connect.tasks import post_daily_standup_prompt


class DailyStandupTaskTests(TestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(
            conversation_type='group',
            name='Daily Standup',
        )
        self.poster = ConversationParticipant.objects.create(
            conversation=self.conversation,
            user_id=101,
            user_email='shizuha@shizuha.com',
            user_name='Shizuha',
            participant_type='agent',
            agent_id='11111111-1111-1111-1111-111111111111',
        )
        self.human = ConversationParticipant.objects.create(
            conversation=self.conversation,
            user_id=202,
            user_email='hritik@example.com',
            user_name='Hritik',
            participant_type='human',
        )

    @override_settings(STANDUP_POSTER_EMAIL='shizuha@shizuha.com')
    @patch('connect.tasks.send_dm_notification.delay')
    @patch('connect.views.internal_api._safe_broadcast')
    def test_post_daily_standup_prompt_is_idempotent_per_day(self, _broadcast, _notify):
        with override_settings(STANDUP_CONVERSATION_ID=str(self.conversation.id)):
            first = post_daily_standup_prompt()
            second = post_daily_standup_prompt()

        self.assertIn('posted standup', first)
        self.assertIn('skipped duplicate standup', second)
        self.assertEqual(
            DirectMessage.objects.filter(conversation=self.conversation).count(),
            1,
        )
        message = DirectMessage.objects.get(conversation=self.conversation)
        self.assertIsNotNone(message.client_message_id)

        self.conversation.refresh_from_db()
        self.human.refresh_from_db()
        self.assertEqual(self.conversation.message_count, 1)
        self.assertEqual(self.human.unread_count, 1)
        self.assertEqual(_broadcast.call_count, 1)
        self.assertEqual(_notify.call_count, 1)

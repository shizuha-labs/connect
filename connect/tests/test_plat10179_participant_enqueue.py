"""PLAT-10179: participant profile ensure must not escape a rolled-back transaction."""
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from connect.models import Conversation, ConversationParticipant


class ParticipantEnqueueOnCommitTests(TestCase):
    def test_rollback_does_not_enqueue(self):
        conv = Conversation.objects.create(conversation_type='direct')
        with patch('connect.signals.enqueue_participant_profile') as enqueue:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    ConversationParticipant.objects.create(
                        conversation=conv, user_id=10179,
                    )
                    raise RuntimeError('rollback')
            enqueue.assert_not_called()

    def test_commit_enqueues_after_commit(self):
        conv = Conversation.objects.create(conversation_type='direct')
        with patch('connect.signals.enqueue_participant_profile') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                ConversationParticipant.objects.create(
                    conversation=conv, user_id=10180,
                )
            enqueue.assert_called_once_with(10180)

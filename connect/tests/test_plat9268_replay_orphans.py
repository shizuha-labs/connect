"""PLAT-9268 regression: undelivered messages must not be orphaned by read
markers or by the agent replay window.

The corroborated 09-19 incident: two DMs sent while the recipient's WebSocket
was down were stored with NO delivery row and never replayed, while later
messages delivered fine. Two cutoff mechanisms conspired:

1. ``eligible_candidate_filter`` took ``max(floor, last_read_at, cleared_at)``
   as the per-conversation cutoff — but ``last_read_at`` is a READ marker, not
   a DELIVERY boundary. It advances whenever any newer message is read, which
   permanently excluded older undelivered rows from every future replay.
2. ``_replay_limits_for(is_agent=True)`` bounded the oldest replayable row at
   15 minutes — any WS outage longer than the window silently lost everything
   stored during it (the incident's recipient reconnected ~50 minutes later).

The fix under test: the replay cutoff is driven by the age floor and
``cleared_at`` only (the delivery-receipt exclusion already prevents
re-sending delivered rows), and agents keep their tight row CAP while
getting the human-scale day floor.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from connect.consumers import (
    DEFAULT_AGENT_DM_REPLAY_LIMIT,
    DEFAULT_DM_REPLAY_WINDOW_DAYS,
    _replay_limits_for,
)
from connect.models import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    MessageDelivery,
)
from connect.services.dm_replay import eligible_candidate_queryset

User = get_user_model()


class Plat9268ReplayOrphanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            username='plat9268-alice', email='plat9268-alice@example.com', password='x')
        cls.bob = User.objects.create_user(
            username='plat9268-bob', email='plat9268-bob@example.com', password='x')
        cls.conv = Conversation.objects.create(conversation_type='direct')
        ConversationParticipant.objects.create(
            conversation=cls.conv, user_id=cls.alice.id, user_email=cls.alice.email)
        cls.bob_participant = ConversationParticipant.objects.create(
            conversation=cls.conv, user_id=cls.bob.id, user_email=cls.bob.email)

        now = timezone.now()
        # The orphan: sent ~50 minutes ago, during bob's WS outage — stored,
        # never delivered (no MessageDelivery row).
        cls.orphan = DirectMessage.objects.create(
            conversation=cls.conv,
            sender_id=cls.alice.id,
            sender_email=cls.alice.email,
            sender_name='Alice',
            content='supply sent during the outage',
            created_at=now - timedelta(minutes=50),
        )
        # A newer message that WAS delivered live after bob reconnected.
        cls.delivered = DirectMessage.objects.create(
            conversation=cls.conv,
            sender_id=cls.alice.id,
            sender_email=cls.alice.email,
            sender_name='Alice',
            content='delivered after reconnect',
            created_at=now - timedelta(minutes=5),
        )
        MessageDelivery.objects.create(message=cls.delivered, user_id=cls.bob.id,
                                       delivered_at=now - timedelta(minutes=4))
        # bob READ the newer message — last_read_at advanced PAST the orphan's
        # created_at. Under the old predicate this orphaned the older row.
        cls.bob_participant.last_read_at = now - timedelta(minutes=4)
        cls.bob_participant.save(update_fields=['last_read_at'])

    def _candidates(self, floor):
        participants = ConversationParticipant.objects.filter(
            user_id=self.bob.id, has_left=False)
        return eligible_candidate_queryset(self.bob.id, participants, floor)

    def test_read_marker_does_not_orphan_undelivered_rows(self):
        """The undelivered row is a replay candidate even though a NEWER
        message was read (last_read_at > orphan.created_at)."""
        floor = timezone.now() - timedelta(days=DEFAULT_DM_REPLAY_WINDOW_DAYS)
        ids = {m.id for m in self._candidates(floor)}
        self.assertIn(self.orphan.id, ids)
        self.assertNotIn(self.delivered.id, ids)  # CON-110 exclusion intact

    def test_agent_floor_is_day_scale_not_minutes(self):
        """Agents keep the tight row CAP but the human-scale day floor — a WS
        outage longer than 15 minutes must not silently lose DMs."""
        limit, floor = _replay_limits_for(is_agent=True)
        self.assertEqual(limit, DEFAULT_AGENT_DM_REPLAY_LIMIT)
        expected_floor = timezone.now() - timedelta(days=DEFAULT_DM_REPLAY_WINDOW_DAYS)
        self.assertGreater(floor, expected_floor - timedelta(minutes=1))
        self.assertLess(floor, expected_floor + timedelta(minutes=1))
        # The orphan (50 minutes old) is inside the agent window now.
        ids = {m.id for m in self._candidates(floor)}
        self.assertIn(self.orphan.id, ids)

    def test_cleared_conversation_still_suppresses_replay(self):
        """cleared_at remains a legitimate replay suppressor."""
        self.bob_participant.cleared_at = timezone.now()
        self.bob_participant.save(update_fields=['cleared_at'])
        try:
            ids = {m.id for m in self._candidates(
                timezone.now() - timedelta(days=DEFAULT_DM_REPLAY_WINDOW_DAYS))}
            self.assertNotIn(self.orphan.id, ids)
        finally:
            self.bob_participant.cleared_at = None
            self.bob_participant.save(update_fields=['cleared_at'])

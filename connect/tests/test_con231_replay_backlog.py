"""CON-231: a capped reconnect must still make progress through the backlog.

The regression this guards is the one the acceptance names: with more eligible
undelivered DMs than the cap, repeated reconnects must drain the backlog and
leave no permanently-skipped row. The pre-existing SQL ordering already excludes
delivered rows before the cap; these tests pin that behaviour against a >cap
backlog spanning multiple conversations, and pin the attribution added here.
"""
from __future__ import annotations

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from connect.models import Conversation, ConversationParticipant, DirectMessage
from connect.services.dm_replay import eligible_candidate_queryset
from connect.tests.test_userchat_missed_messages import _make_consumer_for

User = get_user_model()

# Small cap keeps the fixture honest AND fast: the property under test is
# "backlog > cap drains across reconnects", which does not depend on 200.
TEST_CAP = 5


class Con231BacklogDrainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            username='con231-alice', email='con231-alice@example.com', password='x')
        cls.bob = User.objects.create_user(
            username='con231-bob', email='con231-bob@example.com', password='x')
        cls.convs = []
        for i in range(3):
            conv = Conversation.objects.create(conversation_type='direct')
            ConversationParticipant.objects.create(
                conversation=conv, user_id=cls.alice.id, user_email=cls.alice.email)
            ConversationParticipant.objects.create(
                conversation=conv, user_id=cls.bob.id, user_email=cls.bob.email)
            cls.convs.append(conv)

        # 12 eligible undelivered DMs spread across 3 conversations — comfortably
        # over TEST_CAP so several reconnects are required.
        cls.total = 12
        for i in range(cls.total):
            DirectMessage.objects.create(
                conversation=cls.convs[i % len(cls.convs)],
                sender_id=cls.bob.id,
                sender_email=cls.bob.email,
                sender_name='Bob',
                content=f'backlog {i}',
            )

    def _participants(self):
        return list(
            ConversationParticipant.objects.filter(user_id=self.alice.id, has_left=False)
            .only('conversation_id', 'last_read_at', 'cleared_at')
        )

    def _eligible(self):
        floor = timezone.now() - timezone.timedelta(days=7)
        return eligible_candidate_queryset(self.alice.id, self._participants(), floor)

    def test_backlog_exceeds_cap_and_spans_conversations(self):
        qs = self._eligible()
        self.assertEqual(qs.count(), self.total)
        self.assertGreater(qs.count(), TEST_CAP)
        self.assertGreater(
            len({m.conversation_id for m in qs}), 1,
            'fixture must span multiple conversations, like the live report',
        )

    def test_repeated_reconnects_drain_the_backlog_with_no_row_skipped(self):
        """Each pass delivers the newest CAP rows; delivered rows are excluded
        before the next cap, so older rows are reached instead of being starved."""
        from connect.models import MessageDelivery

        seen = []
        passes = 0
        while self._eligible().exists():
            passes += 1
            self.assertLessEqual(passes, 10, 'drain is not converging')
            batch = list(self._eligible()[:TEST_CAP])
            self.assertTrue(batch)
            for msg in batch:
                MessageDelivery.objects.create(message=msg, user_id=self.alice.id)
                seen.append(msg.id)

        # Every row reached exactly once — nothing skipped, nothing duplicated.
        self.assertEqual(len(seen), self.total)
        self.assertEqual(len(set(seen)), self.total)
        # And it took more than one reconnect, i.e. the cap really did bind.
        self.assertGreater(passes, 1)

    def test_probe_reports_the_over_cap_user(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        with patch('connect.consumers._replay_limits_for',
                   return_value=(TEST_CAP, timezone.now() - timezone.timedelta(days=7))):
            call_command('dm_replay_backlog', '--user', str(self.alice.id), '--json', stdout=out)

        payload = out.getvalue().strip()
        self.assertTrue(payload, 'probe reported nothing for a user over the cap')
        self.assertIn('"eligible_undelivered": 12', payload)
        self.assertIn('"candidate_limit": 5', payload)
        self.assertIn('"over_cap_by": 7', payload)
        # Attribution the operator needs, without high-cardinality ids in metrics.
        self.assertIn('"conversations": 3', payload)
        self.assertIn('"oldest_candidate_age_s"', payload)



class Con231CapLogOrderingTests(TransactionTestCase):
    """CON-231: the batch's oldest_at/newest_at must not invert.

    ⚠️ This drives the REAL builder, `_fetch_missed_messages`. An earlier
    version of this test constructed `MissedMessageBatch` directly with
    hand-supplied timestamps -- it passed identically with the assignment
    correct AND swapped, because the construction under test was never
    executed. Do not "simplify" it back to that shape.

    The reason a guard is warranted at all: the candidate queryset ends
    `.order_by('-created_at')` (descending), but `missed` is re-sorted
    ASCENDING before construction, so reading the queryset's ordering alone
    suggests an inversion that isn't there. That misreading has already been
    proposed once in review; this test is what settles it either way.
    """

    def setUp(self):
        # TransactionTestCase: the async builder opens its own connection, which
        # a transaction-wrapped TestCase closes out from under it
        # ("InterfaceError: connection already closed").
        self.alice = User.objects.create_user(
            username='con231-ord', email='con231-ord@example.com', password='x')
        self.bob = User.objects.create_user(
            username='con231-ord-bob', email='con231-ord-bob@example.com', password='x')
        self.conv = Conversation.objects.create(conversation_type='direct')
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.alice.id, user_email=self.alice.email)
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=self.bob.id, user_email=self.bob.email)
        # Spread ages widely so an inversion is unmistakable, not a rounding tie.
        now = timezone.now()
        for days in (6, 3, 0):
            m = DirectMessage.objects.create(
                conversation=self.conv, sender_id=self.bob.id,
                sender_email=self.bob.email, sender_name='Bob',
                content=f'aged {days}d',
            )
            DirectMessage.objects.filter(pk=m.pk).update(
                created_at=now - timezone.timedelta(days=days))

    def test_batch_oldest_is_not_newer_than_batch_newest(self):
        consumer = _make_consumer_for(self.alice)
        with patch('connect.consumers._replay_limits_for',
                   return_value=(2, timezone.now() - timezone.timedelta(days=7))):
            batch = async_to_sync(consumer._fetch_missed_messages)()

        self.assertTrue(batch.capped, 'fixture must exceed the cap or this asserts nothing')
        self.assertIsNotNone(batch.oldest_at)
        self.assertIsNotNone(batch.newest_at)
        self.assertLessEqual(
            batch.oldest_at, batch.newest_at,
            f'oldest_at ({batch.oldest_at}) is NEWER than newest_at '
            f'({batch.newest_at}) -- the two are swapped, so '
            'oldest_candidate_age_s reports the newest row and a long backlog '
            'tail reads fresh.',
        )

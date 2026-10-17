"""PLAT-1278: Connect invariant metrics tests."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from connect.invariants import (
    collect_connect_invariant_metrics,
    render_invariant_metrics,
)
from connect.models.messaging import (
    BridgeOutboundEvent,
    Conversation,
    ConversationParticipant,
    PresenceSession,
)


def _direct_conversation(user_a, user_b, *, last_message_at=None, key=None):
    conversation = Conversation.objects.create(
        conversation_type='direct',
        direct_key=key or f'direct:{user_a}:{user_b}',
        created_by_id=user_a,
        last_message_at=last_message_at or timezone.now(),
    )
    for user_id in (user_a, user_b):
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=user_id,
            participant_type='human',
        )
    return conversation


def _presence(user_id, org_id, *, is_agent=False, status='online', heartbeat=None):
    return PresenceSession.objects.create(
        user_id=user_id,
        session_key=f'sess-{user_id}-{org_id}-{status}',
        organization_id=org_id,
        is_agent=is_agent,
        status=status,
        last_heartbeat_at=heartbeat or timezone.now(),
    )


class CrossOrgConversationTests(TestCase):
    def test_shared_org_pair_is_clean(self):
        _presence(1, 10)
        _presence(2, 10)
        _direct_conversation(1, 2)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.cross_org_conversations, 0)
        self.assertEqual(metrics.cross_org_unknown, 0)

    def test_disjoint_org_pair_is_flagged(self):
        _presence(1, 10)
        _presence(2, 20)
        _direct_conversation(1, 2)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.cross_org_conversations, 1)

    def test_multi_org_user_sharing_one_org_is_clean(self):
        _presence(1, 10)
        _presence(1, 20)
        _presence(2, 20)
        _direct_conversation(1, 2)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.cross_org_conversations, 0)

    def test_unknown_org_pair_is_counted_separately(self):
        _presence(1, 10)
        _direct_conversation(1, 2)  # user 2 has no presence org data
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.cross_org_conversations, 0)
        self.assertEqual(metrics.cross_org_unknown, 1)

    def test_stale_conversation_is_ignored(self):
        _presence(1, 10)
        _presence(2, 20)
        _direct_conversation(1, 2, last_message_at=timezone.now() - timedelta(days=3))
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.cross_org_conversations, 0)
        self.assertEqual(metrics.cross_org_unknown, 0)


class BridgeBacklogTests(TestCase):
    def _event(self, status, age):
        event = BridgeOutboundEvent.objects.create(
            provider='whatsapp',
            provider_account_id='acct',
            channel_key='chan',
            task_key='T-1',
            agent_id=1,
            recipient_external_id='r',
            idempotency_key=f'k-{status}-{age}',
            request_hash='h',
            body_hash='b',
            status=status,
        )
        BridgeOutboundEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(seconds=age)
        )
        return event

    def test_fresh_pending_is_within_grace(self):
        self._event(BridgeOutboundEvent.Status.PENDING, age=60)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.bridge_outbound_stuck, 0)

    def test_old_pending_is_stuck_with_age(self):
        self._event(BridgeOutboundEvent.Status.PENDING, age=3600)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.bridge_outbound_stuck, 1)
        self.assertGreaterEqual(metrics.bridge_outbound_oldest_age_seconds, 3500)

    def test_terminal_states_are_not_stuck(self):
        self._event(BridgeOutboundEvent.Status.DELIVERED, age=3600)
        self._event(BridgeOutboundEvent.Status.FAILED, age=3600)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.bridge_outbound_stuck, 0)


class AgentUnreadBacklogTests(TestCase):
    def _agent_participant(self, user_id, unread, *, last_read_age=timedelta(hours=1)):
        conversation = Conversation.objects.create(
            conversation_type='direct',
            direct_key=f'direct:agent:{user_id}',
            created_by_id=user_id,
        )
        return ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=user_id,
            participant_type='agent',
            unread_count=unread,
            last_read_at=timezone.now() - last_read_age,
        )

    def test_connected_agent_with_aging_backlog_is_flagged(self):
        _presence(7, 1, is_agent=True)
        self._agent_participant(7, unread=30)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.agent_unread_backlog, 1)

    def test_offline_agent_backlog_is_not_a_dead_consumer(self):
        _presence(7, 1, is_agent=True, heartbeat=timezone.now() - timedelta(hours=2))
        self._agent_participant(7, unread=30)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.agent_unread_backlog, 0)

    def test_recently_read_backlog_is_not_flagged(self):
        _presence(7, 1, is_agent=True)
        self._agent_participant(7, unread=30, last_read_age=timedelta(minutes=5))
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.agent_unread_backlog, 0)

    def test_small_backlog_is_not_flagged(self):
        _presence(7, 1, is_agent=True)
        self._agent_participant(7, unread=3)
        metrics = collect_connect_invariant_metrics()
        self.assertEqual(metrics.agent_unread_backlog, 0)


class RenderAndEndpointTests(TestCase):
    def test_render_exposition_format(self):
        body = render_invariant_metrics(collect_connect_invariant_metrics())
        self.assertIn('connect_dm_cross_org_conversations 0', body)
        self.assertIn('connect_agent_unread_backlog 0', body)
        self.assertIn('connect_invariant_probe_errors 0', body)
        self.assertTrue(body.endswith('\n'))

    def test_metrics_endpoint_includes_invariants(self):
        resp = self.client.get('/metrics')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'connect_dm_cross_org_conversations', resp.content)

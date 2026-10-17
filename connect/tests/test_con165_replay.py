"""CON-165 replay, idempotency, and duplicate-storm controls."""

from unittest.mock import Mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from connect.metrics import render_prometheus_metrics
from connect.models import BridgeChannelPolicy, BridgeOutboundEvent
from connect.services.bridge import BridgeOutboundEnvelope, send_outbound_bridge_message
from connect.services.replay import ReplayScope, replay_outbound_bridge_events


@override_settings(
    CONNECT_BRIDGE_DUPLICATE_STORM_THRESHOLD=2,
    CONNECT_BRIDGE_DUPLICATE_STORM_WINDOW_SECONDS=60,
    CONNECT_BRIDGE_REPLAY_MAX_BATCH_SIZE=10,
)
class BridgeReplayAndDedupeTests(TestCase):
    def setUp(self):
        self.agent = User.objects.create_user(username='agent', email='agent@example.com')
        self.account = 'wa-business-1'
        self.channel = 'ops-alerts'
        BridgeChannelPolicy.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key=self.channel,
            task_key='CON-165',
            agent_id=self.agent.id,
            admin_approved_at=timezone.now(),
            allowed_recipients=['15551234567'],
        )

    def _envelope(self, key, *, body='hello', conversation_id=None):
        return BridgeOutboundEnvelope(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key=self.channel,
            task_key='CON-165',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            body=body,
            idempotency_key=key,
            conversation_id=conversation_id,
        )

    def _event(self, key, status, *, body_hash='body-hash', conversation_id=None):
        return BridgeOutboundEvent.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key=self.channel,
            task_key='CON-165',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            conversation_id=conversation_id,
            idempotency_key=key,
            request_hash=f'req-{key}',
            body_hash=body_hash,
            status=status,
            audit_metadata={'body_redacted': True},
        )

    def test_duplicate_storm_suppresses_repeated_outbound_payload_without_provider_send(self):
        first_send = Mock(return_value={'provider_delivery_id': 'wamid.1'})
        second_send = Mock(return_value={'provider_delivery_id': 'wamid.2'})
        third_send = Mock(side_effect=AssertionError('duplicate storm must suppress before provider send'))

        first = send_outbound_bridge_message(self._envelope('dup-1'), provider_send=first_send)
        second = send_outbound_bridge_message(self._envelope('dup-2'), provider_send=second_send)
        third = send_outbound_bridge_message(self._envelope('dup-3'), provider_send=third_send)

        self.assertEqual(first.event.status, BridgeOutboundEvent.Status.ACCEPTED)
        self.assertEqual(second.event.status, BridgeOutboundEvent.Status.ACCEPTED)
        self.assertEqual(third.event.status, BridgeOutboundEvent.Status.RATE_LIMITED)
        self.assertEqual(third.event.error_code, 'duplicate_storm_suppressed')
        self.assertEqual(third.event.audit_metadata['duplicate_storm_count'], 2)
        self.assertEqual(third.event.audit_metadata['duplicate_storm_threshold'], 2)
        self.assertEqual(BridgeOutboundEvent.objects.count(), 3)
        third_send.assert_not_called()

    def test_replay_is_bounded_idempotent_and_does_not_duplicate_delivered_messages(self):
        delivered = self._event('already-delivered', BridgeOutboundEvent.Status.DELIVERED)
        dead = self._event('dead-lettered', BridgeOutboundEvent.Status.DEAD_LETTERED)
        provider_send = Mock(return_value={'provider_delivery_id': 'wamid.replay'})

        dry_run = replay_outbound_bridge_events(
            scope=ReplayScope(provider='whatsapp', channel_key=self.channel),
            actor='kai@shizuha.com',
            task_key='CON-165',
            reason='preflight incident replay',
            batch_size=10,
            dry_run=True,
            provider_send=provider_send,
        )
        self.assertEqual(dry_run.candidates, 1)
        self.assertEqual(dry_run.replayed, 0)
        provider_send.assert_not_called()

        execute = replay_outbound_bridge_events(
            scope=ReplayScope(
                provider='whatsapp',
                channel_key=self.channel,
                statuses=(BridgeOutboundEvent.Status.DEAD_LETTERED, BridgeOutboundEvent.Status.DELIVERED),
            ),
            actor='kai@shizuha.com',
            task_key='CON-165',
            reason='provider outage recovery',
            batch_size=10,
            dry_run=False,
            provider_send=provider_send,
        )

        delivered.refresh_from_db()
        dead.refresh_from_db()
        self.assertEqual(BridgeOutboundEvent.objects.count(), 2)
        self.assertEqual(delivered.status, BridgeOutboundEvent.Status.DELIVERED)
        self.assertEqual(dead.status, BridgeOutboundEvent.Status.ACCEPTED)
        self.assertEqual(dead.provider_delivery_id, 'wamid.replay')
        self.assertEqual(execute.replayed, 1)
        self.assertEqual(execute.skipped, 1)
        provider_send.assert_called_once()
        self.assertEqual(dead.audit_metadata['last_replay_actor'], 'kai@shizuha.com')
        self.assertEqual(dead.audit_metadata['last_replay_task_key'], 'CON-165')
        self.assertEqual(dead.audit_metadata['last_replay_reason'], 'provider outage recovery')

    def test_replay_command_is_dry_run_by_default_and_scoped(self):
        self._event('pending-a', BridgeOutboundEvent.Status.PENDING)
        self._event('pending-other-channel', BridgeOutboundEvent.Status.PENDING)
        BridgeOutboundEvent.objects.filter(idempotency_key='pending-other-channel').update(channel_key='other')

        call_command(
            'replay_bridge_outbound',
            provider='whatsapp',
            channel_key=self.channel,
            actor='kai@shizuha.com',
            task_key='CON-165',
            reason='dry run test',
            batch_size=50,
        )

        self.assertEqual(BridgeOutboundEvent.objects.filter(status=BridgeOutboundEvent.Status.PENDING).count(), 2)

    def test_con165_metrics_are_exported(self):
        body = render_prometheus_metrics().decode()
        self.assertIn('connect_bridge_replay_total', body)
        self.assertIn('connect_duplicate_storm_total', body)

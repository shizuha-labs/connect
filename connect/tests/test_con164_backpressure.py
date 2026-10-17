"""CON-164: bounded backpressure semantics for Connect delivery paths."""

import asyncio
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from connect.consumers import UserChatConsumer
from connect.models import BridgeChannelPolicy, BridgeOutboundEvent
from connect.services.backpressure import QueueState, build_retry_metadata
from connect.services.bridge import BridgeOutboundEnvelope, send_outbound_bridge_message
from connect.invariants import collect_connect_invariant_metrics, render_invariant_metrics


class OutboundBackpressureTests(TestCase):
    def setUp(self):
        self.agent = User.objects.create_user(username='agent', email='agent@example.com')
        self.account = 'wa-business-1'
        BridgeChannelPolicy.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-164',
            agent_id=self.agent.id,
            admin_approved_at=timezone.now(),
            allowed_recipients=['15551234567'],
        )

    def _envelope(self, key='idem-1'):
        return BridgeOutboundEnvelope(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-164',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            body='hello',
            idempotency_key=key,
        )

    def test_provider_outage_records_retrying_state_with_retry_after(self):
        def provider_down(event, envelope):
            raise RuntimeError('provider down')

        result = send_outbound_bridge_message(self._envelope(), provider_send=provider_down)

        self.assertEqual(result.event.status, BridgeOutboundEvent.Status.RETRYING)
        self.assertEqual(result.event.error_code, 'provider_send_retrying')
        self.assertGreater(result.event.audit_metadata['retry_after_seconds'], 0)
        self.assertEqual(result.event.audit_metadata['delivery_state'], 'retrying')

        replay = send_outbound_bridge_message(self._envelope(), provider_send=lambda *_: self.fail('must not resend'))
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.event.status, BridgeOutboundEvent.Status.RETRYING)

    @patch('connect.services.bridge.outbound_queue_state')
    def test_queue_overload_returns_retrying_without_provider_call(self, queue_state):
        queue_state.return_value = QueueState(
            depth=1001,
            oldest_age_seconds=60,
            overloaded=True,
            reason='queue_depth',
        )
        provider_send = lambda *_: self.fail('provider must not be called while local queue is overloaded')

        result = send_outbound_bridge_message(self._envelope('queue-full'), provider_send=provider_send)

        self.assertEqual(result.event.status, BridgeOutboundEvent.Status.RETRYING)
        self.assertEqual(result.event.error_code, 'outbound_queue_queue_depth')
        self.assertEqual(result.event.audit_metadata['queue_backpressure_reason'], 'queue_depth')
        self.assertEqual(result.event.audit_metadata['delivery_state'], 'retrying')

    def test_retrying_and_dead_lettered_states_are_exported_for_alerts(self):
        retrying = BridgeOutboundEvent.objects.create(
            provider='whatsapp', provider_account_id=self.account, channel_key='ops-alerts',
            task_key='CON-164', agent_id=self.agent.id, recipient_external_id='15551234567',
            idempotency_key='retrying-metric', request_hash='r', body_hash='b',
            status=BridgeOutboundEvent.Status.RETRYING,
        )
        BridgeOutboundEvent.objects.filter(pk=retrying.pk).update(created_at=timezone.now() - timezone.timedelta(minutes=7))
        BridgeOutboundEvent.objects.create(
            provider='whatsapp', provider_account_id=self.account, channel_key='ops-alerts',
            task_key='CON-164', agent_id=self.agent.id, recipient_external_id='15551234567',
            idempotency_key='dead-metric', request_hash='r', body_hash='b',
            status=BridgeOutboundEvent.Status.DEAD_LETTERED,
        )

        metrics = collect_connect_invariant_metrics()
        body = render_invariant_metrics(metrics)

        self.assertEqual(metrics.bridge_outbound_retrying, 1)
        self.assertEqual(metrics.bridge_outbound_dead_lettered, 1)
        self.assertIn('connect_bridge_outbound_retrying 1', body)
        self.assertIn('connect_bridge_outbound_dead_lettered 1', body)

    def test_retry_budget_terminally_dead_letters_after_max_attempts(self):
        event = BridgeOutboundEvent.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-164',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            idempotency_key='dead-letter',
            request_hash='r',
            body_hash='b',
            status=BridgeOutboundEvent.Status.RETRYING,
            audit_metadata={'attempts': 2},
        )

        status_value, metadata = build_retry_metadata(event, RuntimeError('still down'))

        self.assertEqual(status_value, BridgeOutboundEvent.Status.DEAD_LETTERED)
        self.assertEqual(metadata['delivery_state'], 'dead_lettered')
        self.assertEqual(metadata['attempts'], 3)


class WebsocketBackpressureTests(TestCase):
    def test_slow_consumer_is_closed_instead_of_buffered_unboundedly(self):
        consumer = UserChatConsumer.__new__(UserChatConsumer)
        consumer.user = type('User', (), {'id': 7})()
        consumer.channel_name = 'chan-test'
        consumer.backpressure_closed = False
        consumer.close = AsyncMock()

        async def slow_send(*args, **kwargs):
            await asyncio.sleep(0.05)

        consumer.send = slow_send

        ok = asyncio.run(consumer._send_bounded({'type': 'new_message'}, timeout=0.001))

        self.assertFalse(ok)
        self.assertTrue(consumer.backpressure_closed)
        consumer.close.assert_awaited_once_with(code=1013)

    def test_persistence_failure_sends_terminal_error_frame(self):
        consumer = UserChatConsumer.__new__(UserChatConsumer)
        consumer._send_bounded = AsyncMock(return_value=True)

        async def no_message(*args, **kwargs):
            return None

        consumer._persist_message = no_message

        asyncio.run(consumer._handle_send_message({
            'conversation_id': 'conv-1',
            'content': 'hello',
            'client_message_id': 'client-1',
        }))

        consumer._send_bounded.assert_awaited_once()
        payload = consumer._send_bounded.call_args.args[0]
        self.assertEqual(payload['type'], 'send_error')
        self.assertEqual(payload['delivery_state'], 'failed_terminal')

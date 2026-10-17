"""CON-163: Connect delivery telemetry and alertable metric surfaces."""

from cryptography.fernet import Fernet
import hmac
import hashlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase
from django.utils import timezone

from connect.metrics import (
    error_class_label,
    render_outbound_queue_metrics,
    render_prometheus_metrics,
)
from connect.models import (
    BridgeChannelPolicy,
    BridgeIdentityMapping,
    BridgeOutboundEvent,
    Conversation,
    ConversationParticipant,
    DirectMessage,
)
from connect.services.bridge import (
    BridgeOutboundEnvelope,
    process_inbound_webhook,
    send_outbound_bridge_message,
    upsert_webhook_secret,
)
from connect.views.messaging import _safe_broadcast


class TelemetryLabelTests(TestCase):
    def test_error_labels_are_bounded_and_stable(self):
        self.assertEqual(error_class_label(IntegrityError('duplicate key')), 'integrity_error')
        self.assertEqual(error_class_label('Provider sent !@# weird'), 'provider_sent_weird')

    def test_metrics_endpoint_exposes_zero_initialized_liveness_series(self):
        body = render_prometheus_metrics().decode()
        self.assertIn('connect_inbound_messages_total', body)
        self.assertIn('connect_message_persistence_failures_total', body)
        self.assertIn('connect_ws_fanout_total', body)
        self.assertIn('connect_ws_fanout_lag_seconds_bucket', body)
        self.assertIn('connect_provider_events_total', body)
        self.assertIn('connect_outbound_queue_depth', body)


class WebsocketFanoutTelemetryTests(TestCase):
    def _layer(self, *, fail=False):
        layer = MagicMock()
        if fail:
            layer.group_send = AsyncMock(side_effect=RuntimeError('redis down'))
        else:
            layer.group_send = AsyncMock()
        return layer

    @patch('connect.views.messaging._publish_chat_event_to_bridge')
    @patch('connect.views.messaging.get_channel_layer')
    @patch('connect.metrics.record_ws_fanout')
    def test_safe_broadcast_records_success_path(self, record, get_layer, _publish):
        get_layer.return_value = self._layer()
        _safe_broadcast('chat_123', {'type': 'new_message'})
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], 'chat_123')
        self.assertEqual(record.call_args.args[1], 'success')
        self.assertGreaterEqual(record.call_args.args[2], 0)

    @patch('connect.views.messaging._publish_chat_event_to_bridge')
    @patch('connect.views.messaging.get_channel_layer')
    @patch('connect.metrics.record_ws_fanout')
    def test_safe_broadcast_records_failure_path(self, record, get_layer, _publish):
        get_layer.return_value = self._layer(fail=True)
        _safe_broadcast('chat_123', {'type': 'new_message'})
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1], 'failure')


@override_settings(
    CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CONNECT_BRIDGE_ADMIN_IDENTITY_FRESHNESS_HOURS=1,
)
class InboundBridgeTelemetryTests(APITestCase):
    def setUp(self):
        self.account = 'wa-business-1'
        self.secret = 'test-webhook-secret'
        upsert_webhook_secret('whatsapp', self.account, self.secret)
        self.user = User.objects.create_user(username='alice', email='alice@example.com')
        BridgeIdentityMapping.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            external_sender_id='15551234567',
            mapped_user_id=self.user.id,
            mapped_user_email=self.user.email,
            mapped_user_name='Alice',
            verified_at=timezone.now(),
        )

    def _raw_and_headers(self, *, event_id='wamid.1', text='hello', signature=True):
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [{
                            'id': event_id,
                            'from': '15551234567',
                            'timestamp': '1710000000',
                            'text': {'body': text},
                            'type': 'text',
                        }],
                    }
                }]
            }]
        }
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        digest = hmac.new(self.secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
        return raw, {'X-Hub-Signature-256': f'sha256={digest if signature else "bad"}'}

    @patch('connect.services.bridge.record_provider_event')
    @patch('connect.services.bridge.record_inbound_message')
    def test_invalid_signature_records_auth_failure_violation(self, inbound, provider):
        raw, headers = self._raw_and_headers(signature=False)
        with self.assertRaises(Exception):
            process_inbound_webhook('whatsapp', self.account, raw, headers)
        inbound.assert_called_with('whatsapp', 'whatsapp', 'auth_failure')
        provider.assert_called_with('whatsapp', 'whatsapp', 'auth_failure', 'failure')
        self.assertEqual(DirectMessage.objects.count(), 0)

    @patch('connect.services.bridge._safe_broadcast')
    @patch('connect.services.bridge.record_inbound_message')
    def test_routed_webhook_records_healthy_inbound_path(self, inbound, _broadcast):
        raw, headers = self._raw_and_headers(event_id='wamid.ok')
        result = process_inbound_webhook('whatsapp', self.account, raw, headers)
        self.assertTrue(result.created)
        self.assertEqual(DirectMessage.objects.count(), 1)
        inbound.assert_any_call('whatsapp', 'whatsapp', 'routed')

    @patch('connect.services.bridge.DirectMessage.objects.create', side_effect=IntegrityError('duplicate key'))
    @patch('connect.services.bridge.record_message_persistence_failure')
    @patch('connect.services.bridge.record_inbound_message')
    def test_persistence_failure_records_violation_path(self, inbound, persist_failure, _create):
        raw, headers = self._raw_and_headers(event_id='wamid.persist-fail')
        with self.assertRaises(IntegrityError):
            process_inbound_webhook('whatsapp', self.account, raw, headers)
        inbound.assert_any_call('whatsapp', 'whatsapp', 'persistence_error')
        persist_failure.assert_called()


class OutboundBridgeTelemetryTests(TestCase):
    def setUp(self):
        self.agent = User.objects.create_user(username='agent', email='agent@example.com')
        self.account = 'wa-business-1'
        BridgeChannelPolicy.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-163',
            agent_id=self.agent.id,
            admin_approved_at=timezone.now(),
            allowed_recipients=['15551234567'],
        )

    def _envelope(self, key='idem-1'):
        return BridgeOutboundEnvelope(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-163',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            body='hello',
            idempotency_key=key,
        )

    @patch('connect.services.bridge.record_provider_event')
    def test_provider_send_records_success_and_failure_paths(self, provider_event):
        send_outbound_bridge_message(
            self._envelope('ok'),
            provider_send=lambda event, envelope: {'provider_delivery_id': 'wamid.ok'},
        )
        provider_event.assert_any_call('whatsapp', 'ops-alerts', 'provider_send', 'accepted')

        def boom(event, envelope):
            raise RuntimeError('provider down')

        send_outbound_bridge_message(self._envelope('fail'), provider_send=boom)
        provider_event.assert_any_call('whatsapp', 'ops-alerts', 'provider_send', 'failure')

    def test_outbound_queue_depth_and_oldest_age_render_healthy_and_violation(self):
        healthy = render_outbound_queue_metrics(now=timezone.now())
        self.assertIn('connect_outbound_queue_depth{provider="none",channel="none"} 0', healthy)

        event = BridgeOutboundEvent.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops-alerts',
            task_key='CON-163',
            agent_id=self.agent.id,
            recipient_external_id='15551234567',
            idempotency_key='pending',
            request_hash='r',
            body_hash='b',
            status=BridgeOutboundEvent.Status.PENDING,
        )
        BridgeOutboundEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        violation = render_outbound_queue_metrics(now=timezone.now())
        self.assertIn('connect_outbound_queue_depth{provider="whatsapp",channel="bridge_policy"} 1', violation)
        self.assertIn('connect_outbound_oldest_pending_age_seconds{provider="whatsapp",channel="bridge_policy"}', violation)

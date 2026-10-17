import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import DatabaseError, close_old_connections
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from connect.models import (
    Block,
    BridgeChannelPolicy,
    BridgeIdentityMapping,
    BridgeOutboundEvent,
    Conversation,
    ConversationParticipant,
    DirectMessage,
    SecurityEventOutbox,
)

User = get_user_model()


class BlockPrivacyPolicyTest(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice-privacy', email='alice-privacy@example.com')
        self.bob = User.objects.create_user(username='bob-privacy', email='bob-privacy@example.com')
        self.conv = Conversation.objects.create(conversation_type='direct', created_by_id=self.alice.id)
        ConversationParticipant.objects.create(conversation=self.conv, user_id=self.alice.id, user_name='Alice')
        ConversationParticipant.objects.create(conversation=self.conv, user_id=self.bob.id, user_name='Bob')
        self.client.force_authenticate(self.alice)

    def test_blocked_conversation_hidden_from_list_and_message_history(self):
        DirectMessage.objects.create(
            conversation=self.conv,
            sender_id=self.bob.id,
            sender_name='Bob',
            content='private blocked message',
        )
        Block.objects.create(blocker_id=self.bob.id, blocked_id=self.alice.id)

        listing = self.client.get('/api/conversations/')
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(str(self.conv.id), {str(item['id']) for item in listing.data})

        history = self.client.get(f'/api/conversations/{self.conv.id}/messages/')
        self.assertIn(history.status_code, {403, 404})

    def test_blocked_user_cannot_be_added_to_group_or_sent_to(self):
        Block.objects.create(blocker_id=self.alice.id, blocked_id=self.bob.id)

        direct = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(direct.status_code, 403)

        group = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'blocked group',
            'participant_ids': [self.bob.id],
        }, format='json')
        self.assertEqual(group.status_code, 403)


class BridgePrivacyPolicyTest(APITestCase):
    def setUp(self):
        self.agent = User.objects.create_user(username='agent-privacy', email='agent-privacy@example.com')
        self.bob = User.objects.create_user(username='bridge-bob', email='bridge-bob@example.com')
        self.account = 'wa-privacy'
        self.policy = BridgeChannelPolicy.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops',
            task_key='CON-18',
            agent_id=self.agent.id,
            admin_approved_at='2026-06-20T00:00:00Z',
            admin_approved_by=1,
            allowed_recipients=['15550000001', '15550000002'],
        )

    def _envelope(self, *, key='bridge-1', recipient='15550000002', conversation_id=None):
        from connect.services.bridge import BridgeOutboundEnvelope
        return BridgeOutboundEnvelope(
            provider='whatsapp',
            provider_account_id=self.account,
            channel_key='ops',
            task_key='CON-18',
            agent_id=self.agent.id,
            recipient_external_id=recipient,
            body='hello',
            idempotency_key=key,
            conversation_id=str(conversation_id) if conversation_id else None,
        )

    def test_bridge_rate_limit_aggregates_provider_ids_by_canonical_user(self):
        from connect.services.bridge import send_outbound_bridge_message

        for external_id in ['15550000001', '15550000002']:
            BridgeIdentityMapping.objects.create(
                provider='whatsapp',
                provider_account_id=self.account,
                external_sender_id=external_id,
                mapped_user_id=self.bob.id,
                mapped_user_email=self.bob.email,
                mapped_user_name='Bob',
            )
        BridgeOutboundEvent.objects.bulk_create([
            BridgeOutboundEvent(
                provider='whatsapp',
                provider_account_id=self.account,
                channel_key='ops',
                task_key='CON-18',
                agent_id=1000 + i,
                recipient_external_id='15550000001',
                idempotency_key=f'hot-{i}',
                request_hash='r' * 64,
                body_hash='b' * 64,
                status=BridgeOutboundEvent.Status.ACCEPTED,
            )
            for i in range(10)
        ])

        result = send_outbound_bridge_message(self._envelope(key='canonical-hot'))
        self.assertEqual(result.event.status, BridgeOutboundEvent.Status.RATE_LIMITED)
        self.assertEqual(result.event.error_code, 'rate_limit_recipient')

    def test_bridge_denies_mapped_blocked_recipient_in_conversation(self):
        from connect.services.bridge import send_outbound_bridge_message

        BridgeIdentityMapping.objects.create(
            provider='whatsapp',
            provider_account_id=self.account,
            external_sender_id='15550000002',
            mapped_user_id=self.bob.id,
            mapped_user_email=self.bob.email,
            mapped_user_name='Bob',
        )
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=self.agent.id)
        ConversationParticipant.objects.create(conversation=conv, user_id=self.agent.id, user_name='Agent')
        ConversationParticipant.objects.create(conversation=conv, user_id=self.bob.id, user_name='Bob')
        Block.objects.create(blocker_id=self.bob.id, blocked_id=self.agent.id)

        result = send_outbound_bridge_message(self._envelope(key='blocked', conversation_id=conv.id))
        self.assertEqual(result.event.status, BridgeOutboundEvent.Status.FORBIDDEN)
        self.assertEqual(result.event.error_code, 'blocked_bridge_recipient')


class SystemMessageOriginTokenTest(APITestCase):
    def _token(self, *, secret, sender_service, recipient_user_id, content, ts=None):
        ts = int(ts or time.time())
        body_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
        sig = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        return f'{ts}:{sig}'

    @override_settings(
        CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET='system-secret',
        CONNECT_SYSTEM_MESSAGE_ALLOWLIST={'pulse'},
        CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS=300,
    )
    def test_system_message_requires_short_lived_signed_origin_token(self):
        system = User.objects.create_user(username='system', email='system@shizuha.com')
        recipient = User.objects.create_user(username='recipient', email='recipient@example.com')
        content = 'scheduler notice'

        missing = self.client.post('/api/internal/system-message/', {
            'recipient_user_id': recipient.id,
            'content': content,
            'sender_service': 'pulse',
        }, HTTP_X_INTERNAL_SERVICE='shizuha-pulse', format='json')
        self.assertEqual(missing.status_code, 403)

        token = self._token(
            secret='system-secret',
            sender_service='pulse',
            recipient_user_id=recipient.id,
            content=content,
        )
        with patch('connect.views.internal_api._get_system_user_id', return_value=system.id):
            accepted = self.client.post('/api/internal/system-message/', {
                'recipient_user_id': recipient.id,
                'content': content,
                'sender_service': 'pulse',
            }, HTTP_X_INTERNAL_SERVICE='shizuha-pulse', HTTP_X_SYSTEM_MESSAGE_TOKEN=token, format='json')
        self.assertEqual(accepted.status_code, 201)

    @override_settings(
        CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET='system-secret',
        CONNECT_SYSTEM_MESSAGE_ALLOWLIST={'forgejo-bridge'},
        CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS=300,
    )
    def test_forgejo_system_message_replay_is_durable_and_conflicts_fail(self):
        system = User.objects.create_user(username='system', email='system@shizuha.com')
        recipient = User.objects.create_user(username='forgejo-recipient', email='forgejo@example.com')
        content = '[Origin] @forgejo-recipient mentioned you in PR #42'
        client_message_id = 'forgejo:delivery-1:shizuha-labs/deploy:issue:created:9001:recipient'

        def post(body_content):
            token = self._token(
                secret='system-secret',
                sender_service='forgejo-bridge',
                recipient_user_id=recipient.id,
                content=body_content,
            )
            return self.client.post('/api/internal/system-message/', {
                'recipient_user_id': recipient.id,
                'content': body_content,
                'sender_name': 'Origin',
                'sender_service': 'forgejo-bridge',
                'client_message_id': client_message_id,
            }, HTTP_X_INTERNAL_SERVICE='forgejo-bridge', HTTP_X_SYSTEM_MESSAGE_TOKEN=token, format='json')

        with patch('connect.views.internal_api._get_system_user_id', return_value=system.id):
            created = post(content)
            replay = post(content)
            conflict = post(content + ' changed')

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.data['idempotent_replay'])
        self.assertEqual(replay.data['message_id'], created.data['message_id'])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            DirectMessage.objects.filter(
                sender_id=system.id,
                content=content,
            ).count(),
            1,
        )


@override_settings(CONNECT_SECURITY_DEDUPE_REDIS_URL='')
class BoundarySecurityEventIngestTest(APITestCase):
    endpoint = '/api/internal/security-events/'

    def setUp(self):
        cache.clear()

    def _event(self, *, event_id=None):
        return {
            'specversion': '1.0',
            'id': event_id or str(uuid.uuid4()),
            'source': 'shizuha://wiki',
            'type': 'platform.boundary_denial',
            'time': '2026-07-10T06:42:15+00:00',
            'data': {
                'service': 'wiki',
                'operation': 'wiki.explicit_org_read',
                'reason': 'cross_tenant_org_scope',
                'actor_user_id': 42,
                'actor_username': 'ren',
                'source_org_ids': [1, 18],
                'target_org_id': 67,
                'method': 'GET',
                'route': 'wiki:search-list',
                'correlation_id': 'probe-plat-4133',
            },
        }

    def _headers(self, payload, *, secret='boundary-secret', service='shizuha-wiki', ts=None):
        ts = int(ts or time.time())
        canonical = json.dumps(
            payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8')
        body_hash = hashlib.sha256(canonical).hexdigest()
        signed = f'{service}|{ts}|{body_hash}'
        signature = hmac.new(
            secret.encode('utf-8'), signed.encode('utf-8'), hashlib.sha256,
        ).hexdigest()
        return {
            'HTTP_X_INTERNAL_SERVICE': service,
            'HTTP_X_SECURITY_EVENT_TIMESTAMP': str(ts),
            'HTTP_X_SECURITY_EVENT_SIGNATURE': signature,
        }

    @override_settings(
        CONNECT_SECURITY_EVENT_ORIGIN_SECRET='boundary-secret',
        CONNECT_SECURITY_EVENT_ALLOWLIST={'shizuha-wiki', 'shizuha-admin', 'shizuha-id'},
    )
    @patch('connect.services.security_outbox.publish_security_event', return_value=True)
    def test_signed_event_is_durably_sanitized_and_retry_deduped(self, publish):
        payload = self._event()
        headers = self._headers(payload)

        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.post(self.endpoint, payload, format='json', **headers)
        duplicate = self.client.post(self.endpoint, payload, format='json', **headers)

        self.assertEqual(accepted.status_code, 202)
        self.assertFalse(accepted.data['duplicate'])
        self.assertIn('acceptance_id', accepted.data)
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.data['duplicate'])
        self.assertEqual(duplicate.data['acceptance_id'], accepted.data['acceptance_id'])
        publish.assert_called_once_with(accepted.data['acceptance_id'])
        row = SecurityEventOutbox.objects.get(pk=accepted.data['acceptance_id'])
        event = row.payload
        self.assertEqual(event['event_type'], 'platform.boundary_denial')
        self.assertEqual(event['reason'], 'cross_tenant_org_scope')
        self.assertEqual(event['actor_user_id'], 42)
        self.assertEqual(event['target_org_id'], 67)
        self.assertNotIn('query', event)
        self.assertNotIn('token', event)

    @override_settings(
        CONNECT_SECURITY_EVENT_ORIGIN_SECRET='boundary-secret',
        CONNECT_SECURITY_EVENT_ALLOWLIST={'shizuha-wiki', 'shizuha-admin', 'shizuha-id'},
    )
    @patch('connect.services.security_outbox.publish_security_event', return_value=True)
    def test_same_identity_with_different_payload_is_conflict(self, _publish):
        payload = self._event(event_id='same-producer-event')
        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.post(
                self.endpoint, payload, format='json', **self._headers(payload),
            )
        changed = self._event(event_id='same-producer-event')
        changed['data']['actor_username'] = 'different-actor'
        conflict = self.client.post(
            self.endpoint, changed, format='json', **self._headers(changed),
        )

        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(conflict.status_code, 409)
        row = SecurityEventOutbox.objects.get(pk=accepted.data['acceptance_id'])
        self.assertEqual(row.conflict_count, 1)
        self.assertEqual(row.payload['actor_username'], 'ren')

    @override_settings(
        CONNECT_SECURITY_EVENT_ORIGIN_SECRET='boundary-secret',
        CONNECT_SECURITY_EVENT_ALLOWLIST={'shizuha-wiki', 'shizuha-admin', 'shizuha-id'},
        CONNECT_SECURITY_OUTBOX_BACKOFF_BASE_SECONDS=1,
    )
    @patch(
        'connect.tasks.process_security_event_outbox.apply_async',
        side_effect=RuntimeError('simulated broker unavailable'),
    )
    def test_broker_failure_after_commit_keeps_one_pending_row(self, _delay):
        payload = self._event()
        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.post(
                self.endpoint, payload, format='json', **self._headers(payload),
            )

        self.assertEqual(accepted.status_code, 202)
        row = SecurityEventOutbox.objects.get(pk=accepted.data['acceptance_id'])
        self.assertEqual(row.publish_attempts, 1)
        self.assertEqual(row.status, SecurityEventOutbox.Status.RETRYING)
        self.assertIsNone(row.terminal_at)
        self.assertEqual(row.last_error_class, 'RuntimeError')
    @override_settings(
        CONNECT_SECURITY_EVENT_ORIGIN_SECRET='boundary-secret',
        CONNECT_SECURITY_EVENT_ALLOWLIST={'shizuha-wiki', 'shizuha-admin', 'shizuha-id'},
    )
    @patch(
        'connect.services.security_outbox.accept_security_event',
        side_effect=DatabaseError('simulated Postgres acceptance failure'),
    )
    def test_postgres_acceptance_failure_is_not_acknowledged(self, _accept):
        payload = self._event()
        response = self.client.post(
            self.endpoint, payload, format='json', **self._headers(payload),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(SecurityEventOutbox.objects.count(), 0)

    @override_settings(CONNECT_SECURITY_OUTBOX_WORKER_LEASE_SECONDS=30)
    @patch('connect.tasks.process_security_event_outbox.apply_async')
    def test_relay_reclaims_commit_without_fast_wake_and_publishes_uuid(self, publish):
        from connect.services.security_outbox import relay_due_security_events

        row = SecurityEventOutbox.objects.create(
            producer_service='shizuha-wiki',
            producer_event_id='committed-before-process-death',
            event_type='platform.boundary_denial',
            payload={'event_type': 'platform.boundary_denial'},
            request_hash='a' * 64,
            next_dispatch_at=timezone.now() - timedelta(seconds=1),
            lease_owner='dead-web-process',
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

        result = relay_due_security_events(batch_size=1)

        self.assertEqual(result, {'claimed': 1, 'published': 1})
        publish.assert_called_once_with(args=[str(row.pk)], retry=False)
        row.refresh_from_db()
        self.assertEqual(row.publish_attempts, 1)
        self.assertIsNotNone(row.first_published_at)
        self.assertGreater(row.next_dispatch_at, timezone.now())

    @override_settings(
        CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID='42',
        CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS=300,
    )
    def test_worker_retry_and_terminal_replay_create_exactly_one_security_dm(self):
        from connect.tasks import process_security_event_outbox

        system = User.objects.create_user(username='system', email='system@shizuha.com')
        User.objects.create_user(id=42, username='security-lead', email='security@example.com')
        row = SecurityEventOutbox.objects.create(
            producer_service='shizuha-wiki',
            producer_event_id='worker-exactly-once',
            event_type='platform.boundary_denial',
            payload={
                'event_type': 'platform.boundary_denial',
                'event_id': 'worker-exactly-once',
                'reasons': ['cross_tenant_org_scope'],
                'reason': 'cross_tenant_org_scope',
                'service': 'wiki',
                'operation': 'wiki.explicit_org_read',
                'actor_user_id': 7,
                'actor_username': 'ren',
                'source_org_ids': [1],
                'target_org_id': 67,
                'request_method': 'GET',
                'route': 'wiki:search-list',
                'correlation_id': 'worker-exactly-once',
                'detected_at': '2026-07-11T00:00:00Z',
            },
            request_hash='b' * 64,
        )

        with patch(
            'connect.views.internal_api._get_system_user_id', return_value=system.id,
        ), patch('connect.views.internal_api._safe_broadcast'):
            first = process_security_event_outbox.apply(args=[str(row.pk)]).get()
            replay = process_security_event_outbox.apply(args=[str(row.pk)]).get()

        row.refresh_from_db()
        self.assertEqual(first['status'], SecurityEventOutbox.Status.SENT)
        self.assertEqual(replay['status'], SecurityEventOutbox.Status.SENT)
        self.assertEqual(row.status, SecurityEventOutbox.Status.SENT)
        self.assertIsNotNone(row.terminal_message_id)
        self.assertEqual(
            DirectMessage.objects.filter(pk=row.terminal_message_id).count(), 1,
        )

    @override_settings(
        CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID='42',
        CONNECT_SECURITY_OUTBOX_BACKOFF_BASE_SECONDS=1,
    )
    def test_worker_failure_before_dm_transaction_remains_retryable(self):
        from connect.tasks import process_security_event_outbox

        row = SecurityEventOutbox.objects.create(
            producer_service='shizuha-wiki',
            producer_event_id='worker-crash-before-dm',
            event_type='platform.boundary_denial',
            payload={
                'event_type': 'platform.boundary_denial',
                'event_id': 'worker-crash-before-dm',
                'reasons': ['cross_tenant_org_scope'],
                'reason': 'cross_tenant_org_scope',
                'service': 'wiki',
                'operation': 'wiki.explicit_org_read',
                'actor_user_id': 7,
                'actor_username': 'ren',
                'source_org_ids': [1],
                'target_org_id': 67,
                'request_method': 'GET',
                'route': 'wiki:search-list',
                'correlation_id': 'worker-crash-before-dm',
                'detected_at': '2026-07-11T00:00:00Z',
            },
            request_hash='c' * 64,
        )

        with patch(
            'connect.services.security_outbox.deliver_security_dm',
            side_effect=RuntimeError('simulated crash before DM transaction'),
        ):
            result = process_security_event_outbox.apply(args=[str(row.pk)]).get()

        row.refresh_from_db()
        self.assertEqual(result['status'], SecurityEventOutbox.Status.RETRYING)
        self.assertEqual(row.status, SecurityEventOutbox.Status.RETRYING)
        self.assertIsNone(row.terminal_at)
        self.assertEqual(row.last_error_class, 'RuntimeError')

    @override_settings(
        CONNECT_SECURITY_EVENT_ORIGIN_SECRET='boundary-secret',
        CONNECT_SECURITY_EVENT_ALLOWLIST={'shizuha-wiki', 'shizuha-admin', 'shizuha-id'},
    )
    @patch('connect.services.security_monitor._queue_alert')
    def test_unsigned_unallowlisted_and_extra_data_are_rejected(self, queue_alert):
        payload = self._event()
        unsigned = self.client.post(
            self.endpoint, payload, format='json', HTTP_X_INTERNAL_SERVICE='shizuha-wiki',
        )
        unallowlisted = self.client.post(
            self.endpoint,
            payload,
            format='json',
            **self._headers(payload, service='shizuha-pulse'),
        )
        payload['data']['query_text'] = 'must never enter the SIEM event'
        invalid = self.client.post(
            self.endpoint, payload, format='json', **self._headers(payload),
        )

        self.assertEqual(unsigned.status_code, 403)
        self.assertEqual(unallowlisted.status_code, 403)
        self.assertEqual(invalid.status_code, 400)
        queue_alert.assert_not_called()

    @override_settings(CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS=300)
    def test_repeat_alert_fingerprint_is_atomic(self):
        from connect.tasks import _suppress_repeated_boundary_alert

        event = {
            'event_type': 'platform.boundary_denial',
            'service': 'wiki',
            'operation': 'wiki.explicit_org_read',
            'reason': 'cross_tenant_org_scope',
            'actor_user_id': 42,
            'target_org_id': 67,
        }
        self.assertFalse(_suppress_repeated_boundary_alert(event))
        self.assertTrue(_suppress_repeated_boundary_alert(event))

    @override_settings(
        CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID='42',
        CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS=300,
    )
    def test_failed_send_retry_bypasses_its_repeat_claim_and_delivers_once(self):
        from connect.tasks import send_security_anomaly_alert

        system = User.objects.create_user(username='system', email='system@shizuha.com')
        recipient = User.objects.create_user(
            id=42,
            username='security-lead',
            email='security-lead@example.com',
        )
        conversation = Conversation.objects.create(
            conversation_type='direct',
            created_by_id=system.id,
        )
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=system.id,
            user_name='System',
        )
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=recipient.id,
            user_name='Security Lead',
        )
        event = {
            'event_type': 'platform.boundary_denial',
            'reasons': ['cross_tenant_org_scope'],
            'reason': 'cross_tenant_org_scope',
            'service': 'wiki',
            'operation': 'wiki.explicit_org_read',
            'actor_user_id': 7,
            'target_org_id': 67,
        }
        real_create = DirectMessage.objects.create
        attempts = 0

        def flaky_create(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('transient database failure')
            return real_create(**kwargs)

        with patch(
            'connect.views.internal_api._get_system_user_id',
            return_value=system.id,
        ), patch(
            'connect.views.internal_api._get_or_create_system_dm',
            return_value=(conversation, False),
        ), patch(
            'connect.views.internal_api._safe_broadcast',
        ), patch.object(
            DirectMessage.objects,
            'create',
            side_effect=flaky_create,
        ), patch.object(
            send_security_anomaly_alert,
            'retry',
            side_effect=Retry(),
        ):
            with self.assertRaises(Retry):
                send_security_anomaly_alert(event)

        # The retry is the same logical delivery: request.retries=1 must bypass
        # the fingerprint held by the failed first attempt, not self-suppress.
        with patch(
            'connect.views.internal_api._get_system_user_id',
            return_value=system.id,
        ), patch(
            'connect.views.internal_api._get_or_create_system_dm',
            return_value=(conversation, False),
        ), patch(
            'connect.views.internal_api._safe_broadcast',
        ), patch.object(
            DirectMessage.objects,
            'create',
            side_effect=flaky_create,
        ):
            result = send_security_anomaly_alert.apply(
                args=[event],
                retries=1,
                throw=True,
            ).get()

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(attempts, 2)
        self.assertEqual(
            DirectMessage.objects.filter(conversation=conversation).count(),
            1,
        )


class SecurityEventOutboxConcurrencyTest(APITransactionTestCase):
    reset_sequences = True

    @patch('connect.services.security_outbox.publish_security_event', return_value=True)
    def test_concurrent_duplicate_acceptance_converges_on_one_row(self, publish):
        from connect.services.security_outbox import accept_security_event

        event = {
            'event_type': 'platform.boundary_denial',
            'event_id': 'concurrent-producer-event',
            'event_source': 'shizuha://wiki',
            'reasons': ['cross_tenant_org_scope'],
            'reason': 'cross_tenant_org_scope',
            'service': 'wiki',
            'operation': 'wiki.explicit_org_read',
            'actor_user_id': 42,
            'actor_username': 'ren',
            'source_org_ids': [1],
            'target_org_id': 67,
            'request_method': 'GET',
            'route': 'wiki:search-list',
            'correlation_id': 'concurrent-producer-event',
            'detected_at': '2026-07-11T00:00:00Z',
            'received_at': '2026-07-11T00:00:01Z',
        }

        def accept_once():
            close_old_connections()
            try:
                row, duplicate, conflict = accept_security_event(
                    producer_service='shizuha-wiki',
                    event=dict(event),
                )
                return str(row.pk), duplicate, conflict
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: accept_once(), range(2)))

        self.assertEqual(SecurityEventOutbox.objects.count(), 1)
        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(sorted(result[1] for result in results), [False, True])
        self.assertTrue(all(result[2] is False for result in results))
        publish.assert_called_once()

    @override_settings(
        CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID='42',
        CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS=300,
        CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8',
    )
    def test_outbox_repeat_suppression_increments_repeat_suppressed_counter(self):
        """CON-217 outbox repeat suppression must record the repeat_suppressed
        stage so the ConnectSecurityAnomalyNotDelivered rule can distinguish a
        legitimate repeat-window dedup from an undelivered anomaly."""
        from connect import metrics
        from connect.tasks import process_security_event_outbox

        system = User.objects.create_user(username='system', email='system@shizuha.com')
        User.objects.create_user(id=42, username='security-lead', email='security@example.com')

        shared = _SecurityMetricsRedis()
        with patch('connect.metrics._security_metrics_client', return_value=shared):
            for index in range(2):
                row = SecurityEventOutbox.objects.create(
                    producer_service='shizuha-wiki',
                    producer_event_id=f'outbox-repeat-{index}',
                    event_type='platform.boundary_denial',
                    payload={
                        'event_type': 'platform.boundary_denial',
                        'event_id': f'outbox-repeat-{index}',
                        'reasons': ['cross_tenant_org_scope'],
                        'reason': 'cross_tenant_org_scope',
                        'service': 'wiki',
                        'operation': 'wiki.explicit_org_read',
                        'actor_user_id': 7,
                        'actor_username': 'ren',
                        'source_org_ids': [1],
                        'target_org_id': 67,
                        'request_method': 'GET',
                        'route': 'wiki:search-list',
                        'correlation_id': f'outbox-repeat-{index}',
                        'detected_at': '2026-07-11T00:00:00Z',
                    },
                    request_hash=f'{index}' * 64,
                )
                with patch(
                    'connect.views.internal_api._get_system_user_id', return_value=system.id,
                ), patch('connect.views.internal_api._safe_broadcast'):
                    result = process_security_event_outbox.apply(args=[str(row.pk)]).get()
                row.refresh_from_db()
                if index == 0:
                    self.assertEqual(result['status'], SecurityEventOutbox.Status.SENT)
                    self.assertEqual(row.status, SecurityEventOutbox.Status.SENT)
                else:
                    self.assertEqual(
                        result['status'], SecurityEventOutbox.Status.SUPPRESSED_REPEAT,
                    )
                    self.assertEqual(row.status, SecurityEventOutbox.Status.SUPPRESSED_REPEAT)

            body = metrics.render_security_pipeline_metrics()

        self.assertIn(
            'connect_security_pipeline_stage_total{stage="evaluated",result="anomaly"} 2',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_stage_total{stage="sent",result="success"} 1',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_stage_total{stage="evaluated",result="repeat_suppressed"} 1',
            body,
        )


class _SecurityMetricsRedis:
    def __init__(self):
        self.values = {}

    def hincrby(self, _key, field, count):
        self.values[field] = int(self.values.get(field, 0)) + int(count)
        return self.values[field]

    def hgetall(self, _key):
        return dict(self.values)


class DurableSecurityPipelineMetricsTest(APITestCase):
    def test_outbox_metrics_are_postgres_truth_and_fail_loud(self):
        from connect import metrics

        SecurityEventOutbox.objects.create(
            producer_service='shizuha-wiki',
            producer_event_id='metrics-pending',
            event_type='platform.boundary_denial',
            payload={'event_type': 'platform.boundary_denial'},
            request_hash='d' * 64,
        )
        body = metrics.render_security_outbox_metrics()
        self.assertIn('connect_security_outbox_accepted_total{service="connect"} 1', body)
        self.assertIn('connect_security_outbox_pending{service="connect"} 1', body)
        self.assertIn('connect_security_outbox_probe_errors{service="connect"} 0', body)

        with patch.object(
            SecurityEventOutbox.objects,
            'all',
            side_effect=DatabaseError('simulated Postgres metrics outage'),
        ):
            failed = metrics.render_security_outbox_metrics()
        self.assertIn('connect_security_outbox_probe_errors{service="connect"} 1', failed)
        self.assertNotIn('connect_security_outbox_pending{service="connect"}', failed)

    @override_settings(CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8')
    def test_worker_stages_are_rendered_by_an_independent_web_process(self):
        from connect import metrics

        shared = _SecurityMetricsRedis()
        with patch('connect.metrics._security_metrics_client', return_value=shared):
            metrics.record_connect_security_event_evaluated('anomaly')
            metrics.record_connect_security_alert_sent()
            metrics.record_connect_security_alert_failure('no_recipient')
            metrics.record_connect_security_alert_failure('send_exception', count=2)

            # This render is the web-pod scrape side. It sees the shared Redis
            # values even though evaluation/delivery happened in a worker.
            body = metrics.render_security_pipeline_metrics()

        self.assertIn(
            'connect_security_pipeline_stage_total{stage="evaluated",result="anomaly"} 1',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_stage_total{stage="sent",result="success"} 1',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_stage_total{stage="failure",result="no_recipient"} 1',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_stage_total{stage="failure",result="send_exception"} 2',
            body,
        )
        self.assertIn('connect_security_pipeline_probe_errors{service="connect"} 0', body)

    @override_settings(CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8')
    def test_metrics_backend_failure_is_fail_loud(self):
        from connect import metrics

        broken = _SecurityMetricsRedis()
        broken.hgetall = lambda _key: (_ for _ in ()).throw(ConnectionError('redis down'))
        with patch('connect.metrics._security_metrics_client', return_value=broken):
            body = metrics.render_security_pipeline_metrics()

        self.assertIn('connect_security_pipeline_probe_errors{service="connect"} 1', body)
        self.assertNotIn('connect_security_pipeline_stage_total', body)
        self.assertIn(
            'connect_security_pipeline_worker_write_failures_total{service="connect"} 0',
            body,
        )

    @override_settings(CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8')
    def test_backend_recovery_omits_unavailable_counters_without_synthetic_zero(self):
        """Mirror the production N -> Redis error -> same N scrape order."""
        from connect import metrics

        client = MagicMock()
        historical = {'failure|send_exception': '2'}
        client.hgetall.side_effect = [
            historical,
            ConnectionError('redis down'),
            historical,
        ]
        with patch('connect.metrics._security_metrics_client', return_value=client):
            before = metrics.render_security_pipeline_metrics()
            with self.assertLogs('connect.metrics', level='ERROR'):
                unavailable = metrics.render_security_pipeline_metrics()
            recovered = metrics.render_security_pipeline_metrics()

        before_counters = [
            line for line in before.splitlines()
            if line.startswith('connect_security_pipeline_stage_total')
        ]
        recovered_counters = [
            line for line in recovered.splitlines()
            if line.startswith('connect_security_pipeline_stage_total')
        ]
        self.assertEqual(recovered_counters, before_counters)
        self.assertIn(
            'connect_security_pipeline_stage_total'
            '{stage="failure",result="send_exception"} 2',
            before,
        )
        self.assertNotIn('connect_security_pipeline_stage_total', unavailable)
        self.assertIn(
            'connect_security_pipeline_worker_write_failures_total{service="connect"} 0',
            unavailable,
        )
        self.assertIn(
            'connect_security_pipeline_probe_errors{service="connect"} 1',
            unavailable,
        )

    @override_settings(CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8')
    def test_postgres_health_failure_omits_only_its_unavailable_counter_family(self):
        from connect import metrics

        shared = _SecurityMetricsRedis()
        with patch('connect.metrics._security_metrics_client', return_value=shared), patch(
            'connect.models.SecurityPipelineMetricHealth.objects.filter',
            side_effect=DatabaseError('postgres down'),
        ), self.assertLogs('connect.metrics', level='ERROR'):
            body = metrics.render_security_pipeline_metrics()

        self.assertIn('connect_security_pipeline_stage_total', body)
        self.assertNotIn('connect_security_pipeline_worker_write_failures_total', body)
        self.assertNotIn('connect_security_pipeline_worker_write_failure_recent', body)
        self.assertIn('connect_security_pipeline_probe_errors{service="connect"} 1', body)

    @override_settings(CONNECT_SECURITY_METRICS_REDIS_URL='redis://metrics.example/8')
    def test_worker_write_failure_stays_visible_when_web_redis_read_recovers(self):
        from connect import metrics
        from connect.models import SecurityPipelineMetricHealth

        shared = _SecurityMetricsRedis()
        healthy_read = shared.hgetall
        shared.hincrby = lambda *_args: (_ for _ in ()).throw(
            ConnectionError('worker cannot reach metrics Redis')
        )
        shared.hgetall = healthy_read

        with patch('connect.metrics._security_metrics_client', return_value=shared):
            metrics.record_connect_security_event_evaluated('anomaly')
            # The web-side Redis read now succeeds, but it must still export the
            # independent Postgres failure signal from the worker write.
            body = metrics.render_security_pipeline_metrics()

        health = SecurityPipelineMetricHealth.objects.get(channel='redis')
        self.assertEqual(health.failure_count, 1)
        self.assertIsNotNone(health.last_failure_at)
        self.assertIn(
            'connect_security_pipeline_worker_write_failures_total{service="connect"} 1',
            body,
        )
        self.assertIn(
            'connect_security_pipeline_worker_write_failure_recent{service="connect"} 1',
            body,
        )
        self.assertIn('connect_security_pipeline_probe_errors{service="connect"} 1', body)

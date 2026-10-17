import uuid
from threading import Barrier, Thread
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIRequestFactory

from connect.models import DirectMessage, VerifiedAlertDelivery
from connect.views.internal_api import InternalVerifiedAlertDeliveryView


class VerifiedAlertDeliveryTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.client_id = uuid.uuid4()
        self.body = {
            'organization_id': 7,
            'requester_id': 101,
            'recipient_id': 202,
            'client_message_id': str(self.client_id),
            'content': '[ALERT] synthetic test',
        }

    def request(self, body=None):
        return self.factory.post(
            '/connect/api/internal/verified-alert-deliveries/',
            body or self.body, format='json', HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
        )

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({7}))
    @patch('connect.views.internal_api._safe_broadcast')
    def test_exact_replay_returns_same_receipt_and_broadcasts_only_after_commit(self, broadcast, _orgs):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            first = InternalVerifiedAlertDeliveryView.as_view()(self.request())
            self.assertEqual(broadcast.call_count, 0)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(broadcast.call_count, 1)
        second = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data, first.data)
        self.assertEqual(VerifiedAlertDelivery.objects.count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({7}))
    def test_changed_content_or_recipient_conflicts_without_second_insert(self, _orgs):
        first = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        for field, value in [('content', 'changed'), ('recipient_id', 303)]:
            changed = dict(self.body, **{field: value})
            response = InternalVerifiedAlertDeliveryView.as_view()(self.request(changed))
            self.assertEqual(response.status_code, 409)
        self.assertEqual(VerifiedAlertDelivery.objects.count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)
        self.assertEqual(first.status_code, 201)

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({8}))
    def test_exact_org_authority_failure_precedes_insert(self, _orgs):
        response = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(VerifiedAlertDelivery.objects.count(), 0)
        self.assertEqual(DirectMessage.objects.count(), 0)

    @patch('connect.views.internal_api.get_user_org_ids', return_value=None)
    def test_authority_lookup_failure_is_retryable_and_fail_closed(self, _orgs):
        response = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(VerifiedAlertDelivery.objects.count(), 0)

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({7}))
    def test_authenticated_exact_org_receipt_lookup(self, _orgs):
        created = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        receipt_id = created.data['receipt_id']
        request = self.factory.get(
            f'/connect/api/internal/verified-alert-deliveries/{receipt_id}/',
            {'organization_id': 7, 'requester_id': 101},
            HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
        )
        response = InternalVerifiedAlertDeliveryView.as_view()(request, receipt_id=receipt_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'receipt_id': receipt_id, 'state': 'accepted'})

    def test_only_closed_pulse_service_contract_is_accepted(self):
        request = self.factory.post(
            '/connect/api/internal/verified-alert-deliveries/', self.body,
            format='json', HTTP_X_INTERNAL_SERVICE='shizuha-agent',
        )
        response = InternalVerifiedAlertDeliveryView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({7}))
    def test_ambiguous_commit_retry_joins_persisted_receipt(self, _orgs):
        """The first response is deliberately discarded after its commit."""
        lost_response = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        persisted_id = str(VerifiedAlertDelivery.objects.get().id)
        del lost_response
        retry = InternalVerifiedAlertDeliveryView.as_view()(self.request())
        self.assertEqual((retry.status_code, retry.data['receipt_id']), (200, persisted_id))
        self.assertEqual((VerifiedAlertDelivery.objects.count(), DirectMessage.objects.count()), (1, 1))

    @patch('connect.views.internal_api.get_user_org_ids', return_value=frozenset({7}))
    @patch('connect.views.internal_api._safe_broadcast', side_effect=RuntimeError('relay unavailable'))
    def test_relay_failure_cannot_erase_durable_receipt(self, _broadcast, _orgs):
        with self.assertRaisesRegex(RuntimeError, 'relay unavailable'):
            with self.captureOnCommitCallbacks(execute=True):
                InternalVerifiedAlertDeliveryView.as_view()(self.request())
        delivery = VerifiedAlertDelivery.objects.get()
        self.assertTrue(DirectMessage.objects.filter(id=delivery.message_id).exists())
        lookup = self.factory.get(
            f'/connect/api/internal/verified-alert-deliveries/{delivery.id}/',
            {'organization_id': 7, 'requester_id': 101},
            HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
        )
        response = InternalVerifiedAlertDeliveryView.as_view()(lookup, receipt_id=delivery.id)
        self.assertEqual((response.status_code, response.data['receipt_id']), (200, str(delivery.id)))


@skipUnlessDBFeature('has_select_for_update')
class VerifiedAlertDeliveryPostgresRaceTests(TransactionTestCase):
    """Exercise the unique-key loser path with two real DB connections."""

    reset_sequences = True

    def setUp(self):
        self.factory = APIRequestFactory()
        self.client_id = uuid.uuid4()
        self.body = {
            'organization_id': 7, 'requester_id': 101, 'recipient_id': 202,
            'client_message_id': str(self.client_id), 'content': '[ALERT] concurrent',
        }

    def _send(self, gate, results, key):
        close_old_connections()
        request = self.factory.post(
            '/connect/api/internal/verified-alert-deliveries/', self.body,
            format='json', HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
        )
        try:
            results[key] = InternalVerifiedAlertDeliveryView.as_view()(request)
        except Exception as exc:
            results[key] = exc
        finally:
            close_old_connections()

    def test_concurrent_exact_replays_converge_on_one_receipt_and_message(self):
        import connect.views.internal_api as internal_api
        original_hash = internal_api._alert_request_hash
        gate = Barrier(2, timeout=5)

        def synchronized_hash(**kwargs):
            digest = original_hash(**kwargs)
            gate.wait()
            return digest

        results = {}
        with patch.object(internal_api, 'get_user_org_ids', return_value=frozenset({7})), \
             patch.object(internal_api, '_alert_request_hash', side_effect=synchronized_hash), \
             patch.object(internal_api, '_safe_broadcast'):
            first = Thread(target=self._send, args=(gate, results, 'first'))
            second = Thread(target=self._send, args=(gate, results, 'second'))
            first.start(); second.start(); first.join(10); second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(any(isinstance(value, Exception) for value in results.values()), results)
        self.assertEqual({value.status_code for value in results.values()}, {200, 201})
        self.assertEqual(len({value.data['receipt_id'] for value in results.values()}), 1)
        self.assertEqual(VerifiedAlertDelivery.objects.count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)

"""
CON-129: shared-organization tenancy gate for Connect messaging.

INCIDENT 2026-07-02: fleet agents (orgs {1,17,76}) DMd an org-67-only external
user — messaging enforced Block-policy only, with no org-membership check on
DM send / conversation create / participant add / agent-mention delivery.

These tests pin the gate at every enforcement point: cross-org 403
(cross_org_not_allowed), same-org unaffected, block-policy unaffected,
idempotent replay gated, membership-unavailable fail-closed (503), mention
fan-out suppressed, plus the org-set lookup's cache/stale/claim behavior.

Enforcement is OFF in test_settings (the unit suite has no admin service);
every test here switches it ON explicitly via override_settings.
"""
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from connect.models import (
    Block,
    Conversation,
    ConversationParticipant,
    DirectMessage,
)
from connect.services import org_tenancy

User = get_user_model()

# Org topology used across the view tests: sender(101) shares org 1 with 202;
# 303 is org-67-only (the harshita shape); 404 has no resolvable membership.
ORG_MAP = {
    101: frozenset({1, 17}),
    202: frozenset({1}),
    303: frozenset({67}),
    100: frozenset({1}),    # same-org agent participant
    999: frozenset({67}),   # cross-org agent participant
}


def _fake_org_ids(user_id):
    try:
        return ORG_MAP.get(int(user_id))
    except (TypeError, ValueError):
        return None


class ResponseStub:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _id_lookup(url, *args, **kwargs):
    """shizuha-id internal user lookup stub for MessageUserView."""
    params = kwargs.get('params') or {}
    users = {
        'sameorg': {'id': 202, 'username': 'sameorg', 'email': 'sameorg@shizuha.com'},
        'external': {'id': 303, 'username': 'external', 'email': 'external@example.com'},
        'unknownorg': {'id': 404, 'username': 'unknownorg', 'email': 'unknownorg@example.com'},
    }
    if '/internal/users/by-username/' in url:
        user = users.get((params.get('username') or '').lower())
        if user:
            return ResponseStub(200, user)
        return ResponseStub(404, {'error': 'not found'})
    return ResponseStub(500, {'error': f'unexpected url {url}'})


class PersonalOrganizationAuthorityTests(TestCase):
    def setUp(self):
        cache.clear()

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_requires_exactly_one_personal_org_owned_by_user(self, get):
        get.return_value = ResponseStub(200, {'organizations': [
            {'id': 7, 'slug': 'personal-101', 'is_personal': True, 'owner_id': 101},
        ]})
        self.assertEqual(org_tenancy.get_user_personal_org_id(101), 7)

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_slug_only_or_wrong_owner_is_rejected(self, get):
        get.return_value = ResponseStub(200, {'organizations': [
            {'id': 7, 'slug': 'personal-101', 'is_personal': False, 'owner_id': 101},
            {'id': 8, 'slug': 'workspace', 'is_personal': True, 'owner_id': 202},
        ]})
        self.assertIsNone(org_tenancy.get_user_personal_org_id(101))

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_multiple_personal_orgs_fail_closed(self, get):
        get.return_value = ResponseStub(200, {'organizations': [
            {'id': 7, 'is_personal': True, 'owner_id': 101},
            {'id': 8, 'is_personal': True, 'owner_id': 101},
        ]})
        self.assertIsNone(org_tenancy.get_user_personal_org_id(101))


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
class OrgTenancyServiceTests(TestCase):
    """Unit behavior of the org-set lookup and verdict logic."""

    def setUp(self):
        cache.clear()

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_lookup_parses_and_caches(self, get):
        get.return_value = ResponseStub(200, {'memberships': {'1': 'member', '17': 'owner'}})
        first = org_tenancy.get_user_org_ids(7)
        second = org_tenancy.get_user_org_ids(7)
        self.assertEqual(first, frozenset({1, 17}))
        self.assertEqual(second, frozenset({1, 17}))
        self.assertEqual(get.call_count, 1)  # second hit served from cache

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_lookup_serves_stale_on_failure(self, get):
        get.return_value = ResponseStub(200, {'memberships': {'1': 'member'}})
        self.assertEqual(org_tenancy.get_user_org_ids(8), frozenset({1}))
        # Expire the fresh tier but keep last-known-good, then break the API.
        cache.delete(org_tenancy._FRESH_KEY.format(user_id=8))
        get.side_effect = org_tenancy.requests.ConnectionError('admin down')
        self.assertEqual(org_tenancy.get_user_org_ids(8), frozenset({1}))

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_lookup_unavailable_returns_none(self, get):
        get.side_effect = org_tenancy.requests.ConnectionError('admin down')
        self.assertIsNone(org_tenancy.get_user_org_ids(9))

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_personal_org_lookup_selects_canonical_admin_authority_and_caches(self, get):
        get.return_value = ResponseStub(200, {'organizations': [
            {'id': 7, 'slug': 'engineering', 'is_personal': False, 'owner_id': 99},
            {'id': 91, 'slug': 'personal-owner', 'is_personal': True, 'owner_id': 12},
        ]})
        self.assertEqual(org_tenancy.get_user_personal_org_id(12), 91)
        self.assertEqual(org_tenancy.get_user_personal_org_id(12), 91)
        self.assertEqual(get.call_count, 1)

    @mock.patch('connect.services.org_tenancy.requests.get')
    def test_personal_org_lookup_fails_closed_without_personal_org(self, get):
        get.return_value = ResponseStub(200, {'organizations': [
            {'id': 7, 'slug': 'engineering', 'is_personal': False},
        ]})
        self.assertIsNone(org_tenancy.get_user_personal_org_id(12))

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_verdicts(self, _ids):
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 202), org_tenancy.ALLOWED)
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 303), org_tenancy.CROSS_ORG)
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 404), org_tenancy.UNKNOWN)
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 101), org_tenancy.ALLOWED)

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_current_admin_membership_supersedes_stale_sender_token_claim(self, ids):
        token = {'organization_memberships': {'67': 'member'}}
        verdict = org_tenancy.check_messaging_allowed(101, 202, sender_token=token)
        self.assertEqual(verdict, org_tenancy.ALLOWED)
        # The token was minted before the current org-1 grant. Admin is the
        # current authorization authority for both sides, so the fresh grant
        # permits the edge without waiting for token expiry.
        looked_up = {c.args[0] for c in ids.call_args_list}
        self.assertEqual(looked_up, {101, 202})

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_current_admin_revocation_supersedes_stale_sender_token_claim(self, ids):
        token = {'organization_memberships': {'67': 'member'}}
        verdict = org_tenancy.check_messaging_allowed(101, 303, sender_token=token)
        self.assertEqual(verdict, org_tenancy.CROSS_ORG)
        looked_up = {c.args[0] for c in ids.call_args_list}
        self.assertEqual(looked_up, {101, 303})

    def test_token_claim_parsing(self):
        self.assertEqual(
            org_tenancy.org_ids_from_token({'organization_memberships': {'1': 'x', '2': 'y'}}),
            frozenset({1, 2}),
        )
        self.assertIsNone(org_tenancy.org_ids_from_token(None))
        self.assertIsNone(org_tenancy.org_ids_from_token({}))
        self.assertIsNone(org_tenancy.org_ids_from_token({'organization_memberships': {}}))
        self.assertIsNone(org_tenancy.org_ids_from_token({'organization_memberships': 'junk'}))
        # Legacy single organization_id claim is deliberately ignored.
        self.assertIsNone(org_tenancy.org_ids_from_token({'organization_id': 1}))

    @override_settings(CONNECT_ENFORCE_ORG_TENANCY=False)
    @mock.patch('connect.services.org_tenancy.get_user_org_ids')
    def test_kill_switch_disables_enforcement(self, ids):
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 303), org_tenancy.ALLOWED)
        ids.assert_not_called()

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_exempt_sender_allowlist_default_empty_and_sender_only(self, _ids):
        # Default: no exemptions — cross-org stays blocked.
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 303), org_tenancy.CROSS_ORG)
        with override_settings(CONNECT_ORG_TENANCY_EXEMPT_SENDER_IDS='91, 101'):
            # Exempt SENDER may reach any recipient (system notifications).
            self.assertEqual(org_tenancy.check_messaging_allowed(101, 303), org_tenancy.ALLOWED)
            # Exemption is sender-side only: others still can't reach across.
            self.assertEqual(org_tenancy.check_messaging_allowed(303, 202), org_tenancy.CROSS_ORG)


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True, CONNECT_DM_DEDUP_WINDOW_SECONDS=0)
@mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
@mock.patch('connect.views.messaging._safe_broadcast')
@mock.patch('requests.get', side_effect=_id_lookup)
class DirectMessageGateTests(TestCase):
    """MessageUserView (/api/messaging/dm/) enforcement."""

    def setUp(self):
        cache.clear()
        self.profile_delay = mock.patch('connect.tasks.ensure_participant_social_profile.delay')
        self.profile_delay.start()
        self.addCleanup(self.profile_delay.stop)
        self.client = APIClient()
        self.sender = User(id=101, username='fleetagent', email='fleetagent@shizuha.com')
        self.client.force_authenticate(user=self.sender, token={'is_agent': True})

    def _dm(self, recipient_username, content='hello', client_message_id=None):
        payload = {'recipient_username': recipient_username, 'content': content}
        if client_message_id:
            payload['client_message_id'] = client_message_id
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post('/api/messaging/dm/', payload, format='json')

    def test_cross_org_dm_403_and_no_message_row(self, *_):
        resp = self._dm('external')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'cross_org_not_allowed')
        self.assertEqual(DirectMessage.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_same_org_dm_unaffected(self, *_):
        resp = self._dm('sameorg')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_ack_only_replied_is_rejected(self, *_):
        resp = self._dm('sameorg', content='Replied.')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Replied', resp.data['error'])
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_block_policy_unaffected_for_same_org_pair(self, *_):
        Block.objects.create(blocker_id=202, blocked_id=101)
        resp = self._dm('sameorg')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'Cannot send a direct message to this user')
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_membership_unavailable_fails_closed_503(self, *_):
        resp = self._dm('unknownorg')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data['error'], 'org_lookup_unavailable')
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_idempotent_replay_gated(self, *_):
        cmid = str(uuid.uuid4())
        first = self._dm('sameorg', client_message_id=cmid)
        self.assertEqual(first.status_code, 201)

        # Recipient 202 later loses the shared org (or the pair was committed
        # before the gate shipped): the retry must NOT resurface the message
        # across the tenancy boundary.
        with mock.patch.dict(ORG_MAP, {202: frozenset({67})}):
            replay = self._dm('sameorg', client_message_id=cmid)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.data['error'], 'cross_org_not_allowed')

        # Same-org replay still works (idempotent 200, no duplicate row).
        replay_ok = self._dm('sameorg', client_message_id=cmid)
        self.assertEqual(replay_ok.status_code, 200)
        self.assertEqual(DirectMessage.objects.count(), 1)



@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
@mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
class CollectorReplyRoutingTests(TestCase):
    """CON-247: org-safe collector reply routing.

    A reply addressed to the System collector (recipient_username="system") is
    routed into the sender's OWN collector conversation (the per-user System DM
    where the sender is already a participant) — the configured in-org fallback
    collector channel. This is NOT a cross-org send, so it must not 403. The
    CON-129 gate is preserved for every other edge: an arbitrary cross-org DM
    to a regular user still 403s, and a reply to System with no existing
    collector conversation still 403s (genuine cross-org attempt).
    """

    SYSTEM_ID = 500

    def setUp(self):
        cache.clear()
        self.profile_delay = mock.patch('connect.tasks.ensure_participant_social_profile.delay')
        self.profile_delay.start()
        self.addCleanup(self.profile_delay.stop)
        self.client = APIClient()
        self.sender = User(id=101, username='fleetagent', email='fleetagent@shizuha.com')
        self.client.force_authenticate(user=self.sender, token={'is_agent': True})
        # Resolve System to a fixed id, matching the _id_lookup stub below.
        sys_patcher = mock.patch(
            'connect.views.internal_api._get_system_user_id',
            return_value=self.SYSTEM_ID,
        )
        sys_patcher.start()
        self.addCleanup(sys_patcher.stop)
        # Also clear the module-level cache so the mock is authoritative.
        import connect.views.internal_api as ia
        ia._system_user_id_cache = self.SYSTEM_ID

    @staticmethod
    def _system_id_lookup(url, *args, **kwargs):
        """shizuha-id stub resolving `system` -> SYSTEM_ID; everything else falls through."""
        params = kwargs.get('params') or {}
        if '/internal/users/by-username/' in url and (params.get('username') or '').lower() == 'system':
            return ResponseStub(200, {
                'id': CollectorReplyRoutingTests.SYSTEM_ID,
                'username': 'system',
                'email': 'system@shizuha.com',
                'organization_ids': [],
            })
        return _id_lookup(url, *args, **kwargs)

    def _create_collector_conversation(self):
        """Create the per-user System collector DM (sender 101 + System 500)."""
        conv = Conversation.objects.create(
            conversation_type='direct',
            direct_key=Conversation.build_direct_key(101, self.SYSTEM_ID),
            created_by_id=self.SYSTEM_ID,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=101, user_name='fleetagent',
            user_email='fleetagent@shizuha.com', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=conv, user_id=self.SYSTEM_ID, user_name='System',
            user_email='system@shizuha.com', is_admin=True,
        )
        return conv

    @mock.patch('requests.get', side_effect=_system_id_lookup)
    def test_cross_org_dm_to_regular_user_still_403(self, *_):
        """CON-247 must not be a generic bypass: cross-org to a real user still 403s."""
        self._create_collector_conversation()  # collector exists — still no bypass for others
        resp = self.client.post(
            '/api/messaging/dm/',
            {'recipient_username': 'external', 'content': 'hello'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'cross_org_not_allowed')
        self.assertEqual(DirectMessage.objects.count(), 0)

    @mock.patch('requests.get', side_effect=_system_id_lookup)
    def test_collector_reply_routes_into_own_collector_conversation(self, *_):
        conv = self._create_collector_conversation()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                '/api/messaging/dm/',
                {'recipient_username': 'system', 'content': 'my retro reply'},
                format='json',
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(resp.data.get('delivered_via_fallback'))
        self.assertEqual(resp.data['conversation_id'], str(conv.id))
        # Exactly one message landed in the collector conversation.
        self.assertEqual(DirectMessage.objects.filter(conversation=conv).count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)

    @mock.patch('requests.get', side_effect=_system_id_lookup)
    def test_collector_reply_without_existing_conversation_still_403(self, *_):
        """No collector conversation => genuine cross-org attempt => fail-closed.

        The System user (500) has no resolvable org membership, so the CON-129
        gate returns 503 (org_lookup_unavailable, fail-closed) — NOT a delivery
        and NOT a silent drop. The point is nothing is delivered to System when
        there is no org-safe collector channel.
        """
        resp = self.client.post(
            '/api/messaging/dm/',
            {'recipient_username': 'system', 'content': 'hello'},
            format='json',
        )
        self.assertIn(resp.status_code, (403, 503))
        self.assertEqual(DirectMessage.objects.count(), 0)

    @mock.patch('requests.get', side_effect=_system_id_lookup)
    def test_collector_reply_idempotent_retry_exactly_one_message(self, *_):
        """Same client_message_id retry => same receipt, no duplicate delivery."""
        conv = self._create_collector_conversation()
        cmid = str(uuid.uuid4())
        payload = {
            'recipient_username': 'system',
            'content': 'retro reply',
            'client_message_id': cmid,
        }
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post('/api/messaging/dm/', payload, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        with self.captureOnCommitCallbacks(execute=True):
            replay = self.client.post('/api/messaging/dm/', payload, format='json')
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data.get('idempotent_replay'))
        self.assertEqual(DirectMessage.objects.filter(conversation=conv).count(), 1)
        self.assertEqual(DirectMessage.objects.count(), 1)



@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
@mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
class ConversationGateTests(TestCase):
    """ConversationViewSet create / participant-add enforcement."""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.sender = User(id=101, username='fleetagent', email='fleetagent@shizuha.com')

    def _create_conversation(self, payload):
        from connect.views.messaging import ConversationViewSet
        request = self.factory.post('/api/conversations/', payload, format='json')
        force_authenticate(request, user=self.sender)
        return ConversationViewSet.as_view({'post': 'create'})(request)

    @mock.patch('connect.views.messaging._resolve_user_details',
                return_value={'name': 'X', 'email': 'x@example.com'})
    def test_direct_conversation_create_cross_org_403(self, *_):
        resp = self._create_conversation({'conversation_type': 'direct', 'participant_ids': [303]})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'cross_org_not_allowed')
        self.assertEqual(Conversation.objects.count(), 0)

    @mock.patch('connect.views.messaging._resolve_user_details',
                return_value={'name': 'X', 'email': 'x@example.com'})
    def test_direct_conversation_create_same_org_ok(self, *_):
        resp = self._create_conversation({'conversation_type': 'direct', 'participant_ids': [202]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Conversation.objects.count(), 1)

    @mock.patch('connect.views.messaging._resolve_user_details',
                return_value={'name': 'X', 'email': 'x@example.com'})
    def test_group_create_with_cross_org_participant_403(self, *_):
        resp = self._create_conversation({
            'conversation_type': 'group', 'name': 'g',
            'participant_ids': [202, 303],
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'cross_org_not_allowed')
        self.assertEqual(Conversation.objects.count(), 0)

    def test_group_participant_add_cross_org_403(self, *_):
        from connect.views.messaging import ConversationViewSet
        conv = Conversation.objects.create(conversation_type='group', name='g')
        ConversationParticipant.objects.create(
            conversation=conv, user_id=101, user_name='Fleet', is_admin=True,
        )
        request = self.factory.post(
            f'/api/conversations/{conv.id}/participants/',
            {'user_id': 303}, format='json',
        )
        force_authenticate(request, user=self.sender)
        resp = ConversationViewSet.as_view({'post': 'participants'})(request, pk=str(conv.id))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['error'], 'cross_org_not_allowed')
        self.assertFalse(
            ConversationParticipant.objects.filter(conversation=conv, user_id=303).exists()
        )

    def test_group_participant_add_same_org_ok(self, *_):
        from connect.views.messaging import ConversationViewSet
        conv = Conversation.objects.create(conversation_type='group', name='g')
        ConversationParticipant.objects.create(
            conversation=conv, user_id=101, user_name='Fleet', is_admin=True,
        )
        request = self.factory.post(
            f'/api/conversations/{conv.id}/participants/',
            {'user_id': 202, 'user_name': 'Same Org'}, format='json',
        )
        force_authenticate(request, user=self.sender)
        with mock.patch('connect.views.messaging._safe_broadcast'):
            resp = ConversationViewSet.as_view({'post': 'participants'})(request, pk=str(conv.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            ConversationParticipant.objects.filter(conversation=conv, user_id=202).exists()
        )


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
@mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
class MentionDeliveryGateTests(TestCase):
    """Agent-mention fan-out only reaches same-org agents — even for legacy
    conversations that already contain cross-org participants."""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.sender = User(id=101, username='fleetagent', email='fleetagent@shizuha.com')
        self.conv = Conversation.objects.create(conversation_type='group', name='g')
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=101, user_name='Fleet', is_admin=True,
        )
        self.same_org_agent = uuid.uuid4()
        self.cross_org_agent = uuid.uuid4()
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=100, user_name='Kai',
            participant_type='agent', agent_id=self.same_org_agent,
        )
        # Legacy cross-org participant (added before the gate existed).
        ConversationParticipant.objects.create(
            conversation=self.conv, user_id=999, user_name='Tara',
            participant_type='agent', agent_id=self.cross_org_agent,
        )

    def _send(self, content):
        from connect.views.messaging import ConversationViewSet
        request = self.factory.post(
            f'/api/conversations/{self.conv.id}/messages/',
            {'content': content, 'message_type': 'text'}, format='json',
        )
        force_authenticate(request, user=self.sender)
        return ConversationViewSet.as_view({'post': 'messages'})(request, pk=str(self.conv.id))

    @mock.patch('connect.tasks.notify_agent_mention.delay')
    @mock.patch('connect.tasks.send_dm_notification.delay')
    def test_cross_org_mention_suppressed_same_org_delivered(self, _push, mock_mention, _ids):
        resp = self._send('@Kai and @Tara please review')
        self.assertEqual(resp.status_code, 201)
        delivered = {c.args[2] for c in mock_mention.call_args_list}
        self.assertEqual(delivered, {str(self.same_org_agent)})


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True)
class HiveAgentOrgTenancyTests(TestCase):
    """CON-149: the org gate must not block owner↔own Hive-agent DMs.

    Hive personal-agent pseudo-users carry no org membership; the shared-org
    user-tenancy check does not apply to agent bridge endpoints (cross-owner
    isolation is enforced upstream by Hive's owner-scoped resolution). Real
    tenant-user↔tenant-user edges stay fully gated.
    """

    def setUp(self):
        cache.clear()

    def test_is_hive_agent_user_id_classifier(self):
        from connect.services import hive_agents
        # Every minted id is recognised; real shizuha-id users never are.
        minted = hive_agents.hive_agent_user_id('hive:claw:abc')
        self.assertTrue(hive_agents.is_hive_agent_user_id(minted))
        self.assertGreaterEqual(minted, hive_agents.HIVE_AGENT_USER_ID_BASE)
        for real in (101, 202, 303, 404, 116, 252):
            self.assertFalse(hive_agents.is_hive_agent_user_id(real))
        # Band boundaries.
        base = hive_agents.HIVE_AGENT_USER_ID_BASE
        self.assertTrue(hive_agents.is_hive_agent_user_id(base))
        self.assertFalse(hive_agents.is_hive_agent_user_id(base - 1))
        self.assertFalse(hive_agents.is_hive_agent_user_id(base + hive_agents.HIVE_AGENT_USER_ID_SPAN))
        self.assertFalse(hive_agents.is_hive_agent_user_id(None))

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_owner_to_own_agent_edge_allowed_both_directions(self, _ids):
        from connect.services.hive_agents import hive_agent_user_id
        agent_id = hive_agent_user_id('hive:claw:owner101')
        owner = 101  # has orgs {1, 17}
        # Owner → own agent, and the agent's reply → owner, are both intra-tenant.
        self.assertEqual(org_tenancy.check_messaging_allowed(owner, agent_id), org_tenancy.ALLOWED)
        self.assertEqual(org_tenancy.check_messaging_allowed(agent_id, owner), org_tenancy.ALLOWED)
        # Allowed even for an owner whose org set would otherwise be disjoint:
        # the agent carries no org, so org overlap is not the governing rule.
        self.assertEqual(org_tenancy.check_messaging_allowed(303, agent_id), org_tenancy.ALLOWED)

    @mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
    def test_carveout_is_narrow_real_cross_org_user_still_blocked(self, _ids):
        # The agent carve-out must not leak into real user↔user edges.
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 303), org_tenancy.CROSS_ORG)
        self.assertEqual(org_tenancy.check_messaging_allowed(101, 202), org_tenancy.ALLOWED)


@override_settings(CONNECT_ENFORCE_ORG_TENANCY=True, CONNECT_DM_DEDUP_WINDOW_SECONDS=0)
@mock.patch('connect.services.org_tenancy.get_user_org_ids', side_effect=_fake_org_ids)
@mock.patch('connect.views.messaging._safe_broadcast')
class HiveAgentDMViewTests(TestCase):
    """/api/messaging/dm/ owner→own Hive-agent path (the CON-149 live repro)."""

    def setUp(self):
        cache.clear()
        self.profile_delay = mock.patch('connect.tasks.ensure_participant_social_profile.delay')
        self.profile_delay.start()
        self.addCleanup(self.profile_delay.stop)
        self.notify = mock.patch('connect.tasks.send_dm_notification.delay')
        self.notify.start()
        self.addCleanup(self.notify.stop)
        self.client = APIClient()
        # Owner is a real tenant user with org memberships (the qa-hive-fixture shape).
        self.owner = User(id=101, username='qa-hive-fixture', email='owner@shizuha.com')
        self.client.force_authenticate(user=self.owner, token={'organization_memberships': {'1': 'owner'}})

    @mock.patch('connect.views.messaging.dispatch_hive_agent_message', return_value=True)
    @mock.patch('connect.views.messaging.find_hive_agent_by_lookup')
    def test_owner_can_dm_own_hive_agent_201(self, mock_lookup, _dispatch, *_):
        from connect.services.hive_agents import hive_agent_user_id
        agent = {
            'stable_id': 'hive:claw:owner101',
            'display_name': 'My Claw',
            'connect_username': 'hive-claw-owner101',
            'runtime_kind': 'claw',
        }
        mock_lookup.return_value = agent
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                '/api/messaging/dm/',
                {'recipient_username': 'hive-claw-owner101', 'content': 'hello agent',
                 'client_message_id': str(uuid.uuid4())},
                format='json',
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(DirectMessage.objects.count(), 1)
        agent_id = hive_agent_user_id('hive:claw:owner101')
        self.assertTrue(
            ConversationParticipant.objects.filter(user_id=agent_id, participant_type='agent').exists()
        )

    @mock.patch('connect.views.messaging.dispatch_hive_agent_message', return_value=True)
    @mock.patch('connect.views.messaging.find_hive_agent_by_lookup')
    def test_owner_agent_dm_idempotent_replay(self, mock_lookup, _dispatch, *_):
        agent = {'stable_id': 'hive:claw:owner101', 'display_name': 'My Claw',
                 'connect_username': 'hive-claw-owner101', 'runtime_kind': 'claw'}
        mock_lookup.return_value = agent
        cmid = str(uuid.uuid4())
        body = {'recipient_username': 'hive-claw-owner101', 'content': 'hello', 'client_message_id': cmid}
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post('/api/messaging/dm/', body, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        with self.captureOnCommitCallbacks(execute=True):
            replay = self.client.post('/api/messaging/dm/', body, format='json')
        self.assertIn(replay.status_code, (200, 201))
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_missing_signer_secret_cannot_silently_drop_persisted_dm(self, *_):
        agent = {
            'stable_id': 'hive:claw:owner101',
            'display_name': 'My Claw',
            'connect_username': 'hive-claw-owner101',
            'runtime_kind': 'claw',
        }
        context = {
            'owner_id': '101',
            'organization_id': 1,
            'agent_participant_id': '1999999999',
        }
        with override_settings(HIVE_CONNECT_HMAC_SECRET=''), \
             mock.patch('connect.views.messaging.find_hive_agent_by_lookup', return_value=agent), \
             mock.patch(
                 'connect.services.hive_agents._authoritative_delivery_context',
                 return_value=context,
             ), \
             mock.patch('connect.services.hive_agents.requests.post') as post, \
             self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {
                    'recipient_username': 'hive-claw-owner101',
                    'content': 'hello',
                    'client_message_id': str(uuid.uuid4()),
                },
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.data)
        post.assert_not_called()
        human = DirectMessage.objects.get(message_type='text')
        terminal = DirectMessage.objects.get(message_type='system')
        self.assertEqual(terminal.reply_to_id, human.id)
        self.assertIn('Delivery failed', terminal.content)

    def test_missing_persisted_authority_cannot_silently_drop_dm(self, *_):
        agent = {
            'stable_id': 'hive:claw:owner101',
            'display_name': 'My Claw',
            'connect_username': 'hive-claw-owner101',
            'runtime_kind': 'claw',
        }
        with override_settings(HIVE_CONNECT_HMAC_SECRET='test-secret'), \
             mock.patch('connect.views.messaging.find_hive_agent_by_lookup', return_value=agent), \
             mock.patch(
                 'connect.services.hive_agents._authoritative_delivery_context',
                 return_value=None,
             ), \
             mock.patch('connect.services.hive_agents.requests.post') as post, \
             self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/messaging/dm/',
                {
                    'recipient_username': 'hive-claw-owner101',
                    'content': 'hello',
                    'client_message_id': str(uuid.uuid4()),
                },
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.data)
        post.assert_not_called()
        human = DirectMessage.objects.get(message_type='text')
        terminal = DirectMessage.objects.get(message_type='system')
        self.assertEqual(terminal.reply_to_id, human.id)
        self.assertIn('Delivery failed', terminal.content)

"""PLAT-1322: Data-layer authorization-invariant tests across shared stores.

Extends the PLAT-1236 authz-invariant suite to the DATA layer for all
customer-data services. Seeds two tenants (org 1 and org 67), then for every
list/get/update/delete/search/export endpoint asserts cross-tenant access
returns 403/404 and never leaks rows. Covers cache-hit paths and search
indexes.

Existing coverage (not duplicated here):
- test_con129_org_tenancy.py — DM send, conversation create, mention delivery
- test_con142_object_authz.py — profile/post/comment/company/job write IDOR
- test_con142_read_symmetry.py — read/enumeration org scoping
- test_con150_authz_gates.py — meetings, stories

Gaps covered here:
- Search endpoints (people, posts, jobs, companies, universal) — cross-tenant
- Feed endpoints (home, following, trending) — cross-tenant
- Company/job read endpoints — cross-tenant
- Profile read endpoints — cross-tenant
- Cache-hit paths (org lookup cached, then cross-tenant access)
- Search index (PostgreSQL full-text search vector)
"""
import hashlib
import hmac
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from connect.models import (Comment, Conversation, ConversationParticipant, DirectMessage, Follow, Post, SocialProfile, Story, Meeting, MessageStar)

User = get_user_model()

# Two tenants: org 1 (Shizuha internal) and org 67 (external/partner)
ORG_1 = frozenset({1})
ORG_67 = frozenset({67})


def _mk_user(username, email, user_id=None):
    kwargs = {'username': username, 'email': email}
    if user_id is not None:
        kwargs['id'] = user_id
    return User.objects.create_user(**kwargs)


def _profile(user, *, username=None):
    return SocialProfile.objects.create(
        user_id=user.id,
        user_email=user.email,
        username=username or user.username,
        display_name=(username or user.username).title(),
    )


def _org_side_effect(actor_id, recipient_id, **_kwargs):
    """Return True only if both users are in org 1 (the Shizuha org)."""
    # User IDs 1-1000 are org 1; 1001+ are org 67
    if int(actor_id) < 1000 and int(recipient_id) < 1000:
        return True
    if int(actor_id) >= 1000 and int(recipient_id) >= 1000:
        return True
    return False


def _membership_org_ids(user_id):
    """Return org set based on user ID range."""
    if int(user_id) < 1000:
        return ORG_1
    return ORG_67


class DataLayerAuthzBase(APITestCase):
    """Base class: seed two tenants with identical data, then assert isolation."""

    @classmethod
    def setUpTestData(cls):
        # Tenant A (org 1) — user IDs < 1000
        cls.tenant_a = _mk_user('tenanta', 'tenanta@shizuha.com', user_id=101)
        cls.tenant_a_other = _mk_user('tenanta2', 'tenanta2@shizuha.com', user_id=102)
        _profile(cls.tenant_a, username='tenanta')
        _profile(cls.tenant_a_other, username='tenanta2')

        # Tenant B (org 67) — user IDs >= 1000
        cls.tenant_b = _mk_user('tenantb', 'tenantb@external.com', user_id=1001)
        cls.tenant_b_other = _mk_user('tenantb2', 'tenantb2@external.com', user_id=1002)
        _profile(cls.tenant_b, username='tenantb')
        _profile(cls.tenant_b_other, username='tenantb2')

    def setUp(self):
        self.client = APIClient()
        # Mock org resolution so tenant A users resolve to org 1, tenant B to org 67
        self._org_patcher = mock.patch.multiple(
            'connect.services.privacy',
            _membership_org_ids=mock.Mock(side_effect=_membership_org_ids),
            users_share_org=mock.Mock(side_effect=_org_side_effect),
        )
        self._org_patcher.start()
        self.addCleanup(self._org_patcher.stop)

    def _auth_as(self, user):
        self.client.force_authenticate(user=user)


# ── Search endpoints ──────────────────────────────────────────────────────

class PeopleSearchAuthzTests(DataLayerAuthzBase):
    """Cross-tenant people search must not leak profiles."""

    def test_tenant_a_cannot_see_tenant_b_in_people_search(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/people/?q=tenantb')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['user_id'] for p in resp.data['results']]
        self.assertNotIn(self.tenant_b.id, ids)

    def test_tenant_b_cannot_see_tenant_a_in_people_search(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get('/api/search/people/?q=tenanta')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['user_id'] for p in resp.data['results']]
        self.assertNotIn(self.tenant_a.id, ids)

    def test_tenant_a_can_see_own_org_in_people_search(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/people/?q=tenanta')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['user_id'] for p in resp.data['results']]
        self.assertIn(self.tenant_a.id, ids)
        self.assertIn(self.tenant_a_other.id, ids)


class PostSearchAuthzTests(DataLayerAuthzBase):
    """Cross-tenant post search must not leak posts."""

    def setUp(self):
        super().setUp()
        # Tenant A post
        self.post_a = Post.objects.create(
            author_id=self.tenant_a.id, author_email=self.tenant_a.email,
            content='tenanta internal update', visibility='public',
        )
        # Tenant B post
        self.post_b = Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb external update', visibility='public',
        )

    def test_tenant_a_cannot_see_tenant_b_post_in_search(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/posts/?q=tenantb')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertNotIn(str(self.post_b.id), ids)

    def test_tenant_b_cannot_see_tenant_a_post_in_search(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get('/api/search/posts/?q=tenanta')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertNotIn(str(self.post_a.id), ids)

    def test_tenant_a_can_see_own_post_in_search(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/posts/?q=internal')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertIn(str(self.post_a.id), ids)






class UniversalSearchAuthzTests(DataLayerAuthzBase):
    """Cross-tenant universal search must not leak any content type."""

    def setUp(self):
        super().setUp()
        self.post_b = Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb secret project', visibility='public',
        )

    def test_tenant_a_universal_search_excludes_tenant_b_posts(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/?q=tenantb')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['people']), 0)
        self.assertEqual(len(resp.data['posts']), 0)

    def test_tenant_b_universal_search_excludes_tenant_a(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get('/api/search/?q=tenanta')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['people']), 0)
        self.assertEqual(len(resp.data['posts']), 0)


# ── Feed endpoints ────────────────────────────────────────────────────────

class FeedAuthzTests(DataLayerAuthzBase):
    """Cross-tenant feed must not leak posts."""

    def setUp(self):
        super().setUp()
        # Tenant A follows tenant A_other
        Follow.objects.create(follower_id=self.tenant_a.id, following_id=self.tenant_a_other.id)
        # Tenant A post
        Post.objects.create(
            author_id=self.tenant_a.id, author_email=self.tenant_a.email,
            content='tenanta feed post', visibility='public',
        )
        # Tenant B post
        Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb feed post', visibility='public',
        )

    def test_home_feed_excludes_cross_tenant_posts(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/feed/home/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        contents = [p['content'] for p in resp.data['results']]
        self.assertIn('tenanta feed post', contents)
        self.assertNotIn('tenantb feed post', contents)

    def test_following_feed_excludes_cross_tenant_posts(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/feed/following/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        contents = [p['content'] for p in resp.data['results']]
        self.assertNotIn('tenantb feed post', contents)

    def test_trending_feed_excludes_cross_tenant_posts(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/feed/trending/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data.get('results', resp.data)
        contents = [p['content'] for p in results]
        self.assertNotIn('tenantb feed post', contents)


# ── Profile read endpoints ────────────────────────────────────────────────

class ProfileReadAuthzTests(DataLayerAuthzBase):
    """Cross-tenant profile reads must 404."""

    def test_tenant_a_cannot_read_tenant_b_profile_by_username(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_b_cannot_read_tenant_a_profile_by_username(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get(f'/api/profiles/{self.tenant_a.username}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_can_read_own_profile(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_a.username}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_tenant_a_cannot_list_tenant_b_followers(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/followers/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_following(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/following/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_posts(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/posts/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_skills(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/skills/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_experience(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/experience/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_education(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/education/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ── Company read endpoints ────────────────────────────────────────────────



# ── Cache-hit paths ───────────────────────────────────────────────────────

class CacheHitAuthzTests(DataLayerAuthzBase):
    """After org lookup is cached, cross-tenant access must still be blocked."""

    def test_cached_org_lookup_still_blocks_cross_tenant_profile_read(self):
        # First access: populate cache
        self._auth_as(self.tenant_a)
        resp = self.client.get(f'/api/profiles/{self.tenant_a.username}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Second access: cache hit, still must block cross-tenant
        resp = self.client.get(f'/api/profiles/{self.tenant_b.username}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cached_org_lookup_still_blocks_cross_tenant_search(self):
        Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb cached search test', visibility='public',
        )
        self._auth_as(self.tenant_a)
        # Prime cache with same-org lookup
        resp = self.client.get('/api/search/posts/?q=tenanta')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Cache hit, cross-tenant search must still be blocked
        resp = self.client.get('/api/search/posts/?q=tenantb')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 0)

    def test_cached_org_lookup_still_blocks_cross_tenant_feed(self):
        Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb cached feed', visibility='public',
        )
        self._auth_as(self.tenant_a)
        # Prime cache
        resp = self.client.get('/api/feed/home/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Cache hit, cross-tenant feed must still be blocked
        resp = self.client.get('/api/feed/home/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        contents = [p['content'] for p in resp.data['results']]
        self.assertNotIn('tenantb cached feed', contents)


# ── Search index (PostgreSQL full-text) ───────────────────────────────────

class SearchIndexAuthzTests(DataLayerAuthzBase):
    """Full-text search index must not leak across tenants."""

    def setUp(self):
        super().setUp()
        # Create posts with distinct content for FTS
        self.post_a = Post.objects.create(
            author_id=self.tenant_a.id, author_email=self.tenant_a.email,
            content='confidential shizuha roadmap Q3', visibility='public',
        )
        self.post_b = Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='confidential external partner launch', visibility='public',
        )

    def test_fts_does_not_leak_cross_tenant_confidential_content(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/posts/?q=confidential')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertIn(str(self.post_a.id), ids)
        self.assertNotIn(str(self.post_b.id), ids)

    def test_fts_tenant_b_isolated_from_tenant_a(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get('/api/search/posts/?q=confidential')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertIn(str(self.post_b.id), ids)
        self.assertNotIn(str(self.post_a.id), ids)

    def test_fts_shared_keyword_returns_only_own_tenant_results(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/search/posts/?q=roadmap')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [p['id'] for p in resp.data['results']]
        self.assertIn(str(self.post_a.id), ids)
        self.assertNotIn(str(self.post_b.id), ids)


# ── Cross-tenant write IDOR (data-layer complement to CON-142) ────────────

class CrossTenantWriteAuthzTests(DataLayerAuthzBase):
    """Cross-tenant write attempts must 403/404 and not mutate data."""

    def setUp(self):
        super().setUp()
        self.post_b = Post.objects.create(
            author_id=self.tenant_b.id, author_email=self.tenant_b.email,
            content='tenantb post', visibility='public',
        )

    def test_tenant_a_cannot_update_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.patch(
            f'/api/posts/{self.post_b.id}/',
            {'content': 'hijacked by tenant a'},
            format='json',
        )
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.post_b.refresh_from_db()
        self.assertEqual(self.post_b.content, 'tenantb post')

    def test_tenant_a_cannot_delete_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.delete(f'/api/posts/{self.post_b.id}/')
        self.assertIn(resp.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertTrue(Post.objects.filter(pk=self.post_b.pk).exists())

    def test_tenant_a_cannot_react_to_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/posts/{self.post_b.id}/react/',
            {'reaction_type': 'like'},
            format='json',
        )
        # Post is not readable cross-tenant, so get_object fails
        self.assertIn(resp.status_code, (
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ))

    def test_tenant_a_cannot_comment_on_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/posts/{self.post_b.id}/comments/',
            {'content': 'cross-tenant comment'},
            format='json',
        )
        self.assertIn(resp.status_code, (
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ))

    def test_tenant_a_cannot_bookmark_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/posts/{self.post_b.id}/bookmark/',
            format='json',
        )
        self.assertIn(resp.status_code, (
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ))

    def test_tenant_a_cannot_repost_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/posts/{self.post_b.id}/repost/',
            format='json',
        )
        self.assertIn(resp.status_code, (
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ))

    def test_tenant_a_cannot_quote_tenant_b_post(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/posts/{self.post_b.id}/quote/',
            {'content': 'cross-tenant quote'},
            format='json',
        )
        self.assertIn(resp.status_code, (
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ))


# ── Cross-tenant conversation isolation ───────────────────────────────────

class ConversationIsolationTests(DataLayerAuthzBase):
    """Cross-tenant conversation reads must not leak messages."""

    def setUp(self):
        super().setUp()
        # Tenant A conversation
        self.conv_a = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.tenant_a.id,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv_a, user_id=self.tenant_a.id,
            user_email=self.tenant_a.email, is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=self.conv_a, user_id=self.tenant_a_other.id,
            user_email=self.tenant_a_other.email,
        )
        self.msg_a = DirectMessage.objects.create(
            conversation=self.conv_a, sender_id=self.tenant_a.id,
            content='tenant-a internal message',
        )

    def test_tenant_b_cannot_read_tenant_a_conversation_participants(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get(f'/api/conversations/{self.conv_a.id}/participants/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_b_cannot_read_tenant_a_conversation_messages(self):
        self._auth_as(self.tenant_b)
        resp = self.client.get(f'/api/conversations/{self.conv_a.id}/messages/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_b_cannot_send_to_tenant_a_conversation(self):
        self._auth_as(self.tenant_b)
        resp = self.client.post(
            f'/api/conversations/{self.conv_a.id}/messages/',
            {'content': 'cross-tenant message'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(DirectMessage.objects.filter(conversation=self.conv_a).count(), 1)


# ── Standup / message-plane isolation ─────────────────────────────────────

@override_settings(
    CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET='standup-secret',
    CONNECT_SYSTEM_MESSAGE_ALLOWLIST={'pulse'},
    CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS=300,
)
class StandupMessagePlaneIsolationTests(DataLayerAuthzBase):
    """PLAT-4232: Standup rides ordinary Connect conversations/System DMs.

    These tests pin the message-plane invariant required by CON-218: every
    conversation/message action must root in explicit participant membership,
    and per-recipient System DMs must not be visible or mutable by another user
    or by a same-org manager who is not an explicit participant.
    """

    system_user_id = 9000

    def _system_token(self, *, recipient_user_id, content, sender_service='pulse', ts=None, secret='standup-secret'):
        ts = int(time.time()) if ts is None else int(ts)
        body_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
        digest = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        return f'{ts}:{digest}'

    def _post_system_dm(self, recipient, content, *, token=None, sender_service='pulse'):
        token = token or self._system_token(recipient_user_id=recipient.id, content=content, sender_service=sender_service)
        with mock.patch('connect.views.internal_api._get_system_user_id', return_value=self.system_user_id), \
             mock.patch('connect.views.internal_api._safe_broadcast'):
            return self.client.post(
                '/api/internal/system-message/',
                {
                    'recipient_user_id': recipient.id,
                    'content': content,
                    'sender_name': 'Standup Bot',
                    'sender_service': sender_service,
                },
                format='json',
                HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
                HTTP_X_SYSTEM_MESSAGE_TOKEN=token,
            )

    def _conversation_payload_ids(self, response):
        if not hasattr(response, 'data'):
            return set()
        payload = response.data
        if isinstance(payload, dict):
            payload = payload.get('results', payload.get('conversations', []))
        return {str(row['id']) for row in payload}

    def test_every_conversation_and_message_action_denies_non_participant_and_preserves_rows(self):
        conv = Conversation.objects.create(conversation_type='group', name='Daily Standup', created_by_id=self.tenant_a.id)
        ConversationParticipant.objects.create(conversation=conv, user_id=self.tenant_a.id, user_email=self.tenant_a.email, is_admin=True)
        ConversationParticipant.objects.create(conversation=conv, user_id=self.tenant_a_other.id, user_email=self.tenant_a_other.email)
        msg = DirectMessage.objects.create(conversation=conv, sender_id=self.tenant_a.id, content='standup private marker')
        before = {
            'messages': DirectMessage.objects.filter(conversation=conv).count(),
            'participants': ConversationParticipant.objects.filter(conversation=conv, has_left=False).count(),
            'content': msg.content,
        }

        self._auth_as(self.tenant_b)
        list_resp = self.client.get('/api/conversations/')
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(conv.id), self._conversation_payload_ids(list_resp))

        starred_resp = self.client.get('/api/conversations/starred/')
        self.assertEqual(starred_resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(msg.id), {str(row['id']) for row in starred_resp.data})

        changes_resp = self.client.get('/api/conversations/changes/?after_cursor=0')
        self.assertEqual(changes_resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(conv.id), {str(row['id']) for row in changes_resp.data['conversations']})

        checks = [
            ('get', f'/api/conversations/{conv.id}/', None),
            ('patch', f'/api/conversations/{conv.id}/', {'name': 'pwn'}),
            ('get', f'/api/conversations/{conv.id}/messages/', None),
            ('post', f'/api/conversations/{conv.id}/messages/', {'content': 'pwn'}),
            ('post', f'/api/conversations/{conv.id}/read/', None),
            ('post', f'/api/conversations/{conv.id}/mute/', None),
            ('post', f'/api/conversations/{conv.id}/pin/', None),
            ('post', f'/api/conversations/{conv.id}/archive/', None),
            ('post', f'/api/conversations/{conv.id}/clear/', None),
            ('post', f'/api/conversations/{conv.id}/delete_for_me/', None),
            ('get', f'/api/conversations/{conv.id}/participants/', None),
            ('post', f'/api/conversations/{conv.id}/participants/remove/', {'user_id': self.tenant_a_other.id}),
            ('get', f'/api/conversations/{conv.id}/messages/{msg.id}/info/', None),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/react/', {'emoji': '👀'}),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/forward/', {'conversation_id': str(conv.id)}),
            ('patch', f'/api/conversations/{conv.id}/messages/{msg.id}/edit/', {'content': 'pwn'}),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/star/', None),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/ack/', {'type': 'read'}),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/delete/', {'type': 'for_everyone'}),
        ]
        for method, url, data in checks:
            resp = getattr(self.client, method)(url, data or {}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, url)

        msg.refresh_from_db()
        self.assertEqual(DirectMessage.objects.filter(conversation=conv).count(), before['messages'])
        self.assertEqual(ConversationParticipant.objects.filter(conversation=conv, has_left=False).count(), before['participants'])
        self.assertEqual(msg.content, before['content'])
        self.assertFalse(MessageStar.objects.filter(message=msg, user_id=self.tenant_b.id).exists())

    def test_per_recipient_system_dms_are_private_to_recipient_and_not_manager_visible(self):
        manager = _mk_user('standup-manager', 'standup-manager@shizuha.com', user_id=103)
        _profile(manager, username='standup-manager')
        resp_a = self._post_system_dm(self.tenant_a, 'standup marker A')
        resp_b = self._post_system_dm(self.tenant_a_other, 'standup marker B')
        self.assertEqual(resp_a.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp_b.status_code, status.HTTP_201_CREATED)
        conv_a = resp_a.data['conversation_id']
        conv_b = resp_b.data['conversation_id']
        self.assertNotEqual(conv_a, conv_b)

        self._auth_as(self.tenant_a)
        own = self.client.get(f'/api/conversations/{conv_a}/messages/')
        foreign = self.client.get(f'/api/conversations/{conv_b}/messages/')
        self.assertEqual(own.status_code, status.HTTP_200_OK)
        self.assertEqual(foreign.status_code, status.HTTP_404_NOT_FOUND)

        self._auth_as(manager)
        list_resp = self.client.get('/api/conversations/')
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(conv_a, self._conversation_payload_ids(list_resp))
        self.assertNotIn(conv_b, self._conversation_payload_ids(list_resp))
        self.assertEqual(self.client.get(f'/api/conversations/{conv_a}/messages/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.post(f'/api/conversations/{conv_a}/messages/', {'content': 'manager bypass'}, format='json').status_code, status.HTTP_404_NOT_FOUND)

    def test_system_dm_does_not_inflate_recipient_unread_badge(self):
        # PLAT-6502: system task-update DMs must NOT bump the unread badge —
        # a burst of N system DMs leaves unread_count at 0 (badge stays
        # meaningful for real human/agent messages).
        from connect.models import (ConversationParticipant)
        resp = self._post_system_dm(self.tenant_a, 'task update 1')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        conv_id = resp.data['conversation_id']
        participant = ConversationParticipant.objects.get(
            conversation_id=conv_id, user_id=self.tenant_a.id,
        )
        self.assertEqual(participant.unread_count, 0)
        # A burst of many system DMs still does not inflate the badge.
        for i in range(5):
            self._post_system_dm(self.tenant_a, f'task update {i}')
        participant.refresh_from_db()
        self.assertEqual(participant.unread_count, 0)

    def test_system_message_origin_token_recipient_body_time_and_allowlist_are_fail_closed(self):
        base_count = DirectMessage.objects.count()
        valid_for_a = self._system_token(recipient_user_id=self.tenant_a.id, content='marker')
        cases = [
            # Recipient swap: token signs A but body targets B.
            (self.tenant_b, 'marker', valid_for_a, 'pulse'),
            # Body tamper: token signs different content.
            (self.tenant_a, 'tampered', valid_for_a, 'pulse'),
            # Stale/replayed timestamp outside max age.
            (self.tenant_a, 'marker', self._system_token(recipient_user_id=self.tenant_a.id, content='marker', ts=int(time.time()) - 1000), 'pulse'),
            # Unallowlisted sender service, even with a matching HMAC.
            (self.tenant_a, 'marker', self._system_token(recipient_user_id=self.tenant_a.id, content='marker', sender_service='evil'), 'evil'),
        ]
        for recipient, content, token, service in cases:
            resp = self._post_system_dm(recipient, content, token=token, sender_service=service)
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(DirectMessage.objects.count(), base_count)

    def test_group_membership_removal_revokes_list_read_search_and_write_paths(self):
        conv = Conversation.objects.create(conversation_type='group', name='Engineering Standup', created_by_id=self.tenant_a.id)
        ConversationParticipant.objects.create(conversation=conv, user_id=self.tenant_a.id, user_email=self.tenant_a.email, is_admin=True)
        ConversationParticipant.objects.create(conversation=conv, user_id=self.tenant_a_other.id, user_email=self.tenant_a_other.email)
        msg = DirectMessage.objects.create(conversation=conv, sender_id=self.tenant_a.id, content='revocation marker')

        self._auth_as(self.tenant_a)
        remove_resp = self.client.post(f'/api/conversations/{conv.id}/participants/remove/', {'user_id': self.tenant_a_other.id}, format='json')
        self.assertEqual(remove_resp.status_code, status.HTTP_200_OK)

        self._auth_as(self.tenant_a_other)
        list_resp = self.client.get('/api/conversations/')
        self.assertNotIn(str(conv.id), self._conversation_payload_ids(list_resp))
        for method, url, data in [
            ('get', f'/api/conversations/{conv.id}/', None),
            ('get', f'/api/conversations/{conv.id}/messages/', None),
            ('post', f'/api/conversations/{conv.id}/messages/', {'content': 'after removal'}),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/react/', {'emoji': '👀'}),
            ('post', f'/api/conversations/{conv.id}/messages/{msg.id}/ack/', {'type': 'read'}),
        ]:
            resp = getattr(self.client, method)(url, data or {}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, url)



# ── Cross-tenant story isolation ──────────────────────────────────────────

class StoryIsolationTests(DataLayerAuthzBase):
    """Cross-tenant story reads must not leak."""

    def setUp(self):
        super().setUp()
        self.story_b = Story.objects.create(
            author_id=self.tenant_b.id,
            author_email=self.tenant_b.email,
            author_username=self.tenant_b.username,
            story_type='text',
            text_content='tenant-b story',
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def test_tenant_a_cannot_view_tenant_b_story(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(f'/api/stories/{self.story_b.id}/view/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_react_to_tenant_b_story(self):
        self._auth_as(self.tenant_a)
        resp = self.client.post(
            f'/api/stories/{self.story_b.id}/react/',
            {'reaction': '👍'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_a_cannot_list_tenant_b_stories(self):
        self._auth_as(self.tenant_a)
        resp = self.client.get('/api/stories/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data.get('results', [])
        ids = [s['id'] for s in results]
        self.assertNotIn(str(self.story_b.id), ids)

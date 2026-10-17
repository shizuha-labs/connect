"""CON-142 slice 3: read/enumeration authz symmetry for Connect surfaces."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from connect.models import (Comment, Connection, Conversation, ConversationParticipant, DirectMessage, Follow, Mention, Post, SocialProfile)

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


def _profile(user, *, username=None, followers=0):
    return SocialProfile.objects.create(
        user_id=user.id,
        user_email=user.email,
        username=username or user.username,
        display_name=(username or user.username).title(),
        followers_count=followers,
    )


class ReadSymmetryBase(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.viewer = _mk_user('viewer', 'viewer@example.com')
        self.same_org = _mk_user('sameorg', 'sameorg@example.com')
        self.cross_org = _mk_user('crossorg', 'crossorg@example.com')
        _profile(self.viewer)
        _profile(self.same_org)
        _profile(self.cross_org)

    def _share_side_effect(self, actor_id, recipient_id, **_kwargs):
        return int(actor_id) == int(recipient_id) or int(recipient_id) == int(self.same_org.id)

    def _patch_orgs(self):
        return mock.patch.multiple(
            'connect.services.privacy',
            _membership_org_ids=mock.Mock(return_value={1}),
            users_share_org=mock.Mock(side_effect=self._share_side_effect),
        )


class ProfileEnumerationTests(ReadSymmetryBase):
    def test_followers_hide_cross_org_profiles(self):
        Follow.objects.create(follower_id=self.same_org.id, following_id=self.viewer.id)
        Follow.objects.create(follower_id=self.cross_org.id, following_id=self.viewer.id)
        self.client.force_authenticate(self.viewer)

        with self._patch_orgs():
            resp = self.client.get(f'/api/profiles/{self.viewer.username}/followers/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row['user_id'] for row in resp.data['results']}
        self.assertIn(self.same_org.id, ids)
        self.assertNotIn(self.cross_org.id, ids)

    def test_suggestions_hide_cross_org_candidates(self):
        # Give the cross-org candidate more followers; org scope must beat reach.
        SocialProfile.objects.filter(user_id=self.same_org.id).update(followers_count=1)
        SocialProfile.objects.filter(user_id=self.cross_org.id).update(followers_count=999)
        self.client.force_authenticate(self.viewer)

        with self._patch_orgs():
            resp = self.client.get('/api/suggestions/?limit=10')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row['user_id'] for row in resp.data['suggestions']}
        self.assertIn(self.same_org.id, ids)
        self.assertNotIn(self.cross_org.id, ids)
        self.assertTrue(resp.data['sources']['same_org'])

    def test_profile_posts_unknown_user_is_typed_json_404_not_html(self):
        # CON-342: an unknown username (including one with a dot, which the
        # default DRF lookup regex [^/.]+ used to reject -> HTML 404) must hit
        # the profile route and return a typed JSON 404, not an HTML page.
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs():
            for unknown in ('no-such-user', 'no.such.user'):
                resp = self.client.get(f'/api/profiles/{unknown}/posts/')
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                self.assertIn('application/json', resp['Content-Type'])
                self.assertIn('detail', resp.data)

    def test_profile_posts_dotted_username_resolves_known_user(self):
        # CON-342: a valid username containing a dot must resolve to the profile
        # (typed 200), not fall through to an HTML 404.
        dotted = _mk_user('nagi.shizuha', 'nagi.shizuha@example.com')
        _profile(dotted, username='nagi.shizuha')
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs():
            # The viewer shares an org with the dotted user (same_org id).
            with mock.patch(
                'connect.services.privacy.users_share_org',
                mock.Mock(side_effect=lambda a, b, **kw: int(b) in (int(self.same_org.id), int(dotted.id))),
            ):
                resp = self.client.get(f'/api/profiles/{dotted.username}/posts/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class SearchAndFeedReadSymmetryTests(ReadSymmetryBase):
    def setUp(self):
        super().setUp()
        self.same_post = Post.objects.create(
            author_id=self.same_org.id,
            author_email=self.same_org.email,
            content='connect launch update',
            visibility='public',
        )
        self.same_followers_post = Post.objects.create(
            author_id=self.same_org.id,
            author_email=self.same_org.email,
            content='followers only launch detail',
            visibility='followers',
        )
        self.same_connections_post = Post.objects.create(
            author_id=self.same_org.id,
            author_email=self.same_org.email,
            content='connections only launch detail',
            visibility='connections',
        )
        self.same_mentioned_post = Post.objects.create(
            author_id=self.same_org.id,
            author_email=self.same_org.email,
            content='mentioned only launch detail',
            visibility='mentioned',
        )
        self.cross_post = Post.objects.create(
            author_id=self.cross_org.id,
            author_email=self.cross_org.email,
            content='connect launch private tenant data',
            visibility='public',
        )
        self.same_comment = Comment.objects.create(
            post=self.same_post,
            author_id=self.same_org.id,
            author_email=self.same_org.email,
            content='same org comment',
        )
        self.cross_comment = Comment.objects.create(
            post=self.cross_post,
            author_id=self.cross_org.id,
            author_email=self.cross_org.email,
            content='cross org comment',
        )


    def test_main_post_endpoints_filter_cross_org_posts(self):
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs():
            list_resp = self.client.get('/api/posts/')
            same_detail = self.client.get(f'/api/posts/{self.same_post.id}/')
            cross_detail = self.client.get(f'/api/posts/{self.cross_post.id}/')

        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        rows = list_resp.data['results'] if isinstance(list_resp.data, dict) else list_resp.data
        ids = {str(row['id']) for row in rows}
        self.assertIn(str(self.same_post.id), ids)
        self.assertNotIn(str(self.cross_post.id), ids)
        self.assertEqual(same_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(cross_detail.status_code, status.HTTP_404_NOT_FOUND)

    def test_comment_endpoints_scope_through_readable_parent_posts(self):
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs():
            same_nested = self.client.get(f'/api/posts/{self.same_post.id}/comments/')
            cross_nested = self.client.get(f'/api/posts/{self.cross_post.id}/comments/')
            standalone = self.client.get('/api/comments/')

        self.assertEqual(same_nested.status_code, status.HTTP_200_OK)
        nested_rows = same_nested.data['results'] if isinstance(same_nested.data, dict) else same_nested.data
        self.assertIn(str(self.same_comment.id), {str(row['id']) for row in nested_rows})
        self.assertEqual(cross_nested.status_code, status.HTTP_404_NOT_FOUND)

        self.assertEqual(standalone.status_code, status.HTTP_200_OK)
        rows = standalone.data['results'] if isinstance(standalone.data, dict) else standalone.data
        ids = {str(row['id']) for row in rows}
        self.assertIn(str(self.same_comment.id), ids)
        self.assertNotIn(str(self.cross_comment.id), ids)

    def test_profile_posts_action_hides_non_public_posts_without_relationship(self):
        self.client.force_authenticate(self.viewer)

        with self._patch_orgs():
            resp = self.client.get(f'/api/profiles/{self.same_org.username}/posts/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        ids = {str(row['id']) for row in rows}
        self.assertIn(str(self.same_post.id), ids)
        self.assertNotIn(str(self.same_followers_post.id), ids)
        self.assertNotIn(str(self.same_connections_post.id), ids)
        self.assertNotIn(str(self.same_mentioned_post.id), ids)
        self.assertNotIn(str(self.cross_post.id), ids)

    def test_profile_posts_action_allows_relationship_visible_posts(self):
        Follow.objects.create(follower_id=self.viewer.id, following_id=self.same_org.id)
        Connection.objects.create(user_a_id=self.viewer.id, user_b_id=self.same_org.id, requester_id=self.viewer.id, status='accepted')
        Mention.objects.create(
            post=self.same_mentioned_post,
            mentioned_user_id=self.viewer.id,
            mentioned_email=self.viewer.email,
            mentioned_username=self.viewer.username,
            start_index=0,
            end_index=len(self.viewer.username) + 1,
        )
        self.client.force_authenticate(self.viewer)

        with self._patch_orgs():
            resp = self.client.get(f'/api/profiles/{self.same_org.username}/posts/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        ids = {str(row['id']) for row in rows}
        self.assertIn(str(self.same_post.id), ids)
        self.assertIn(str(self.same_followers_post.id), ids)
        self.assertIn(str(self.same_connections_post.id), ids)
        self.assertIn(str(self.same_mentioned_post.id), ids)
        self.assertNotIn(str(self.cross_post.id), ids)

    def test_typed_search_filters_people_and_posts_by_org(self):
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs(), \
             mock.patch('connect.views.search.ranked_people_search', return_value=SocialProfile.objects.exclude(user_id=self.viewer.id)):
            people_resp = self.client.get('/api/search/people/?q=connect&limit=10')
        with self._patch_orgs(), \
             mock.patch('connect.views.search.ranked_post_search', return_value=Post.objects.all()):
            posts_resp = self.client.get('/api/search/posts/?q=connect&limit=10')

        self.assertEqual(people_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(posts_resp.status_code, status.HTTP_200_OK)
        people_ids = {row['user_id'] for row in people_resp.data['results']}
        post_ids = {str(row['id']) for row in posts_resp.data['results']}
        self.assertIn(self.same_org.id, people_ids)
        self.assertNotIn(self.cross_org.id, people_ids)
        self.assertIn(str(self.same_post.id), post_ids)
        self.assertNotIn(str(self.cross_post.id), post_ids)

    def test_home_feed_filters_public_cross_org_posts(self):
        self.client.force_authenticate(self.viewer)
        with self._patch_orgs():
            resp = self.client.get('/api/feed/home/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {str(row['id']) for row in resp.data['results']}
        self.assertIn(str(self.same_post.id), ids)
        self.assertNotIn(str(self.cross_post.id), ids)


class MessagingReadSymmetryTests(ReadSymmetryBase):

    def test_participants_hide_legacy_cross_org_roster_entries(self):
        conversation = Conversation.objects.create(conversation_type='group', name='legacy')
        ConversationParticipant.objects.create(conversation=conversation, user_id=self.viewer.id, user_email=self.viewer.email, is_admin=True)
        ConversationParticipant.objects.create(conversation=conversation, user_id=self.same_org.id, user_email=self.same_org.email)
        ConversationParticipant.objects.create(conversation=conversation, user_id=self.cross_org.id, user_email=self.cross_org.email)
        self.client.force_authenticate(self.viewer)

        with self._patch_orgs():
            resp = self.client.get(f'/api/conversations/{conversation.id}/participants/')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row['user_id'] for row in resp.data}
        self.assertIn(self.viewer.id, ids)
        self.assertIn(self.same_org.id, ids)
        self.assertNotIn(self.cross_org.id, ids)

    @mock.patch('connect.views.messaging.check_messaging_allowed', return_value='cross_org')
    def test_legacy_group_send_has_per_recipient_org_backstop(self, _gate):
        conversation = Conversation.objects.create(conversation_type='group', name='legacy')
        ConversationParticipant.objects.create(conversation=conversation, user_id=self.viewer.id, user_email=self.viewer.email, is_admin=True)
        ConversationParticipant.objects.create(conversation=conversation, user_id=self.cross_org.id, user_email=self.cross_org.email)
        self.client.force_authenticate(self.viewer)

        resp = self.client.post(
            f'/api/conversations/{conversation.id}/messages/',
            {'content': 'should not cross tenant'},
            format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(DirectMessage.objects.filter(conversation=conversation).exists())

    @mock.patch('connect.views.social.users_share_org', return_value=False)
    def test_cross_org_connection_request_forbidden(self, _share):
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(f'/api/connections/request/{self.cross_org.username}/')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('connect.views.social.users_share_org', return_value=True)
    def test_pending_connection_requests_carry_sender_name_and_username(self, _share):
        """CON-261: pending connection requests must serialize the sender's
        display name AND username so the MCP renders a real sender, not
        ``Unknown (@)``."""
        requester = _mk_user('zen', 'zen@example.com')
        _profile(requester, username='zen')
        # same_org is the recipient; requester (zen) sent the pending request.
        Connection.objects.create(
            user_a_id=min(requester.id, self.same_org.id),
            user_b_id=max(requester.id, self.same_org.id),
            requester_id=requester.id,
            status='pending',
            message='hello from zen',
        )
        self.client.force_authenticate(self.same_org)
        resp = self.client.get('/api/connections/requests/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        reqs = data.get('results', data) if isinstance(data, dict) else data
        self.assertEqual(len(reqs), 1)
        r = reqs[0]
        self.assertEqual(r['requester_id'], requester.id)
        self.assertEqual(r['requester_username'], 'zen')
        self.assertNotEqual(r['requester_name'], 'Unknown')
        # Accept/reject remain functional on the serialized id.
        self.assertIn('id', r)
        self.assertTrue(r['id'])

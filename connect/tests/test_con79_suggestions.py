"""
CON-79: GET /api/connect/suggestions/ — "People you might know".

Acceptance:
- returns >=5 suggestions for a 0-connection user
- ranked by reach (most-followed — source b)
- excludes self, already-connected, and already-followed users
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from connect.models import SocialProfile, Connection, Follow, Post

User = get_user_model()

URL = '/api/suggestions/'


def _profile(uid, name, followers=0, visibility='public'):
    return SocialProfile.objects.create(
        user_id=uid, user_email=f'{name}@x.com', username=name,
        display_name=name.title(), followers_count=followers,
        profile_visibility=visibility,
    )


class SuggestionsTests(APITestCase):
    def setUp(self):
        self.me = User.objects.create_user(
            username='ryo-con79', email='ryo-con79@example.com')
        self.client.force_authenticate(user=self.me)
        # The caller's own (public) profile — must never be suggested to itself.
        _profile(self.me.id, 'ryo-con79', followers=1)
        # 7 candidates with strictly increasing reach (followers 101..107).
        for uid in range(101, 108):
            _profile(uid, f'cand{uid}', followers=uid)

    def test_zero_connection_user_gets_at_least_five(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertGreaterEqual(resp.data['count'], 5)
        ids = [s['user_id'] for s in resp.data['suggestions']]
        self.assertNotIn(self.me.id, ids)  # never suggest self

    def test_ranked_by_followers_desc(self):
        resp = self.client.get(URL + '?limit=3')
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [s['user_id'] for s in resp.data['suggestions']]
        # most-followed first
        self.assertEqual(ids, [107, 106, 105])

    def test_excludes_connected_and_followed(self):
        # accepted connection with 107; caller follows 106
        Connection.objects.create(
            user_a_id=self.me.id, user_b_id=107,
            requester_id=self.me.id, status='accepted')
        Follow.objects.create(follower_id=self.me.id, following_id=106)
        resp = self.client.get(URL)
        ids = [s['user_id'] for s in resp.data['suggestions']]
        self.assertNotIn(107, ids)  # already connected
        self.assertNotIn(106, ids)  # already followed
        self.assertIn(105, ids)     # still suggested

    def test_excludes_pending_connection(self):
        # a pending request also counts as "already in flight" — don't re-suggest
        Connection.objects.create(
            user_a_id=self.me.id, user_b_id=104,
            requester_id=self.me.id, status='pending')
        resp = self.client.get(URL)
        ids = [s['user_id'] for s in resp.data['suggestions']]
        self.assertNotIn(104, ids)

    def test_sources_flags(self):
        resp = self.client.get(URL)
        self.assertTrue(resp.data['sources']['most_followed'])   # (b) applied
        self.assertTrue(resp.data['sources']['same_org'])        # CON-142 org/read-symmetry gate
        self.assertTrue(resp.data['sources']['active_posters'])  # CON-101

    def test_active_recent_poster_ranked_above_dormant_high_follower(self):
        # CON-101: cand 102 has LOW reach (followers=102) but posted recently;
        # cands 105-107 have higher reach but are dormant (no posts). The active
        # recent poster must be surfaced FIRST so a new user sees active people.
        Post.objects.create(author_id=102, author_email='cand102@x.com', content='hi')
        resp = self.client.get(URL + '?limit=4')
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [s['user_id'] for s in resp.data['suggestions']]
        self.assertEqual(ids[0], 102)  # active poster ranks first despite lower reach

    def test_no_recent_posts_falls_back_to_most_followed(self):
        # Zero recent posters → ordering is unchanged (pure most-followed), so the
        # active-poster source is a non-regression when nobody has posted.
        resp = self.client.get(URL + '?limit=3')
        ids = [s['user_id'] for s in resp.data['suggestions']]
        self.assertEqual(ids, [107, 106, 105])

"""
CON-142: object-level authorization across Connect write surfaces.

CON-139 audit found unscoped write/delete surfaces = plain cross-user IDOR: the
5 ModelViewSets (profile / post / comment / company / job) resolved an object by
URL against a non-owner-scoped queryset and had no object-ownership guard on
update/destroy, so any authenticated user (any org) could mutate another user's
object. Profile read also leaked user_email (PII) to every viewer.

These are negative tests: User B (a different user / non-admin) must NOT be able
to mutate User A's object, and the owner/admin still can.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import (SocialProfile, Post, Comment)

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class ProfileAuthzTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = _mk_user('owner_a', 'owner_a@shizuha.com')
        self.attacker = _mk_user('attacker_b', 'attacker_b@shizuha.com')
        self.profile = SocialProfile.objects.create(
            user_id=self.owner.id, user_email=self.owner.email,
            username='owner_a', display_name='Owner A',
        )

    def test_cross_user_profile_update_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.patch(f'/api/profiles/{self.profile.username}/', {'bio': 'pwned'}, format='json')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.profile.refresh_from_db()
        self.assertNotEqual(self.profile.bio, 'pwned')

    def test_cross_user_profile_delete_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.delete(f'/api/profiles/{self.profile.username}/')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertTrue(SocialProfile.objects.filter(pk=self.profile.pk).exists())

    def test_owner_can_update_own_profile(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.patch(f'/api/profiles/{self.profile.username}/', {'bio': 'mine'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, 'mine')

    def test_owner_sees_user_email(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f'/api/profiles/{self.profile.username}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data.get('user_email'), self.owner.email)

    @mock.patch('connect.services.privacy._membership_org_ids', return_value={1})
    @mock.patch('connect.services.privacy.users_share_org', return_value=False)
    def test_cross_org_profile_read_forbidden(self, _share, _orgs):
        self.client.force_authenticate(self.attacker)
        resp = self.client.get(f'/api/profiles/{self.profile.username}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch('connect.services.privacy._membership_org_ids', return_value={1})
    @mock.patch('connect.services.privacy.users_share_org', return_value=True)
    def test_user_email_visible_to_same_org_viewer(self, _share, _orgs):
        self.client.force_authenticate(self.attacker)
        resp = self.client.get(f'/api/profiles/{self.profile.username}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data.get('user_email'), self.owner.email)


class PostCommentAuthzTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = _mk_user('post_owner', 'post_owner@shizuha.com')
        self.attacker = _mk_user('post_attacker', 'post_attacker@shizuha.com')
        self.post = Post.objects.create(
            author_id=self.owner.id, author_email=self.owner.email,
            content='original', visibility='public',
        )
        self.comment = Comment.objects.create(
            post=self.post, author_id=self.owner.id,
            author_email=self.owner.email, content='original comment',
        )

    def test_cross_user_post_update_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.patch(f'/api/posts/{self.post.id}/', {'content': 'hijacked'}, format='json')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.post.refresh_from_db()
        self.assertEqual(self.post.content, 'original')

    def test_cross_user_post_delete_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.delete(f'/api/posts/{self.post.id}/')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertTrue(Post.objects.filter(pk=self.post.pk).exists())

    def test_owner_can_delete_own_post(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.delete(f'/api/posts/{self.post.id}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_cross_user_comment_update_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.patch(f'/api/comments/{self.comment.id}/', {'content': 'edited'}, format='json')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.content, 'original comment')

    def test_cross_user_comment_delete_forbidden(self):
        self.client.force_authenticate(self.attacker)
        resp = self.client.delete(f'/api/comments/{self.comment.id}/')
        # CON-142: cross-user writes are gated by org-scoped querysets, so an
        # out-of-scope object is 404 (do-not-leak-existence) rather than 403.
        # Either is a valid secure rejection; the resource-unchanged assertion
        # below is the actual security invariant.
        self.assertIn(resp.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())

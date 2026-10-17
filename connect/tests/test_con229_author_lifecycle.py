"""CON-229: author can address a followers post after create."""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from connect.models import Post

User = get_user_model()


class AuthorFollowersPostLifecycleTests(APITestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='author229', email='author229@example.com',
        )
        self.client.force_authenticate(self.author)

    def test_create_returns_id_and_detail_accepts_followers_post(self):
        created = self.client.post('/api/posts/', {
            'content': 'followers-only lifecycle',
            'visibility': 'followers',
        }, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        post_id = created.data.get('id')
        self.assertTrue(post_id)
        self.assertEqual(str(Post.objects.get(pk=post_id).visibility), 'followers')

        detail = self.client.get(f'/api/posts/{post_id}/')
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(str(detail.data['id']), str(post_id))

    def test_non_author_still_cannot_read_followers_post(self):
        post = Post.objects.create(
            author_id=self.author.id,
            author_email=self.author.email,
            content='hidden',
            visibility='followers',
        )
        stranger = User.objects.create_user(username='stranger229', email='stranger229@example.com')
        self.client.force_authenticate(stranger)
        with mock.patch('connect.services.privacy.users_share_org', return_value=False):
            detail = self.client.get(f'/api/posts/{post.id}/')
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

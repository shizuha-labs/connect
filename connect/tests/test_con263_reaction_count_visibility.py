"""CON-263: a successful react mutation must be visible in the post reaction
count through the real API consumer boundary.

Regression: react → read showed Reactions: 0 because the PostSerializer never
exposed a `reactions_count` field, so the MCP/read surfaces fell back to a
hardcoded 0. This test covers mutation + both the detail and list serializers.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import Post

User = get_user_model()


class PostReactionCountVisibilityTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='con263', email='con263@example.com',
        )
        self.post = Post.objects.create(
            author_id=self.user.id,
            author_email=self.user.email,
            author_username=self.user.username,
            content='CON-263 regression post',
        )
        self.client.force_authenticate(self.user)

    def test_react_then_read_shows_updated_count_and_unreact_restores_baseline(self):
        # Baseline: no reactions.
        detail = self.client.get(f'/api/posts/{self.post.id}/')
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data['reactions_count'], 0)
        self.assertIsNone(detail.data['user_reaction'])

        # React.
        react = self.client.post(
            f'/api/posts/{self.post.id}/react/',
            {'reaction_type': 'insightful'},
            format='json',
        )
        self.assertEqual(react.status_code, status.HTTP_200_OK)

        # Detail read reflects the mutation.
        detail = self.client.get(f'/api/posts/{self.post.id}/')
        self.assertEqual(detail.data['reactions_count'], 1)
        self.assertEqual(detail.data['user_reaction'], 'insightful')

        # List read reflects the mutation too.
        listing = self.client.get('/api/posts/', {'limit': 10})
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        results = listing.data.get('results', listing.data)
        post_row = next(
            (r for r in results if str(r['id']) == str(self.post.id)), None,
        )
        self.assertIsNotNone(post_row, 'post must appear in the list')
        self.assertEqual(post_row['reactions_count'], 1)

        # Unreact restores the baseline.
        unreact = self.client.post(f'/api/posts/{self.post.id}/unreact/', format='json')
        self.assertEqual(unreact.status_code, status.HTTP_200_OK)
        detail = self.client.get(f'/api/posts/{self.post.id}/')
        self.assertEqual(detail.data['reactions_count'], 0)
        self.assertIsNone(detail.data['user_reaction'])

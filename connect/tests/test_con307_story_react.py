"""CON-307: story react accepts the documented reaction_type (MCP contract).

Regression: connect_react_to_story POSTs {"reaction_type": "like"} but the
story react view read request.data.get('reaction') instead, so every MCP call
returned 400 "Reaction is required". The view now accepts reaction_type
(like/love/wow/haha, default 'like') with backward-compatible `reaction`
(emoji) fallback, and still fails closed when neither is supplied.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import Follow, Story, StoryReaction

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class StoryReactContractTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.author = _mk_user('con307_author', 'con307_author@example.com')
        self.viewer = _mk_user('con307_viewer', 'con307_viewer@example.com')
        self.story = Story.objects.create(
            author_id=self.author.id,
            author_email=self.author.email,
            author_username=self.author.username,
            story_type='text',
            text_content='con307 story',
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def _follow_author(self, user):
        Follow.objects.create(follower_id=user.id, following_id=self.author.id)

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_react_accepts_reaction_type_like(self, _share_org):
        """The documented reaction_type param (MCP contract) persists a reaction."""
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        response = self.client.post(
            f'/api/stories/{self.story.id}/react/',
            {'reaction_type': 'like'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['reaction'], 'like')
        self.assertEqual(response.data['message'], '')
        self.assertTrue(
            StoryReaction.objects.filter(
                story=self.story, user_id=self.viewer.id, reaction='like'
            ).exists()
        )

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_react_accepts_legacy_reaction_field(self, _share_org):
        """Backward compatibility: the legacy emoji `reaction` field still works."""
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        response = self.client.post(
            f'/api/stories/{self.story.id}/react/',
            {'reaction': '👍'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['reaction'], '👍')

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_react_still_fails_closed_without_reaction(self, _share_org):
        """No reaction_type and no reaction → 400 (unchanged fail-closed)."""
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        response = self.client.post(
            f'/api/stories/{self.story.id}/react/',
            {'message': 'hi'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'Reaction is required')

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_other_named_reaction_types_persist(self, _share_org):
        """love/wow/haha are accepted alongside like."""
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        for rt in ('love', 'wow', 'haha'):
            story = Story.objects.create(
                author_id=self.author.id,
                author_email=self.author.email,
                author_username=self.author.username,
                story_type='text',
                text_content=f'con307 {rt}',
                expires_at=timezone.now() + timezone.timedelta(hours=1),
            )
            response = self.client.post(
                f'/api/stories/{story.id}/react/',
                {'reaction_type': rt},
                format='json',
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data['reaction'], rt)

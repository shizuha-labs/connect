"""CON-306: Stories MCP<->API field-contract regression.

The MCP client sends content/caption and reads content/caption/author_name;
the backend serializer exposed text_content/author_username and omitted id from
create output, so stories rendered empty content and @Unknown author, and
create returned ID: None. These tests lock the contract aliases in place.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import Story

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class StoryContractTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _mk_user('story_contract', 'story_contract@example.com')
        self.client.force_authenticate(self.user)

    def _create_story(self, **payload):
        return self.client.post('/api/stories/', payload, format='json')

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_create_with_content_returns_canonical_id_and_persists_text(self, _share_org):
        """MCP create_story sends content/caption => response has id, row persists."""
        resp = self._create_story(
            content='CON-306 probe content',
            caption='CON-306 probe caption',
            background_color='#4A90D9',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        # Canonical story ID is returned (was ID: None before the fix).
        self.assertTrue(body.get('id'))
        story = Story.objects.get(id=body['id'])
        self.assertEqual(story.text_content, 'CON-306 probe content')
        self.assertEqual(story.story_type, 'text')
        # Response also exposes the contract aliases.
        self.assertEqual(body.get('content'), 'CON-306 probe content')
        self.assertEqual(body.get('caption'), 'CON-306 probe content')

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_get_story_renders_content_caption_and_author_name(self, _share_org):
        """get_story must render content/caption/author_name (not empty/@Unknown)."""
        story = Story.objects.create(
            author_id=self.user.id,
            author_email=self.user.email,
            author_username=self.user.username,
            story_type='text',
            text_content='hello story world',
            background_color='#4A90D9',
        )
        resp = self.client.get(f'/api/stories/{story.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['content'], 'hello story world')
        self.assertEqual(body['caption'], 'hello story world')
        self.assertEqual(body['author_name'], self.user.username)
        # Backward-compat fields remain.
        self.assertEqual(body['text_content'], 'hello story world')
        self.assertEqual(body['author_username'], self.user.username)

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_create_defaults_story_type_to_text_when_content_provided(self, _share_org):
        """story_type omitted + content provided => story_type defaults to text."""
        resp = self._create_story(content='just content, no type')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        story = Story.objects.get(id=resp.json()['id'])
        self.assertEqual(story.story_type, 'text')

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_my_stories_renders_content_and_author(self, _share_org):
        """get_my_stories renders content/caption/author_name for each story."""
        Story.objects.create(
            author_id=self.user.id,
            author_email=self.user.email,
            author_username=self.user.username,
            story_type='text',
            text_content='my story body',
        )
        resp = self.client.get('/api/stories/my_stories/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        stories = resp.json()
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]['content'], 'my story body')
        self.assertEqual(stories[0]['author_name'], self.user.username)

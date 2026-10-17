"""CON-150: remaining object-level authz gates for meetings and stories."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import (
    Connection,
    Follow,
    Meeting,
    MeetingInvite,
    MeetingParticipant,
    Story,
)

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class MeetingJoinByCodeAuthzTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = _mk_user('meeting_host', 'meeting_host@example.com')
        self.attacker = _mk_user('meeting_attacker', 'meeting_attacker@example.com')
        self.peer = _mk_user('meeting_peer', 'meeting_peer@example.com')
        self.meeting = Meeting.objects.create(
            title='Scoped Meeting',
            host_id=self.host.id,
            host_email=self.host.email,
            status='active',
        )

    @mock.patch('connect.services.livekit_service.LiveKitService.create_room_token')
    @mock.patch('connect.views.meetings.users_share_org', return_value=False)
    def test_code_only_cross_org_user_waits_without_token_and_cannot_retry_bypass(
        self,
        _share_org,
        mock_create_token,
    ):
        """Possessing a code alone must not mint a room token."""
        mock_create_token.return_value = {'token': 'should-not-be-used'}
        self.client.force_authenticate(self.attacker)

        for _ in range(2):
            response = self.client.post('/api/meetings/join-by-code/', {
                'meeting_code': self.meeting.meeting_code,
            }, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['status'], 'waiting')
            self.assertNotIn('token', response.data)

        participant = MeetingParticipant.objects.get(meeting=self.meeting, user_id=self.attacker.id)
        self.assertEqual(participant.status, 'waiting')
        mock_create_token.assert_not_called()

    @mock.patch('connect.services.livekit_service.LiveKitService.create_room_token')
    @mock.patch('connect.views.meetings.users_share_org', return_value=True)
    def test_same_org_user_can_join_by_code(self, _share_org, mock_create_token):
        mock_create_token.return_value = {
            'token': 'same-org-token',
            'url': 'ws://localhost:7880',
            'room_name': 'test-room',
            'participant_identity': 'user-peer',
        }
        self.client.force_authenticate(self.peer)

        response = self.client.post('/api/meetings/join-by-code/', {
            'meeting_code': self.meeting.meeting_code,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['token'], 'same-org-token')
        participant = MeetingParticipant.objects.get(meeting=self.meeting, user_id=self.peer.id)
        self.assertEqual(participant.status, 'joined')

    @mock.patch('connect.services.livekit_service.LiveKitService.create_room_token')
    @mock.patch('connect.views.meetings.users_share_org', return_value=False)
    def test_explicit_invite_can_join_by_code(self, _share_org, mock_create_token):
        mock_create_token.return_value = {
            'token': 'invite-token',
            'url': 'ws://localhost:7880',
            'room_name': 'test-room',
            'participant_identity': 'user-peer',
        }
        MeetingInvite.objects.create(
            meeting=self.meeting,
            invited_user_id=self.peer.id,
            invited_email=self.peer.email,
            inviter_id=self.host.id,
        )
        self.client.force_authenticate(self.peer)

        response = self.client.post('/api/meetings/join-by-code/', {
            'meeting_code': self.meeting.meeting_code,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['token'], 'invite-token')


class StoryAuthzTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.author = _mk_user('story_author', 'story_author@example.com')
        self.viewer = _mk_user('story_viewer', 'story_viewer@example.com')
        self.connection_viewer = _mk_user('story_connection', 'story_connection@example.com')
        self.story = Story.objects.create(
            author_id=self.author.id,
            author_email=self.author.email,
            author_username=self.author.username,
            story_type='text',
            text_content='private update',
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def _follow_author(self, user):
        Follow.objects.create(follower_id=user.id, following_id=self.author.id)

    @mock.patch('connect.views.stories.users_share_org', return_value=False)
    def test_unilateral_follow_cross_org_cannot_list_view_or_react_to_story(self, _share_org):
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        list_response = self.client.get('/api/stories/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data['results'], [])

        view_response = self.client.post(f'/api/stories/{self.story.id}/view/')
        self.assertEqual(view_response.status_code, status.HTTP_404_NOT_FOUND)

        react_response = self.client.post(
            f'/api/stories/{self.story.id}/react/',
            {'reaction': '👍'},
            format='json',
        )
        self.assertEqual(react_response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_same_org_follower_can_view_story(self, _share_org):
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        list_response = self.client.get('/api/stories/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data['results']), 1)

        view_response = self.client.post(f'/api/stories/{self.story.id}/view/')
        self.assertEqual(view_response.status_code, status.HTTP_200_OK)

    @mock.patch('connect.views.stories.users_share_org', return_value=False)
    def test_accepted_connection_can_view_story_cross_org(self, _share_org):
        self._follow_author(self.connection_viewer)
        Connection.objects.create(
            user_a_id=min(self.author.id, self.connection_viewer.id),
            user_b_id=max(self.author.id, self.connection_viewer.id),
            requester_id=self.connection_viewer.id,
            status='accepted',
        )
        self.client.force_authenticate(self.connection_viewer)

        list_response = self.client.get('/api/stories/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data['results']), 1)

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_connections_visibility_excludes_same_org_without_connection(self, _share_org):
        self.story.visibility = 'connections'
        self.story.save(update_fields=['visibility'])
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        list_response = self.client.get('/api/stories/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data['results'], [])

    @mock.patch('connect.views.stories.users_share_org', return_value=True)
    def test_same_org_non_author_cannot_patch_or_delete_story(self, _share_org):
        self._follow_author(self.viewer)
        self.client.force_authenticate(self.viewer)

        patch_response = self.client.patch(
            f'/api/stories/{self.story.id}/',
            {'text_content': 'attacker edit'},
            format='json',
        )
        self.assertEqual(patch_response.status_code, status.HTTP_403_FORBIDDEN)
        self.story.refresh_from_db()
        self.assertEqual(self.story.text_content, 'private update')

        delete_response = self.client.delete(f'/api/stories/{self.story.id}/')
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Story.objects.filter(pk=self.story.pk).exists())

    @mock.patch('connect.views.stories.users_share_org', return_value=False)
    def test_accepted_connection_non_author_cannot_patch_story(self, _share_org):
        self._follow_author(self.connection_viewer)
        Connection.objects.create(
            user_a_id=min(self.author.id, self.connection_viewer.id),
            user_b_id=max(self.author.id, self.connection_viewer.id),
            requester_id=self.connection_viewer.id,
            status='accepted',
        )
        self.client.force_authenticate(self.connection_viewer)

        patch_response = self.client.patch(
            f'/api/stories/{self.story.id}/',
            {'text_content': 'connection edit'},
            format='json',
        )
        self.assertEqual(patch_response.status_code, status.HTTP_403_FORBIDDEN)
        self.story.refresh_from_db()
        self.assertEqual(self.story.text_content, 'private update')

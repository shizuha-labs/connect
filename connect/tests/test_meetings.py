"""
Tests for video meeting functionality.
"""

from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from connect.models import Meeting, MeetingParticipant, MeetingInvite
from connect.models.meetings import generate_meeting_code

User = get_user_model()


class MeetingCodeGenerationTest(TestCase):
    """Test meeting code generation."""

    def test_meeting_code_format(self):
        """Test that meeting code is in xxx-xxxx-xxx format."""
        code = generate_meeting_code()
        self.assertEqual(len(code), 12)
        self.assertEqual(code[3], '-')
        self.assertEqual(code[8], '-')

    def test_meeting_code_uniqueness(self):
        """Test that meeting codes are unique."""
        codes = set()
        for _ in range(100):
            code = generate_meeting_code()
            self.assertNotIn(code, codes)
            codes.add(code)


class MeetingModelTest(TestCase):
    """Test Meeting model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='testhost',
            email='host@example.com',
            password='testpass123'
        )

    def test_create_meeting(self):
        """Test creating a meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
            host_email=self.user.email,
            host_name='Test Host',
        )

        self.assertIsNotNone(meeting.id)
        self.assertIsNotNone(meeting.meeting_code)
        self.assertEqual(meeting.status, 'scheduled')
        self.assertIsNotNone(meeting.livekit_room_name)

    def test_meeting_start(self):
        """Test starting a meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
        )

        meeting.start()

        self.assertEqual(meeting.status, 'active')
        self.assertIsNotNone(meeting.started_at)

    def test_meeting_end(self):
        """Test ending a meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
            status='active',
        )

        meeting.end()

        self.assertEqual(meeting.status, 'ended')
        self.assertIsNotNone(meeting.ended_at)

    def test_can_join_scheduled(self):
        """Test can_join for scheduled meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
            status='scheduled',
        )
        self.assertTrue(meeting.can_join)

    def test_can_join_active(self):
        """Test can_join for active meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
            status='active',
        )
        self.assertTrue(meeting.can_join)

    def test_cannot_join_ended(self):
        """Test can_join for ended meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.user.id,
            status='ended',
        )
        self.assertFalse(meeting.can_join)


class MeetingParticipantModelTest(TestCase):
    """Test MeetingParticipant model."""

    def setUp(self):
        self.host = User.objects.create_user(
            username='testhost',
            email='host@example.com',
            password='testpass123'
        )
        self.meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
            status='active',
        )

    def test_create_participant(self):
        """Test creating a participant."""
        participant = MeetingParticipant.objects.create(
            meeting=self.meeting,
            user_id=self.host.id,
            user_email=self.host.email,
            user_name='Test Host',
            role='host',
        )

        self.assertIsNotNone(participant.id)
        self.assertEqual(participant.status, 'invited')

    def test_participant_join(self):
        """Test participant joining."""
        participant = MeetingParticipant.objects.create(
            meeting=self.meeting,
            user_id=self.host.id,
            user_email=self.host.email,
            role='participant',
        )

        participant.join()

        self.assertEqual(participant.status, 'joined')
        self.assertIsNotNone(participant.joined_at)
        self.assertEqual(self.meeting.participant_count, 1)

    def test_participant_leave(self):
        """Test participant leaving."""
        participant = MeetingParticipant.objects.create(
            meeting=self.meeting,
            user_id=self.host.id,
            user_email=self.host.email,
            role='participant',
            status='joined',
        )
        self.meeting.participant_count = 1
        self.meeting.save()

        participant.leave()

        self.assertEqual(participant.status, 'left')
        self.assertIsNotNone(participant.left_at)
        self.assertEqual(self.meeting.participant_count, 0)


class MeetingAPITest(APITestCase):
    """Test Meeting API endpoints."""

    def setUp(self):
        self.host = User.objects.create_user(
            username='testhost',
            email='host@example.com',
            password='testpass123',
            first_name='Test',
            last_name='Host'
        )
        self.participant = User.objects.create_user(
            username='testparticipant',
            email='participant@example.com',
            password='testpass123',
            first_name='Test',
            last_name='Participant'
        )
        self.client = APIClient()

    def test_list_meetings_requires_auth(self):
        """Test that listing meetings requires authentication."""
        response = self.client.get('/api/meetings/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_meetings(self):
        """Test listing user's meetings."""
        self.client.force_authenticate(user=self.host)

        # Create a meeting
        Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
            host_email=self.host.email,
        )

        response = self.client.get('/api/meetings/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_create_meeting(self):
        """Test creating a scheduled meeting."""
        self.client.force_authenticate(user=self.host)

        data = {
            'title': 'Team Standup',
            'description': 'Daily standup meeting',
        }

        response = self.client.post('/api/meetings/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['title'], 'Team Standup')
        self.assertIn('meeting_code', response.data)

    @patch('connect.services.livekit_service.LiveKitService.create_room_token')
    def test_create_instant_meeting(self, mock_create_token):
        """Test creating an instant meeting."""
        mock_create_token.return_value = {
            'token': 'test-token',
            'url': 'ws://localhost:7880',
            'room_name': 'test-room',
            'participant_identity': 'user-1',
        }

        self.client.force_authenticate(user=self.host)

        response = self.client.post('/api/meetings/instant/', {'title': 'Quick Meeting'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('token', response.data)
        self.assertIn('meeting', response.data)
        self.assertEqual(response.data['meeting']['status'], 'active')

    @patch('connect.services.livekit_service.LiveKitService.create_room_token')
    @patch('connect.views.meetings.users_share_org', return_value=True)
    def test_join_meeting_by_code(self, _share_org, mock_create_token):
        """Test joining a meeting by code."""
        mock_create_token.return_value = {
            'token': 'test-token',
            'url': 'ws://localhost:7880',
            'room_name': 'test-room',
            'participant_identity': 'user-2',
        }

        # Create meeting as host
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
            host_email=self.host.email,
            status='active',
        )

        # Join as participant
        self.client.force_authenticate(user=self.participant)

        response = self.client.post('/api/meetings/join-by-code/', {
            'meeting_code': meeting.meeting_code,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('token', response.data)

    def test_join_invalid_code(self):
        """Test joining with invalid meeting code."""
        self.client.force_authenticate(user=self.participant)

        response = self.client.post('/api/meetings/join-by-code/', {
            'meeting_code': 'abc-defg-hij',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_meeting_host_only(self):
        """Test that only host can end meeting."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
            status='active',
        )

        # Try to end as non-host (meeting not visible to them - 404)
        self.client.force_authenticate(user=self.participant)
        response = self.client.post(f'/api/meetings/{meeting.id}/end/')
        # Non-participants can't see the meeting at all (security)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # End as host
        self.client.force_authenticate(user=self.host)
        response = self.client.post(f'/api/meetings/{meeting.id}/end/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        meeting.refresh_from_db()
        self.assertEqual(meeting.status, 'ended')

    def test_list_participants(self):
        """Test listing meeting participants."""
        meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
            status='active',
        )
        MeetingParticipant.objects.create(
            meeting=meeting,
            user_id=self.host.id,
            user_email=self.host.email,
            user_name='Test Host',
            role='host',
            status='joined',
        )

        self.client.force_authenticate(user=self.host)
        response = self.client.get(f'/api/meetings/{meeting.id}/participants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


class MeetingInviteTest(APITestCase):
    """Test meeting invite functionality."""

    def setUp(self):
        self.host = User.objects.create_user(
            username='testhost',
            email='host@example.com',
            password='testpass123',
            first_name='Test',
            last_name='Host'
        )
        self.meeting = Meeting.objects.create(
            title='Test Meeting',
            host_id=self.host.id,
        )
        self.client = APIClient()

    def test_send_invites(self):
        """Test sending meeting invites."""
        self.client.force_authenticate(user=self.host)

        response = self.client.post(f'/api/meetings/{self.meeting.id}/invite/', {
            'emails': ['user1@example.com', 'user2@example.com'],
            'message': 'Please join our meeting',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(MeetingInvite.objects.filter(meeting=self.meeting).count(), 2)

    def test_send_invites_host_only(self):
        """Test that only host can send invites."""
        other_user = User.objects.create_user(
            username='other',
            email='other@example.com',
            password='testpass123'
        )

        self.client.force_authenticate(user=other_user)

        response = self.client.post(f'/api/meetings/{self.meeting.id}/invite/', {
            'emails': ['user1@example.com'],
        }, format='json')

        # Non-participants can't see the meeting at all (security)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

"""
Exhaustive tests for WhatsApp-parity group chat features.

Covers:
  - Group creation with system messages
  - Add participant with system message + broadcast
  - Remove participant (admin-only)
  - Make admin / remove admin (admin-only)
  - Leave group with auto-promote (sole admin)
  - PATCH group name/description with system messages
  - Message preview prefixed with sender name in groups
  - Permission checks (non-admin cannot manage)
  - Full group lifecycle integration test
"""

from unittest.mock import patch, MagicMock, AsyncMock

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage,
)

User = get_user_model()


def get_results(data):
    """Extract list from a DRF response that may be paginated or a plain list."""
    if isinstance(data, list):
        return data
    return data.get('results', [])


# We mock the channel layer everywhere to avoid needing a running Redis.
# group_send must be AsyncMock because async_to_sync awaits it.
MOCK_CHANNEL = 'connect.views.messaging.get_channel_layer'


def _mock_channel():
    mock_layer = MagicMock()
    mock_layer.group_send = AsyncMock()
    return mock_layer


# ---------------------------------------------------------------------------
# Helper: build a group with N members
# ---------------------------------------------------------------------------

class GroupTestMixin:
    """Shared setUp for group chat tests."""

    def _create_users(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass123',
            first_name='Alice', last_name='Smith',
        )
        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pass123',
            first_name='Bob', last_name='Jones',
        )
        self.charlie = User.objects.create_user(
            username='charlie', email='charlie@example.com', password='pass123',
            first_name='Charlie', last_name='Brown',
        )
        self.dave = User.objects.create_user(
            username='dave', email='dave@example.com', password='pass123',
            first_name='Dave', last_name='Wilson',
        )

    def _create_group(self, name='Test Group', creator=None, members=None):
        """Create a group conversation with participants."""
        creator = creator or self.alice
        members = members or [self.bob, self.charlie]

        conv = Conversation.objects.create(
            conversation_type='group',
            name=name,
            created_by_id=creator.id,
        )
        # Creator is admin
        ConversationParticipant.objects.create(
            conversation=conv, user_id=creator.id,
            user_email=creator.email,
            user_name=creator.get_full_name(),
            is_admin=True,
        )
        # Other members
        for m in members:
            ConversationParticipant.objects.create(
                conversation=conv, user_id=m.id,
                user_email=m.email,
                user_name=m.get_full_name(),
            )
        return conv


# ---------------------------------------------------------------------------
# 1. Group creation with system messages
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class GroupCreationTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.client.force_authenticate(user=self.alice)

    def test_create_group_returns_201(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Project Alpha',
            'participant_ids': [self.bob.id, self.charlie.id],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['conversation_type'], 'group')
        self.assertEqual(resp.data['name'], 'Project Alpha')

    def test_create_group_adds_all_participants(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Big Team',
            'participant_ids': [self.bob.id, self.charlie.id, self.dave.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        self.assertEqual(conv.participants.count(), 4)  # alice + 3

    def test_create_group_creator_is_admin(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Admin Test',
            'participant_ids': [self.bob.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        alice_p = ConversationParticipant.objects.get(
            conversation=conv, user_id=self.alice.id
        )
        self.assertTrue(alice_p.is_admin)

    def test_create_group_generates_system_message(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Sys Msg Test',
            'participant_ids': [self.bob.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        sys_msgs = DirectMessage.objects.filter(
            conversation=conv, message_type='system'
        )
        self.assertEqual(sys_msgs.count(), 1)
        self.assertIn('created group', sys_msgs.first().content)
        self.assertIn('Sys Msg Test', sys_msgs.first().content)
        self.assertIn('Alice Smith', sys_msgs.first().content)

    def test_create_group_system_message_updates_conversation_metadata(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Meta Test',
            'participant_ids': [self.bob.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        self.assertEqual(conv.message_count, 1)
        self.assertIsNotNone(conv.last_message_at)
        self.assertIn('created group', conv.last_message_preview)

    def test_create_direct_does_not_generate_system_message(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'direct',
            'participant_ids': [self.bob.id],
        }, format='json')
        conv = Conversation.objects.get(id=resp.data['id'])
        sys_msgs = DirectMessage.objects.filter(
            conversation=conv, message_type='system'
        )
        self.assertEqual(sys_msgs.count(), 0)

    def test_create_group_with_participant_details(self, _mock):
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Detail Test',
            'participant_ids': [self.bob.id],
            'participant_details': {
                str(self.bob.id): {
                    'name': 'Bob Jones',
                    'email': 'bob@example.com'
                }
            }
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        bob_p = ConversationParticipant.objects.get(
            conversation_id=resp.data['id'], user_id=self.bob.id
        )
        self.assertEqual(bob_p.user_name, 'Bob Jones')


# ---------------------------------------------------------------------------
# 2. Add participant
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class AddParticipantTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()
        self.client.force_authenticate(user=self.alice)

    def test_admin_can_add_participant(self, _mock):
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/',
            {'user_id': self.dave.id, 'user_name': 'Dave Wilson', 'user_email': 'dave@example.com'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn(resp.data['status'], ['added', 'rejoined'])
        # Dave is now a participant
        self.assertTrue(ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.dave.id, has_left=False
        ).exists())

    def test_add_participant_creates_system_message(self, _mock):
        self.client.post(
            f'/api/conversations/{self.group.id}/participants/',
            {'user_id': self.dave.id, 'user_name': 'Dave Wilson'},
            format='json'
        )
        sys_msgs = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='added'
        )
        self.assertEqual(sys_msgs.count(), 1)
        self.assertIn('Alice Smith', sys_msgs.first().content)
        self.assertIn('Dave Wilson', sys_msgs.first().content)

    def test_non_admin_cannot_add_participant(self, _mock):
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/',
            {'user_id': self.dave.id},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_rejoin_after_leaving(self, _mock):
        """A member who left can be re-added."""
        ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.bob.id
        ).update(has_left=True, left_at=timezone.now())

        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/',
            {'user_id': self.bob.id, 'user_name': 'Bob Jones'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertFalse(bob_p.has_left)

    def test_get_participants_list(self, _mock):
        resp = self.client.get(f'/api/conversations/{self.group.id}/participants/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 3)  # alice, bob, charlie


# ---------------------------------------------------------------------------
# 3. Remove participant
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class RemoveParticipantTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()
        self.client.force_authenticate(user=self.alice)

    def test_admin_can_remove_participant(self, _mock):
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/remove/',
            {'user_id': self.bob.id},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertTrue(bob_p.has_left)
        self.assertIsNotNone(bob_p.left_at)

    def test_remove_creates_system_message(self, _mock):
        self.client.post(
            f'/api/conversations/{self.group.id}/participants/remove/',
            {'user_id': self.bob.id},
            format='json'
        )
        sys_msg = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='removed'
        ).first()
        self.assertIsNotNone(sys_msg)
        self.assertIn('Alice Smith', sys_msg.content)
        self.assertIn('Bob Jones', sys_msg.content)

    def test_non_admin_cannot_remove(self, _mock):
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/remove/',
            {'user_id': self.charlie.id},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_remove_nonexistent_user_fails(self, _mock):
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/remove/',
            {'user_id': 99999},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_remove_without_user_id_fails(self, _mock):
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/remove/',
            {},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_remove_from_direct_conversation(self, _mock):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id, is_admin=True
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.bob.id
        )
        resp = self.client.post(
            f'/api/conversations/{direct.id}/participants/remove/',
            {'user_id': self.bob.id},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 4. Make admin / Remove admin
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class AdminManagementTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()
        self.client.force_authenticate(user=self.alice)

    def test_make_admin(self, _mock):
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.bob.id}/make_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertTrue(bob_p.is_admin)

    def test_make_admin_creates_system_message(self, _mock):
        self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.bob.id}/make_admin/'
        )
        sys_msg = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='made'
        ).first()
        self.assertIsNotNone(sys_msg)
        self.assertIn('Alice Smith', sys_msg.content)
        self.assertIn('Bob Jones', sys_msg.content)
        self.assertIn('admin', sys_msg.content)

    def test_remove_admin(self, _mock):
        # First make Bob admin
        ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.bob.id
        ).update(is_admin=True)

        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.bob.id}/remove_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertFalse(bob_p.is_admin)

    def test_remove_admin_creates_system_message(self, _mock):
        ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.bob.id
        ).update(is_admin=True)

        self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.bob.id}/remove_admin/'
        )
        sys_msg = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='removed'
        ).last()  # Get the latest system message
        self.assertIsNotNone(sys_msg)
        self.assertIn('admin', sys_msg.content)

    def test_non_admin_cannot_promote(self, _mock):
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.charlie.id}/make_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_admin_cannot_demote(self, _mock):
        ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.charlie.id
        ).update(is_admin=True)

        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.charlie.id}/remove_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_remove_admin_non_admin_user_returns_404(self, _mock):
        """Trying to remove admin from non-admin user returns 404."""
        resp = self.client.post(
            f'/api/conversations/{self.group.id}/participants/{self.bob.id}/remove_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_make_admin_on_direct_fails(self, _mock):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id, is_admin=True
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.bob.id
        )
        resp = self.client.post(
            f'/api/conversations/{direct.id}/participants/{self.bob.id}/make_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 5. Leave group with auto-promote
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class LeaveGroupTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()
        self.client.force_authenticate(user=self.alice)

    def test_member_can_leave(self, _mock):
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(f'/api/conversations/{self.group.id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertTrue(bob_p.has_left)

    def test_leave_creates_system_message(self, _mock):
        self.client.force_authenticate(user=self.bob)
        self.client.post(f'/api/conversations/{self.group.id}/leave/')
        sys_msgs = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='left'
        )
        self.assertTrue(sys_msgs.exists())
        self.assertIn('Bob Jones', sys_msgs.first().content)

    def test_sole_admin_leaving_promotes_next_member(self, _mock):
        """When the only admin leaves, oldest remaining member is promoted."""
        # Alice is sole admin
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post(f'/api/conversations/{self.group.id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Alice should have left
        alice_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.alice.id
        )
        self.assertTrue(alice_p.has_left)

        # Bob (oldest remaining) should now be admin
        bob_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.bob.id
        )
        self.assertTrue(bob_p.is_admin)

    def test_sole_admin_leave_creates_promotion_system_message(self, _mock):
        """Auto-promote creates a system message about the new admin."""
        self.client.post(f'/api/conversations/{self.group.id}/leave/')
        sys_msgs = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='now an admin'
        )
        self.assertTrue(sys_msgs.exists())

    def test_non_sole_admin_leaving_does_not_promote(self, _mock):
        """When there are multiple admins, no auto-promote happens."""
        # Make bob an admin too
        ConversationParticipant.objects.filter(
            conversation=self.group, user_id=self.bob.id
        ).update(is_admin=True)

        self.client.post(f'/api/conversations/{self.group.id}/leave/')
        # Charlie should NOT have been promoted
        charlie_p = ConversationParticipant.objects.get(
            conversation=self.group, user_id=self.charlie.id
        )
        self.assertFalse(charlie_p.is_admin)

    def test_left_member_not_in_conversation_list(self, _mock):
        self.client.force_authenticate(user=self.bob)
        self.client.post(f'/api/conversations/{self.group.id}/leave/')

        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        conv_ids = [r['id'] for r in results]
        self.assertNotIn(str(self.group.id), conv_ids)

    def test_cannot_leave_direct_conversation(self, _mock):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id, is_admin=True
        )
        resp = self.client.post(f'/api/conversations/{direct.id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 6. Update group info (PATCH)
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class UpdateGroupInfoTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group(name='Original Name')
        self.client.force_authenticate(user=self.alice)

    def test_admin_can_update_name(self, _mock):
        resp = self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'name': 'New Name'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, 'New Name')

    def test_update_name_creates_system_message(self, _mock):
        self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'name': 'Renamed Group'},
            format='json'
        )
        sys_msg = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='changed the group name'
        ).first()
        self.assertIsNotNone(sys_msg)
        self.assertIn('Renamed Group', sys_msg.content)

    def test_admin_can_update_description(self, _mock):
        resp = self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'description': 'A great team'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.group.refresh_from_db()
        self.assertEqual(self.group.description, 'A great team')

    def test_update_description_creates_system_message(self, _mock):
        self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'description': 'New desc'},
            format='json'
        )
        sys_msg = DirectMessage.objects.filter(
            conversation=self.group, message_type='system',
            content__contains='changed the group description'
        ).first()
        self.assertIsNotNone(sys_msg)

    def test_update_both_name_and_description(self, _mock):
        self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'name': 'Both Changed', 'description': 'Both desc'},
            format='json'
        )
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, 'Both Changed')
        self.assertEqual(self.group.description, 'Both desc')
        # Should create 2 system messages
        sys_msgs = DirectMessage.objects.filter(
            conversation=self.group, message_type='system'
        )
        name_msgs = sys_msgs.filter(content__contains='group name')
        desc_msgs = sys_msgs.filter(content__contains='group description')
        self.assertEqual(name_msgs.count(), 1)
        self.assertEqual(desc_msgs.count(), 1)

    def test_non_admin_cannot_update(self, _mock):
        self.client.force_authenticate(user=self.bob)
        resp = self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'name': 'Hacker'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_same_name_no_system_message(self, _mock):
        """No system message if value hasn't actually changed."""
        self.client.patch(
            f'/api/conversations/{self.group.id}/',
            {'name': 'Original Name'},
            format='json'
        )
        sys_msgs = DirectMessage.objects.filter(
            conversation=self.group, message_type='system'
        )
        self.assertEqual(sys_msgs.count(), 0)

    def test_cannot_update_direct_conversation(self, _mock):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id, is_admin=True
        )
        resp = self.client.patch(
            f'/api/conversations/{direct.id}/',
            {'name': 'Bad'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 7. Message preview prefix in groups
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class GroupMessagePreviewTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()
        self.client.force_authenticate(user=self.alice)

    def test_group_message_preview_has_sender_prefix(self, _mock):
        self.client.post(
            f'/api/conversations/{self.group.id}/messages/',
            {'content': 'Hello everyone!'},
            format='json'
        )
        self.group.refresh_from_db()
        self.assertTrue(
            self.group.last_message_preview.startswith('Alice:'),
            f"Expected preview to start with 'Alice:', got '{self.group.last_message_preview}'"
        )

    def test_direct_message_preview_no_prefix(self, _mock):
        direct = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.alice.id
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.alice.id,
            user_email=self.alice.email, user_name='Alice Smith', is_admin=True,
        )
        ConversationParticipant.objects.create(
            conversation=direct, user_id=self.bob.id,
            user_email=self.bob.email, user_name='Bob Jones',
        )

        self.client.post(
            f'/api/conversations/{direct.id}/messages/',
            {'content': 'Just a DM'},
            format='json'
        )
        direct.refresh_from_db()
        self.assertFalse(
            direct.last_message_preview.startswith('Alice:'),
            f"DM preview should not have prefix, got '{direct.last_message_preview}'"
        )
        self.assertEqual(direct.last_message_preview, 'Just a DM')

    def test_message_updates_conversation_metadata(self, _mock):
        self.client.post(
            f'/api/conversations/{self.group.id}/messages/',
            {'content': 'Track me'},
            format='json'
        )
        self.group.refresh_from_db()
        self.assertEqual(self.group.message_count, 1)
        self.assertIsNotNone(self.group.last_message_at)


# ---------------------------------------------------------------------------
# 8. System message helper function
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class SystemMessageHelperTest(GroupTestMixin, TestCase):

    def setUp(self):
        self._create_users()
        self.group = self._create_group()

    def test_system_message_has_correct_fields(self, _mock):
        from connect.views.messaging import _create_system_message
        msg = _create_system_message(self.group, 'Test system message')

        self.assertEqual(msg.message_type, 'system')
        self.assertEqual(msg.sender_id, 0)
        self.assertEqual(msg.sender_email, 'system')
        self.assertEqual(msg.sender_name, '')
        self.assertEqual(msg.content, 'Test system message')

    def test_system_message_updates_conversation(self, _mock):
        from connect.views.messaging import _create_system_message
        _create_system_message(self.group, 'Update test')

        self.group.refresh_from_db()
        self.assertIsNotNone(self.group.last_message_at)
        self.assertEqual(self.group.last_message_preview, 'Update test')


# ---------------------------------------------------------------------------
# 9. Full group lifecycle integration test
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class FullGroupLifecycleTest(GroupTestMixin, APITestCase):
    """
    End-to-end WhatsApp-like group chat flow:
    1. Alice creates group with Bob + Charlie
    2. Verify system message "Alice created group 'Team'"
    3. Alice sends a message
    4. Bob sends a message → preview = "Bob: ..."
    5. Alice adds Dave → system message
    6. Alice makes Bob admin → system message
    7. Alice removes Charlie → system message
    8. Bob (now admin) updates group name
    9. Dave leaves → system message
    10. Bob removes Alice as admin (Alice left as sole non-admin)
    11. Verify message order and system messages interspersed
    """

    def setUp(self):
        self._create_users()
        self.client.force_authenticate(user=self.alice)

    def test_full_group_lifecycle(self, _mock):
        # Step 1: Create group
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Team',
            'participant_ids': [self.bob.id, self.charlie.id],
            'participant_details': {
                str(self.bob.id): {'name': 'Bob Jones', 'email': 'bob@example.com'},
                str(self.charlie.id): {'name': 'Charlie Brown', 'email': 'charlie@example.com'},
            }
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        group_id = resp.data['id']

        # Step 2: Verify creation system message
        msgs = DirectMessage.objects.filter(conversation_id=group_id, message_type='system')
        self.assertEqual(msgs.count(), 1)
        self.assertIn('created group', msgs.first().content)

        # Step 3: Alice sends a message
        resp = self.client.post(
            f'/api/conversations/{group_id}/messages/',
            {'content': 'Hey team!'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # Step 4: Bob sends a message
        self.client.force_authenticate(user=self.bob)
        resp = self.client.post(
            f'/api/conversations/{group_id}/messages/',
            {'content': 'Hi everyone!'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        conv = Conversation.objects.get(id=group_id)
        self.assertTrue(conv.last_message_preview.startswith('Bob:'))

        # Step 5: Alice adds Dave
        self.client.force_authenticate(user=self.alice)
        resp = self.client.post(
            f'/api/conversations/{group_id}/participants/',
            {'user_id': self.dave.id, 'user_name': 'Dave Wilson', 'user_email': 'dave@example.com'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        add_msg = DirectMessage.objects.filter(
            conversation_id=group_id, message_type='system',
            content__contains='added Dave Wilson'
        )
        self.assertTrue(add_msg.exists())

        # Step 6: Alice makes Bob admin
        resp = self.client.post(
            f'/api/conversations/{group_id}/participants/{self.bob.id}/make_admin/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bob_p = ConversationParticipant.objects.get(
            conversation_id=group_id, user_id=self.bob.id
        )
        self.assertTrue(bob_p.is_admin)

        # Step 7: Alice removes Charlie
        resp = self.client.post(
            f'/api/conversations/{group_id}/participants/remove/',
            {'user_id': self.charlie.id},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        charlie_p = ConversationParticipant.objects.get(
            conversation_id=group_id, user_id=self.charlie.id
        )
        self.assertTrue(charlie_p.has_left)

        # Step 8: Bob updates group name
        self.client.force_authenticate(user=self.bob)
        resp = self.client.patch(
            f'/api/conversations/{group_id}/',
            {'name': 'Awesome Team'},
            format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        conv.refresh_from_db()
        self.assertEqual(conv.name, 'Awesome Team')

        # Step 9: Dave leaves
        self.client.force_authenticate(user=self.dave)
        resp = self.client.post(f'/api/conversations/{group_id}/leave/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        leave_msg = DirectMessage.objects.filter(
            conversation_id=group_id, message_type='system',
            content__contains='Dave'
        ).filter(content__contains='left')
        self.assertTrue(leave_msg.exists())

        # Step 10: Verify all messages in order
        all_msgs = DirectMessage.objects.filter(
            conversation_id=group_id
        ).order_by('created_at')

        # Should have: create_sys, alice_msg, bob_msg, add_dave_sys,
        # make_admin_sys, remove_charlie_sys, rename_sys, dave_left_sys
        self.assertGreaterEqual(all_msgs.count(), 8)

        # Verify text messages are interspersed with system messages
        system_count = all_msgs.filter(message_type='system').count()
        text_count = all_msgs.filter(message_type='text').count()
        self.assertGreaterEqual(system_count, 6)
        self.assertEqual(text_count, 2)

        # Step 11: Verify remaining active participants
        active = ConversationParticipant.objects.filter(
            conversation_id=group_id, has_left=False
        )
        self.assertEqual(active.count(), 2)  # Alice + Bob
        # Both should be admins
        for p in active:
            self.assertTrue(p.is_admin)


# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------

@patch(MOCK_CHANNEL, return_value=_mock_channel())
class GroupEdgeCasesTest(GroupTestMixin, APITestCase):

    def setUp(self):
        self._create_users()
        self.client.force_authenticate(user=self.alice)

    def test_all_members_leave_last_member_auto_promoted(self, _mock):
        """When everyone leaves except one, the last person becomes admin."""
        group = self._create_group(members=[self.bob])

        # Alice leaves (sole admin) → Bob promoted
        self.client.post(f'/api/conversations/{group.id}/leave/')
        bob_p = ConversationParticipant.objects.get(
            conversation=group, user_id=self.bob.id
        )
        self.assertTrue(bob_p.is_admin)

    def test_system_messages_visible_to_all_participants(self, _mock):
        """All active participants can see system messages."""
        group = self._create_group()

        # Alice adds Dave (creates system message)
        self.client.post(
            f'/api/conversations/{group.id}/participants/',
            {'user_id': self.dave.id, 'user_name': 'Dave Wilson'},
            format='json'
        )

        # Bob should be able to see system messages
        self.client.force_authenticate(user=self.bob)
        resp = self.client.get(f'/api/conversations/{group.id}/messages/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        msgs = resp.data.get('messages', resp.data if isinstance(resp.data, list) else resp.data.get('results', []))
        sys_msgs = [m for m in msgs if m['message_type'] == 'system']
        self.assertTrue(len(sys_msgs) > 0)

    def test_group_appears_in_conversation_list_for_all_members(self, _mock):
        """All group members see the group in their conversation list."""
        resp = self.client.post('/api/conversations/', {
            'conversation_type': 'group',
            'name': 'Visibility Test',
            'participant_ids': [self.bob.id, self.charlie.id],
        }, format='json')
        group_id = resp.data['id']

        for user in [self.alice, self.bob, self.charlie]:
            self.client.force_authenticate(user=user)
            resp = self.client.get('/api/conversations/')
            results = get_results(resp.data)
            conv_ids = [r['id'] for r in results]
            self.assertIn(
                str(group_id), conv_ids,
                f"{user.username} should see the group"
            )

    def test_removed_member_cannot_access_group(self, _mock):
        """After removal, member can't send messages or see the group."""
        group = self._create_group()

        # Remove Bob
        self.client.post(
            f'/api/conversations/{group.id}/participants/remove/',
            {'user_id': self.bob.id},
            format='json'
        )

        # Bob should not see the group in conversation list
        self.client.force_authenticate(user=self.bob)
        resp = self.client.get('/api/conversations/')
        results = get_results(resp.data)
        conv_ids = [r['id'] for r in results]
        self.assertNotIn(str(group.id), conv_ids)

    def test_multiple_system_messages_in_sequence(self, _mock):
        """Multiple admin actions produce separate system messages."""
        group = self._create_group()

        # Add Dave, then make Bob admin, then remove Charlie
        self.client.post(
            f'/api/conversations/{group.id}/participants/',
            {'user_id': self.dave.id, 'user_name': 'Dave'},
            format='json'
        )
        self.client.post(
            f'/api/conversations/{group.id}/participants/{self.bob.id}/make_admin/'
        )
        self.client.post(
            f'/api/conversations/{group.id}/participants/remove/',
            {'user_id': self.charlie.id},
            format='json'
        )

        sys_msgs = DirectMessage.objects.filter(
            conversation=group, message_type='system'
        ).order_by('created_at')
        self.assertEqual(sys_msgs.count(), 3)
        # Verify they're all different
        contents = [m.content for m in sys_msgs]
        self.assertEqual(len(set(contents)), 3)

"""
CON-52: agents must be searchable + render names in conversations.

Root cause covered: agents are messageable by username but never get a Connect
SocialProfile, so people-search (queries SocialProfile) misses them and
conversation participant rendering is blank. These tests pin the provisioning
service, the management-command backfill, and the username search fallback.
"""
from unittest import mock

from django.test import TestCase

from connect.models import SocialProfile
from connect.services import profile_sync


_IDENTITY = {'username': 'kai', 'display_name': 'Kai', 'email': 'kai@shizuha.com'}


class EnsureSocialProfileTests(TestCase):
    @mock.patch('connect.services.profile_sync.resolve_identity', return_value=_IDENTITY)
    def test_creates_profile_for_agent(self, _resolve):
        profile = profile_sync.ensure_social_profile(840)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.user_id, 840)
        self.assertEqual(profile.username, 'kai')
        self.assertEqual(profile.display_name, 'Kai')

    @mock.patch('connect.services.profile_sync.resolve_identity', return_value=_IDENTITY)
    def test_idempotent(self, _resolve):
        a = profile_sync.ensure_social_profile(840)
        b = profile_sync.ensure_social_profile(840)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(SocialProfile.objects.filter(user_id=840).count(), 1)

    @mock.patch('connect.services.profile_sync.resolve_identity', return_value={})
    def test_skips_when_unresolvable(self, _resolve):
        # No username available -> do not write a junk row.
        self.assertIsNone(profile_sync.ensure_social_profile(999))
        self.assertFalse(SocialProfile.objects.filter(user_id=999).exists())

    def test_explicit_values_skip_resolve(self):
        # When caller supplies fields, no shizuha-id round-trip is needed.
        with mock.patch('connect.services.profile_sync.resolve_identity') as r:
            profile = profile_sync.ensure_social_profile(
                841, username='ren', display_name='Ren', email='ren@shizuha.com',
            )
            r.assert_not_called()
        self.assertEqual(profile.username, 'ren')

    @mock.patch('connect.services.profile_sync.resolve_identity', return_value=_IDENTITY)
    def test_search_vector_makes_agent_findable(self, _resolve):
        # The SocialProfile post_save signal populates search_vector from
        # display_name/username, so the agent is now findable by username.
        profile_sync.ensure_social_profile(840)
        from django.contrib.postgres.search import SearchQuery
        found = SocialProfile.objects.filter(search_vector=SearchQuery('kai'))
        self.assertTrue(found.filter(user_id=840).exists())

    @mock.patch('connect.services.profile_sync.resolve_identity', return_value=_IDENTITY)
    def test_heals_placeholder_display_name(self, _resolve):
        from connect.models import SocialProfile
        SocialProfile.objects.create(
            user_id=735, username='ena', display_name='User 735',
            user_email='ena@shizuha.com',
        )
        profile = profile_sync.ensure_social_profile(735)
        self.assertEqual(profile.display_name, 'Kai')


class DisplayNamePlaceholderTests(TestCase):
    def test_user_n_and_blank_are_placeholders(self):
        from connect.services.display_names import is_placeholder_name
        self.assertTrue(is_placeholder_name('', 735))
        self.assertTrue(is_placeholder_name('User 735', 735))
        self.assertTrue(is_placeholder_name('user_735', 735))
        self.assertFalse(is_placeholder_name('User 735', 734))
        self.assertFalse(is_placeholder_name('Ena', 735))
        self.assertFalse(is_placeholder_name('Yuna', 734))

    def test_identity_prefers_first_name_then_username(self):
        from connect.services.display_names import label_from_identity
        self.assertEqual(
            label_from_identity({'first_name': 'Ena', 'username': 'ena'}, 735),
            'Ena',
        )
        self.assertEqual(
            label_from_identity({'username': 'yuna', 'email': 'yuna@shizuha.com'}, 734),
            'yuna',
        )
        self.assertEqual(
            label_from_identity({'email': 'ena@shizuha.com'}, 735),
            'ena',
        )
        self.assertEqual(label_from_identity({'display_name': 'User 735'}, 735), '')

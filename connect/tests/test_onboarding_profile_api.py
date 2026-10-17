from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import SocialProfile

User = get_user_model()


class ConnectProfileBootstrapTests(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def test_me_allocates_unique_username_instead_of_500(self):
        # The Connect username 'yuki' is already claimed by another user's
        # SocialProfile. Auth usernames are globally unique, so the incumbent
        # owner carries a distinct auth username — what collides is the derived
        # *profile* handle, which is exactly what /api/me/ must resolve.
        existing_user = User.objects.create_user(username='yuki-legacy', email='old-yuki@example.com')
        SocialProfile.objects.create(
            user_id=existing_user.id,
            user_email=existing_user.email,
            username='yuki',
            display_name='Existing Yuki',
        )
        new_user = User.objects.create_user(username='yuki', email='new-yuki@example.com')
        self.client.force_authenticate(new_user)

        response = self.client.get('/api/me/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user_id'], new_user.id)
        self.assertNotEqual(response.data['username'], 'yuki')
        self.assertTrue(response.data['username'].startswith('yuki-'))

    def test_onboarding_endpoints_return_json_for_new_user(self):
        user = User.objects.create_user(username='newbie', email='newbie@example.com')
        self.client.force_authenticate(user)

        status_response = self.client.get('/api/onboarding/status/')
        suggestions_response = self.client.get('/api/onboarding/suggestions/?limit=8')

        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertIn('steps', status_response.data)
        self.assertEqual(suggestions_response.status_code, status.HTTP_200_OK)
        self.assertIn('suggestions', suggestions_response.data)

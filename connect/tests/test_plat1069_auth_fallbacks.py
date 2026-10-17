from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from unittest.mock import patch

from connect.authentication import FederatedUser, ShizuhaIDAuthentication


class ShizuhaIDAuthenticationFallbackTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth = ShizuhaIDAuthentication()
        self.local_user = get_user_model().objects.create_user(
            id=42,
            username='local-collision',
            email='local-collision@connect.local',
        )

    def _request(self):
        return self.factory.get('/api/messaging/inbox/', HTTP_AUTHORIZATION='Bearer rs256.token')

    @patch('connect.authentication.ShizuhaJWTAuthentication.get_validated_token')
    def test_email_claim_returns_federated_user_before_local_pk_lookup(self, mock_validate):
        mock_validate.return_value = {
            'user_id': self.local_user.id,
            'email': 'human@shizuha.com',
            'username': 'human',
        }

        user, _ = self.auth.authenticate(self._request())

        self.assertIsInstance(user, FederatedUser)
        self.assertEqual(user.id, self.local_user.id)
        self.assertEqual(user.email, 'human@shizuha.com')
        self.assertNotEqual(user.email, self.local_user.email)

    @patch('connect.authentication.ShizuhaJWTAuthentication.get_validated_token')
    def test_no_email_legacy_rs256_token_can_still_use_local_user(self, mock_validate):
        mock_validate.return_value = {
            'user_id': self.local_user.id,
            'username': 'legacy-local',
        }

        user, _ = self.auth.authenticate(self._request())

        self.assertEqual(user.pk, self.local_user.pk)
        self.assertEqual(user.email, self.local_user.email)

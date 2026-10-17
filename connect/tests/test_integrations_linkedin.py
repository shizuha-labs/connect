import os

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from connect.linkedin_credentials import load_linkedin_credentials, public_linkedin_status
from connect.models.integrations import ExternalAccount

User = get_user_model()


class LinkedInCredentialsHelperTest(SimpleTestCase):
    def test_env_credentials_never_leak_into_public_status(self):
        os.environ['LINKEDIN_EMAIL'] = 'linkedin-owner@example.test'
        os.environ['LINKEDIN_PASSWORD'] = 'not-the-real-password'
        try:
            row = load_linkedin_credentials()
            self.assertEqual(row['email'], 'linkedin-owner@example.test')
            pub = public_linkedin_status()
            self.assertTrue(pub['has_credentials'])
            self.assertEqual(pub['email'], 'linkedin-owner@example.test')
            self.assertNotIn('password', pub)
        finally:
            os.environ.pop('LINKEDIN_EMAIL', None)
            os.environ.pop('LINKEDIN_PASSWORD', None)


class LinkedInIntegrationApiTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='fixture-owner', email='jdoe@example.com')
        self.other = User.objects.create_user(username='other', email='other@example.com')
        self.client = APIClient()
        ExternalAccount.objects.create(
            user_id=self.user.id,
            provider='linkedin',
            status='connected',
            email='linkedin-owner@example.test',
            secret_ref='k8s:connect-linkedin-agent',
            capabilities=['jobs', 'apply'],
        )

    def test_owner_sees_linkedin_binding_without_password(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get('/api/integrations/linkedin/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['provider'], 'linkedin')
        self.assertEqual(resp.data['status'], 'connected')
        self.assertEqual(resp.data['email'], 'linkedin-owner@example.test')
        self.assertNotIn('password', resp.data)
        listed = self.client.get('/api/integrations/')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data['results']), 1)

    def test_other_user_does_not_see_owner_binding(self):
        self.client.force_authenticate(self.other)
        resp = self.client.get('/api/integrations/linkedin/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'disconnected')
        listed = self.client.get('/api/integrations/')
        self.assertEqual(listed.data['results'], [])

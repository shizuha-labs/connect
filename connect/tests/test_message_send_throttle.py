"""Agents and reads are not billed against the human 60/min send bucket."""
from django.contrib.auth.models import User
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from connect.views.messaging import MessageSendThrottle


class MessageSendThrottleExemptTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.throttle = MessageSendThrottle()

    def test_agent_posts_are_never_throttled(self):
        req = self.factory.post('/api/conversations/x/messages/', {'content': 'pong'}, format='json')
        req.user = User(id=734, username='yuna')
        req.auth = {'is_agent': True}
        for _ in range(80):
            self.assertTrue(self.throttle.allow_request(req, None))

    def test_human_reads_are_never_throttled(self):
        req = self.factory.get('/api/conversations/x/messages/')
        req.user = User(id=3, username='hritik')
        req.auth = {'is_agent': False}
        for _ in range(80):
            self.assertTrue(self.throttle.allow_request(req, None))

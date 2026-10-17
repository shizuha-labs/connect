from unittest.mock import patch
import os

from asgiref.sync import async_to_sync
from django.test import RequestFactory, SimpleTestCase, override_settings

from connectmanager.urls import ws_health_check


class _SyncNewChannelLayer:
    def __init__(self):
        self.messages = {}
        self.prefixes = []

    def new_channel(self, prefix='specific'):
        self.prefixes.append(prefix)
        return f'{prefix}!test'

    async def send(self, channel, message):
        self.messages[channel] = message

    async def receive(self, channel):
        return self.messages.pop(channel)


class _AsyncNewChannelLayer(_SyncNewChannelLayer):
    async def new_channel(self, prefix='specific'):
        return super().new_channel(prefix)


class _BrokenLayer(_SyncNewChannelLayer):
    async def send(self, channel, message):
        raise TypeError('boom')


@override_settings(ROOT_URLCONF='connectmanager.urls')
class WsHealthCheckTest(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get('/api/ws-health/')

    def _call(self, layer):
        with patch('channels.layers.get_channel_layer', return_value=layer):
            return async_to_sync(ws_health_check)(self.request)

    def test_accepts_sync_new_channel_implementations(self):
        layer = _SyncNewChannelLayer()
        response = self._call(layer)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'healthy', response.content)
        self.assertEqual(layer.prefixes, ['healthz'])

    def test_accepts_async_new_channel_implementations(self):
        response = self._call(_AsyncNewChannelLayer())

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'healthy', response.content)

    def test_soft_passes_channel_layer_error_by_default(self):
        response = self._call(_BrokenLayer())

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'degraded', response.content)
        self.assertIn(b'channel_layer_error', response.content)
        self.assertIn(b'TypeError', response.content)
        self.assertIn(b'readiness_soft_fail', response.content)

    def test_can_fail_closed_on_channel_layer_error(self):
        with patch.dict(os.environ, {'WS_HEALTH_CHANNEL_SOFT_FAIL': '0'}):
            response = self._call(_BrokenLayer())

        self.assertEqual(response.status_code, 503)
        self.assertIn(b'channel_layer_error', response.content)
        self.assertIn(b'TypeError', response.content)

    def test_recycles_channel_layer_cache_on_failure(self):
        # CON-107: on a round-trip failure, the cached channel-layer backend is
        # evicted so the next get_channel_layer() rebuilds fresh redis pools — the
        # self-heal that lets /api/ws-health/ recover after a redis blip without a
        # pod restart.
        import channels.layers as clayers

        class _Mgr:
            def __init__(self):
                self.backends = {'default': object()}

        mgr = _Mgr()
        with patch.object(clayers, 'channel_layers', mgr):
            response = self._call(_BrokenLayer())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mgr.backends, {})  # cache cleared -> next probe reconnects fresh

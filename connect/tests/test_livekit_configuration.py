from django.test import SimpleTestCase, override_settings
from connect.services.livekit_service import LiveKitService, LiveKitUnavailable


class LiveKitConfigurationTests(SimpleTestCase):
    @override_settings(LIVEKIT_API_KEY='', LIVEKIT_API_SECRET='')
    def test_unconfigured_video_never_mints_demo_tokens(self):
        service = LiveKitService()
        with self.assertRaises(LiveKitUnavailable) as error:
            service.create_token('fixture-room', 'fixture-user')
        self.assertEqual(error.exception.status_code, 503)

    @override_settings(LIVEKIT_API_KEY='fixture-key', LIVEKIT_API_SECRET='')
    def test_partial_configuration_fails_closed(self):
        with self.assertRaises(LiveKitUnavailable):
            LiveKitService().create_token('fixture-room', 'fixture-user')

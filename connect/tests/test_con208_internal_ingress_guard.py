"""CON-208: /api/internal/ must be unreachable through the public ingress.

The endpoints were guarded only by the spoofable X-Internal-Service header
while the ingress also exposed them publicly. InternalApiIngressGuardMiddleware
404s any internal-path request that arrived through the ingress (detected by
the nginx-injected X-Forwarded-For / X-Real-IP headers), while direct
in-cluster Service calls (no such headers) still reach the view.
"""
from rest_framework.test import APITestCase, APIClient

INTERNAL_PATH = '/api/internal/system-message/'
SPOOFED = {'HTTP_X_INTERNAL_SERVICE': 'shizuha-pulse'}


class InternalApiIngressGuardTests(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def test_ingress_borne_request_is_404_even_with_spoofed_service_header(self):
        # X-Forwarded-For present == arrived through the public ingress.
        resp = self.client.post(
            INTERNAL_PATH, {}, format='json',
            HTTP_X_FORWARDED_FOR='203.0.113.7', **SPOOFED,
        )
        self.assertEqual(resp.status_code, 404)

    def test_x_real_ip_also_blocks(self):
        resp = self.client.post(
            INTERNAL_PATH, {}, format='json',
            HTTP_X_REAL_IP='203.0.113.7', **SPOOFED,
        )
        self.assertEqual(resp.status_code, 404)

    def test_in_cluster_call_without_forwarding_headers_reaches_view(self):
        # No XFF / X-Real-IP == direct in-cluster Service call: guard must NOT
        # block. With empty body the view runs its own field validation (400),
        # which proves the request passed the ingress guard (would be 404 if
        # blocked). This is the CON-208 exploit shape, now only reachable
        # in-cluster.
        resp = self.client.post(INTERNAL_PATH, {}, format='json', **SPOOFED)
        self.assertNotEqual(resp.status_code, 404)
        self.assertEqual(resp.status_code, 400)

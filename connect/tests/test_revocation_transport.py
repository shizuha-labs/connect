"""Exercise real HTTP revocation transport through the JWT authentication caller."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework_simplejwt.exceptions import InvalidToken

from connect.authentication import ShizuhaJWTAuthentication
from connect import authentication as auth
from connect.tests.test_jwks_canonical_verifier import _make_rsa, _rs256, KID
import jwt
from connect import token_revocation as revocation


class RevocationTransportTests(SimpleTestCase):
    def setUp(self):
        self.responses = []
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                status, body = owner.responses.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302, 307, 308):
                    self.send_header('Location', body['location'])
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/oauth/revocation-status'
        self.env = patch.dict(os.environ, {
            'SHIZUHA_REVOCATION_STATUS_URL': self.url,
            'SHIZUHA_EXPECTED_ISSUER': 'https://identity.example/id',
        })
        self.env.start()
        revocation._REVOCATION_CACHE.clear()
        private_key, public_key = _make_rsa()
        self.private_key = private_key
        self.old_jwks = dict(auth._JWKS_CACHE)
        auth._JWKS_CACHE["keys"] = {KID: jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).prepare_key(public_key.decode())}
        auth._JWKS_CACHE["fetched_at"] = time.time()
        self.raw = _rs256(private_key, extra_claims={"client_id": "connect"}).encode()
        self.auth = ShizuhaJWTAuthentication()

    def tearDown(self):
        auth._JWKS_CACHE.clear()
        auth._JWKS_CACHE.update(self.old_jwks)
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        revocation._REVOCATION_CACHE.clear()

    def test_unavailable_lookup_denies_but_next_request_recovers(self):
        self.responses.extend([(503, {}), (200, {'active': True})])
        with self.assertRaises(InvalidToken):
            self.auth.get_validated_token(self.raw)
        self.assertNotIn(revocation._cache_key(self.raw.decode()), revocation._REVOCATION_CACHE)
        self.auth.get_validated_token(self.raw)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], ('/api/oauth/revocation-status', {'token': self.raw.decode()}))
        self.assertEqual(os.environ['SHIZUHA_EXPECTED_ISSUER'], 'https://identity.example/id')

    def test_real_revocation_still_denies_and_is_cached(self):
        self.responses.extend([(200, {'active': True}), (200, {'active': False})])
        self.auth.get_validated_token(self.raw)
        revocation._REVOCATION_CACHE[revocation._cache_key(self.raw.decode())] = (time.monotonic()-1, False)
        for _ in range(2):
            with self.assertRaises(InvalidToken):
                self.auth.get_validated_token(self.raw)
        self.assertEqual(len(self.calls), 2)

    def test_malformed_active_never_authorizes_or_poisons_cache(self):
        self.responses.extend([(200, []), (200, {'active': 'false'}), (200, {'active': True})])
        for _ in range(2):
            with self.assertRaises(InvalidToken):
                self.auth.get_validated_token(self.raw)
            self.assertFalse(revocation._REVOCATION_CACHE)
        self.auth.get_validated_token(self.raw)

    def test_legacy_issuer_url_remains_compatible_when_unconfigured(self):
        with patch.dict(os.environ, {'SHIZUHA_REVOCATION_STATUS_URL': ''}):
            self.assertEqual(revocation._revocation_status_url(), 'https://identity.example/id/api/oauth/revocation-status')

    def test_redirect_is_denied_without_forwarding_token_or_caching(self):
        self.responses.extend([(307, {'location': self.url + '/redirected'}), (200, {'active': True})])
        with self.assertRaises(InvalidToken):
            self.auth.get_validated_token(self.raw)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(revocation._REVOCATION_CACHE)
        self.auth.get_validated_token(self.raw)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(path == '/api/oauth/revocation-status' for path, _ in self.calls))

    def session_token(self, **claims):
        return _rs256(self.private_key, extra_claims=claims).encode()

    def test_session_without_client_id_consults_authority_and_recovers(self):
        raw = self.session_token(token_version=7)
        self.responses.extend([(503, {}), (200, {'active': True, 'token_version': 7})])
        with self.assertRaises(InvalidToken):
            self.auth.get_validated_token(raw)
        self.assertFalse(revocation._REVOCATION_CACHE)
        self.auth.get_validated_token(raw)
        self.assertEqual(len(self.calls), 2)

    def test_stale_session_version_denied_between_active_requests(self):
        active = self.session_token(token_version=7)
        stale = self.session_token(token_version=6)
        self.responses.extend([(200, {'active': True, 'token_version': 7})] * 2)
        self.auth.get_validated_token(active)
        with self.assertRaises(InvalidToken):
            self.auth.get_validated_token(stale)
        self.auth.get_validated_token(active)
        self.assertEqual(len(self.calls), 2)

    def test_session_revocation_after_cache_expiry_denies(self):
        raw = self.session_token(token_version=7)
        self.responses.extend([(200, {'active': True, 'token_version': 7}), (200, {'active': False})])
        self.auth.get_validated_token(raw)
        revocation._REVOCATION_CACHE[revocation._cache_key(raw.decode())] = (time.monotonic()-1, False)
        for _ in range(2):
            with self.assertRaises(InvalidToken):
                self.auth.get_validated_token(raw)
        self.assertEqual(len(self.calls), 2)

    def test_malformed_present_version_denies_uncached(self):
        for field in ('claim', 'response'):
            for bad in ('7', True, 7.5, [], {}):
                with self.subTest(field=field, bad=bad):
                    revocation._REVOCATION_CACHE.clear()
                    raw = self.session_token(token_version=bad if field == 'claim' else 7)
                    self.responses.append((200, {'active': True, 'token_version': bad if field == 'response' else 7}))
                    with self.assertRaises(InvalidToken):
                        self.auth.get_validated_token(raw)
                    self.assertFalse(revocation._REVOCATION_CACHE)

    def test_absent_version_remains_compatible_with_authoritative_active(self):
        for claims, data in (({}, {'active': True, 'token_version': 7}),
                             ({'token_version': 7}, {'active': True})):
            raw = self.session_token(**claims)
            self.responses.append((200, data))
            self.auth.get_validated_token(raw)
        self.assertEqual(len(self.calls), 2)

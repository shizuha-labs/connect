"""
Revocation enforcement tests for PLAT-1033.

Covers the shared revocation client copied from PLAT-1032 and verifies Connect's
RS256 authentication paths deny revoked OAuth access tokens; HS256 user tokens are rejected before revocation checks.
"""
import base64
import json
import time
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.tokens import AccessToken

from connect.authentication import JWTAuthMiddleware, ShizuhaJWTAuthentication, _Rs256Token
from connect.token_revocation import (
    _CACHE_TTL_SECONDS,
    _REVOCATION_CACHE,
    _cache_key,
    is_token_revoked,
)


def _unsigned_jwt_header(alg="RS256", kid="test-kid"):
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    payload = {"sub": "42"}
    enc = lambda obj: base64.urlsafe_b64encode(
        json.dumps(obj, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{enc(header)}.{enc(payload)}.c2lnbmF0dXJl"


class TokenRevocationClientTests(TestCase):
    def setUp(self):
        _REVOCATION_CACHE.clear()

    @patch("connect.token_revocation.requests.post")
    def test_missing_or_empty_client_id_still_consults_authority(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.json.return_value = {"active": False}
        for claims, token in (({}, "session.jwt"), ({"client_id": ""}, "agent.jwt")):
            self.assertTrue(is_token_revoked(claims, token))
        self.assertEqual(mock_post.call_count, 2)

    @patch("connect.token_revocation.requests.post")
    def test_revoked_token_returns_true(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"active": False}
        mock_post.return_value = mock_resp

        self.assertTrue(is_token_revoked({"client_id": "connect"}, "revoked.jwt"))

    @patch("connect.token_revocation.requests.post")
    def test_active_token_returns_false(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        self.assertFalse(is_token_revoked({"client_id": "connect"}, "active.jwt"))

    @patch("connect.token_revocation.requests.post", side_effect=OSError("connection refused"))
    def test_network_error_fails_closed(self, _mock):
        self.assertTrue(is_token_revoked({"client_id": "connect"}, "some.jwt"))

    @patch("connect.token_revocation.requests.post")
    def test_non_200_response_fails_closed(self, mock_post):
        mock_post.return_value = MagicMock(status_code=503)

        self.assertTrue(is_token_revoked({"client_id": "connect"}, "some.jwt"))

    @patch("connect.token_revocation.requests.post")
    def test_bad_json_response_fails_closed(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("bad json")
        mock_post.return_value = mock_resp

        self.assertTrue(is_token_revoked({"client_id": "connect"}, "some.jwt"))

    @patch("connect.token_revocation.requests.post")
    def test_cache_hit_prevents_duplicate_lookup(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        token = "cached.jwt"
        is_token_revoked({"client_id": "connect"}, token)
        is_token_revoked({"client_id": "connect"}, token)

        mock_post.assert_called_once()

    @patch("connect.token_revocation.requests.post")
    def test_expired_cache_triggers_new_lookup(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        token = "expiry.jwt"
        _REVOCATION_CACHE[_cache_key(token)] = (
            time.monotonic() - _CACHE_TTL_SECONDS - 1,
            False,
        )

        is_token_revoked({"client_id": "connect"}, token)

        mock_post.assert_called_once()


class ConnectAuthenticationRevocationTests(TestCase):
    def setUp(self):
        _REVOCATION_CACHE.clear()

    def test_hs256_user_token_is_rejected(self):
        with self.assertRaises(InvalidToken):
            ShizuhaJWTAuthentication().get_validated_token(str(AccessToken()))

    @patch("connect.authentication.is_token_revoked", return_value=False)
    @patch("connect.authentication._try_rs256_validate")
    def test_rs256_path_enforces_revocation_check(self, mock_rs, mock_revoked):
        token = _Rs256Token({"user_id": "42", "client_id": "connect"})
        mock_rs.return_value = token

        raw = _unsigned_jwt_header("RS256")
        validated = ShizuhaJWTAuthentication().get_validated_token(raw)

        self.assertIs(validated, token)
        mock_revoked.assert_called_once_with(token, raw)

    @patch("connect.authentication.is_token_revoked", return_value=True)
    @patch("connect.authentication._try_rs256_validate")
    def test_rs256_revoked_token_is_rejected(self, mock_rs, _mock_revoked):
        mock_rs.return_value = _Rs256Token({"user_id": "42", "client_id": "connect"})

        with self.assertRaises(InvalidToken):
            ShizuhaJWTAuthentication().get_validated_token(_unsigned_jwt_header("RS256"))

    @patch("connect.authentication.is_token_revoked", return_value=True)
    @patch("connect.authentication._try_rs256_validate")
    def test_websocket_auth_treats_revoked_rs256_token_as_anonymous(self, mock_rs, _mock_revoked):
        middleware = JWTAuthMiddleware(lambda scope, receive, send: None)
        raw = _unsigned_jwt_header("RS256")
        scope = {"query_string": f"token={raw}".encode("ascii"), "headers": []}
        mock_rs.return_value = _Rs256Token({
            "user_id": "42",
            "username": "agent",
            "email": "agent@shizuha.com",
            "client_id": "connect",
        })

        user, validated_token = async_to_sync(middleware._authenticate)(scope)

        self.assertIsInstance(user, AnonymousUser)
        self.assertIsNone(validated_token)

"""Canonical JWKS / RS256 verifier vectors for connect (PLAT-984 / PLAT-675).

Mirrors the canonical suite first shipped on pulse (shizuha-tasks
test_jwks_canonical_verifier.py). Asserts the hardened
``ShizuhaJWTAuthentication.get_validated_token`` contract:

  1. Valid HS256 token rejected          (CON-122 cutover)
  2. ``alg=none`` rejected
  3. Unsupported alg (RS384) rejected     (alg-allowlist)
  4. HS256/RS256 key-confusion rejected   (RS256 payload HS256-signed w/ pubkey)
  5. Valid RS256 token (kid → JWKS) accepted
  6. Fail-closed: kid-less RS256, unknown-kid, and JWKS-down all rejected

Network is never touched — the JWKS cache is seeded directly.

unittest-style (Django ``manage.py test``): the mandatory pre-push suite and
CI run the unittest loader, and pytest is not a runtime/CI dependency
(``uv sync --no-dev``). A pytest-only module errored on import → reddened the
whole connect suite / blocked the local push gate while these security vectors
never actually ran. Ported to ``SimpleTestCase`` so they execute everywhere.
"""
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase

from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.tokens import AccessToken

from connect import authentication as auth

KID = "test-rs256-kid"


def _make_rsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _rs256(priv_pem, *, kid=KID, alg="RS256", exp_in=3600, extra_claims=None):
    headers = {} if kid is None else {"kid": kid}
    payload = {"sub": "1", "user_id": 1, "exp": int(time.time()) + exp_in}
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, priv_pem, algorithm=alg, headers=headers)


class JwksCanonicalVerifierTests(SimpleTestCase):
    """Pure token-verification vectors — no DB, no network."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # RSA keygen is slow; generate once for the whole vector set.
        cls.priv_pem, cls.pub_pem = _make_rsa()

    def setUp(self):
        from connect import token_revocation
        token_revocation._REVOCATION_CACHE.clear()
        self.addCleanup(token_revocation._REVOCATION_CACHE.clear)

    def _seed_jwks(self):
        """Seed the module JWKS cache with the test public key under KID."""
        pubkey = jwt.algorithms.RSAAlgorithm(
            jwt.algorithms.RSAAlgorithm.SHA256
        ).prepare_key(self.pub_pem.decode())
        auth._JWKS_CACHE["keys"] = {KID: pubkey}
        auth._JWKS_CACHE["fetched_at"] = time.time()

        def _reset():
            auth._JWKS_CACHE["keys"] = {}
            auth._JWKS_CACHE["fetched_at"] = 0
        self.addCleanup(_reset)

    # ── Vector 1: HS256 rejected (CON-122 legacy shared-secret cutover) ───────
    def test_valid_hs256_rejected(self):
        raw = str(AccessToken())  # simplejwt mints an HS256 token w/ the configured key
        with self.assertRaises(InvalidToken):
            auth.ShizuhaJWTAuthentication().get_validated_token(raw)

    # ── Vector 2: alg=none rejected ─────────────────────────────────────────────
    def test_alg_none_rejected(self):
        raw = jwt.encode({"sub": "1"}, key=None, algorithm="none")
        with self.assertRaises(InvalidToken):
            auth.ShizuhaJWTAuthentication().get_validated_token(raw)

    # ── Vector 3: unsupported alg (RS384) rejected by allowlist ─────────────────
    def test_unsupported_alg_rejected(self):
        raw = _rs256(self.priv_pem, alg="RS384")
        with self.assertRaises(InvalidToken):
            auth.ShizuhaJWTAuthentication().get_validated_token(raw)

    # ── Vector 4: HS256/RS256 key-confusion rejected ────────────────────────────
    def test_key_confusion_rejected(self):
        """Genuine key-confusion attack: alg=HS256 header, HMAC-signed with the RSA
        *public* key bytes as the secret. Must NOT validate — strict alg routing sends
        it to the HS256 path, which verifies against the shared SIGNING_KEY (not the RSA
        key), so the forged signature fails. (PyJWT blocks the naive jwt.encode form, so
        the token is crafted manually to exercise the real attack.)"""
        self._seed_jwks()
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        body = _b64(json.dumps({"sub": "1", "user_id": 1, "exp": int(time.time()) + 3600}).encode())
        sig = _b64(hmac.new(self.pub_pem, head + b"." + body, hashlib.sha256).digest())
        forged = (head + b"." + body + b"." + sig).decode()
        with self.assertRaises(InvalidToken):
            auth.ShizuhaJWTAuthentication().get_validated_token(forged)

    # ── Vector 5: valid RS256 (kid → JWKS) accepted ─────────────────────────────
    def test_valid_rs256_accepted(self):
        self._seed_jwks()
        raw = _rs256(self.priv_pem)
        with patch.object(auth, "is_token_revoked", return_value=False) as active:
            tok = auth.ShizuhaJWTAuthentication().get_validated_token(raw)
        active.assert_called_once_with(tok, raw)
        self.assertIsNotNone(tok)
        self.assertEqual(tok.get("user_id"), 1)

    # ── Vector 6: fail-closed — kid-less, unknown-kid, JWKS-down ─────────────────
    def test_rs256_missing_kid_rejected(self):
        self._seed_jwks()
        raw = _rs256(self.priv_pem, kid=None)
        with self.assertRaises(InvalidToken):
            auth.ShizuhaJWTAuthentication().get_validated_token(raw)

    def test_rs256_unknown_kid_failclosed(self):
        self._seed_jwks()
        raw = _rs256(self.priv_pem, kid="some-other-kid")
        # force-refresh returns no keys (simulate JWKS that lacks this kid / outage)
        with patch.object(auth, "_jwks_fetch_keys", lambda force_refresh=False: {}):
            with self.assertRaises(InvalidToken):
                auth.ShizuhaJWTAuthentication().get_validated_token(raw)

    # ── Revocation cutoff: OAuth client tokens fail closed after signature verify ─
    def test_rs256_oauth_revocation_cutoff_rejected(self):
        self._seed_jwks()
        raw = _rs256(self.priv_pem, extra_claims={
            "client_id": "agent-client",
            "username": "agent-user",
            "email": "agent@example.com",
            "iat": int(time.time()) - 60,
        })
        with patch.object(auth, "is_token_revoked", lambda claims, token: True):
            with self.assertRaises(InvalidToken):
                auth.ShizuhaJWTAuthentication().get_validated_token(raw)

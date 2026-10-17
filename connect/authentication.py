"""
Custom JWT Authentication for Shizuha Connect

This authentication class validates JWT tokens issued by Shizuha ID
without requiring the user to exist in the local database.

In a microservices architecture with unified login:
- Shizuha ID issues JWT tokens
- Other services (Connect) validate and trust those tokens
- User data is fetched from the token claims, not the local database
"""

import logging
from urllib.parse import parse_qs
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from rest_framework import authentication, exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from connect.token_revocation import is_token_revoked

logger = logging.getLogger(__name__)
User = get_user_model()


def _coerce_connect_user_id(user_id):
    """Return a stable integer key for Connect's legacy user_id columns.

    Shizuha ID human tokens use integer IDs. Some agent tokens have carried a
    UUID subject while still setting ``is_agent=True``; Connect's social graph
    tables are currently keyed by integer ``user_id`` columns, so blindly doing
    ``int(uuid)`` turns every authenticated agent profile/post API into a 500.
    Keep integer IDs unchanged, and map UUID subjects into a deterministic
    positive 31-bit key so agent-facing endpoints fail closed less often while
    the broader schema remains integer-based.
    """
    if user_id is None:
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        pass

    import uuid as _uuid
    import zlib as _zlib

    raw = str(user_id)
    try:
        normalized = str(_uuid.UUID(raw))
    except (TypeError, ValueError):
        raise ValueError(f"Unsupported federated user_id {raw!r}; expected integer or UUID")
    # PostgreSQL integer is signed 32-bit; keep the value positive and non-zero.
    return (_zlib.crc32(normalized.encode('utf-8')) % 2147483646) + 1


class FederatedUser:
    """
    A lightweight user object for federated authentication.
    Used when the user exists in Shizuha ID but not in the local database.
    """

    def __init__(self, user_id, email=None, username=None, first_name=None, last_name=None, date_joined=None, enabled_services=None, is_agent=False):
        from django.utils import timezone
        self.raw_user_id = str(user_id) if user_id is not None else None
        self.id = _coerce_connect_user_id(user_id)
        self.pk = self.id
        self.email = email or f"user_{user_id}@shizuha.id"
        self.username = username or f"user_{user_id}"
        self.first_name = first_name or ""
        self.last_name = last_name or ""
        self.date_joined = date_joined or timezone.now()
        self.is_authenticated = True
        self.is_active = True
        self.is_anonymous = False
        self.is_staff = False
        self.is_superuser = False
        self.is_agent = bool(is_agent)
        # None = unrestricted (staff/superuser), [] = no services, [...] = specific services
        self.enabled_services = enabled_services

    def __str__(self):
        return f"FederatedUser({self.id})"

    def save(self, *args, **kwargs):
        pass  # No-op for federated users

    def get_full_name(self):
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.username

    def get_short_name(self):
        return self.first_name or self.username


# ───────────────────────── OAuth 2.0 advisory + enforcement ─────────────────
# Inlined from packages/shizuha-auth: Phase 2/4 of OAuth migration. Services
# that import the shared package should prefer that copy; this duplicate
# keeps parity while the migration to the shared package is in-flight.

def _oauth_advisory_claim_check(validated_token):
    """Log mismatched `aud`/`iss` claims without rejecting. Returns status dict."""
    import os as _os
    from django.conf import settings as _st
    service_name = getattr(_st, 'SHIZUHA_SERVICE_NAME', '') or _os.environ.get('SHIZUHA_SERVICE_NAME', '')
    expected_issuer = getattr(_st, 'SHIZUHA_EXPECTED_ISSUER', '') or _os.environ.get('SHIZUHA_EXPECTED_ISSUER', '')
    aud = validated_token.get('aud')
    if aud is not None and aud not in ('*', ['*']):
        aud_list = aud if isinstance(aud, list) else [aud]
        if service_name and service_name not in aud_list:
            logger.warning(
                f"JWT aud={aud_list!r} does not include service={service_name!r}. "
                f"Phase 2 advisory — token accepted."
            )
    iss = validated_token.get('iss')
    if iss is not None and expected_issuer and iss != expected_issuer:
        logger.warning(
            f"JWT iss={iss!r} does not match expected={expected_issuer!r}. "
            f"Phase 2 advisory — token accepted."
        )


def _oauth_enforce_audience_claim(validated_token):
    """Phase 4: when SHIZUHA_AUD_ENFORCE=True, reject tokens with mismatched aud."""
    import os as _os
    from django.conf import settings as _st
    from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
    enforce = bool(getattr(_st, 'SHIZUHA_AUD_ENFORCE', False)) or \
        _os.environ.get('SHIZUHA_AUD_ENFORCE', '').lower() in ('1', 'true', 'yes')
    if not enforce:
        return
    service_name = getattr(_st, 'SHIZUHA_SERVICE_NAME', '') or _os.environ.get('SHIZUHA_SERVICE_NAME', '')
    if not service_name:
        return
    aud = validated_token.get('aud')
    if aud is None or aud == '*' or aud == ['*']:
        return
    aud_list = aud if isinstance(aud, list) else [aud]
    if service_name not in aud_list:
        logger.warning(
            f"JWT rejected: aud={aud_list!r} does not include required service={service_name!r}"
        )
        raise _InvalidToken(f"Token audience {aud_list} does not include '{service_name}'")



def _raw_token_to_str(raw_token):
    if isinstance(raw_token, bytes):
        try:
            return raw_token.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise InvalidToken('Token is not valid UTF-8') from exc
    return str(raw_token)


def _oauth_enforce_token_not_revoked(validated_token, raw_token):
    """Reject every ID token revoked directly or by an older token version.

    Session and agent tokens are checked even without a client_id. Lookup
    errors deny access without caching, after local signature/expiry validation.
    """
    if is_token_revoked(validated_token, _raw_token_to_str(raw_token)):
        raise InvalidToken('Token has been revoked')

# ───────────────────────── OAuth 2.0 RS256 validation ──────────────────────
# Connect user/browser tokens must be Shizuha ID RS256/JWKS tokens. Legacy
# shared-secret HS256 validation was removed in CON-122.

class _Rs256Token(dict):
    """Lightweight dict-like token — satisfies simplejwt get_user() interface."""
    pass


_JWKS_CACHE = {'keys': {}, 'fetched_at': 0}
_JWKS_TTL = 600  # seconds
_ALLOWED_JWT_ALGS = {'RS256'}


def _jwks_fetch_keys(force_refresh=False):
    """Fetch/cache shizuha-id JWKS keys by kid (canonical, PLAT-675 — honor kid, fail closed).

    A forced refresh (used on kid miss/rotation) returns ONLY the just-fetched key set,
    never stale keys, so an unknown kid or JWKS outage cannot validate against an old key.
    """
    import json as _json, os as _os, time as _time, urllib.request as _urlreq
    import jwt as _jwt
    if (not force_refresh and _time.time() - _JWKS_CACHE['fetched_at'] < _JWKS_TTL
            and _JWKS_CACHE['keys']):
        return _JWKS_CACHE['keys']
    issuer = (_os.environ.get('SHIZUHA_EXPECTED_ISSUER') or
              'http://shizuha-id:8001').rstrip('/')
    jwks_url = _os.environ.get('SHIZUHA_OAUTH_JWKS_URL') or f'{issuer}/.well-known/jwks.json'
    try:
        resp = _urlreq.urlopen(jwks_url, timeout=5)
        jwks = _json.loads(resp.read())
        keys = {
            k['kid']: _jwt.algorithms.RSAAlgorithm.from_jwk(_json.dumps(k))
            for k in jwks.get('keys', [])
            if k.get('kid') and k.get('kty') == 'RSA' and k.get('alg') in (None, 'RS256')
        }
        _JWKS_CACHE['keys'] = keys
        _JWKS_CACHE['fetched_at'] = _time.time()
        return keys
    except Exception as _exc:
        logger.warning(f'JWKS fetch from {jwks_url} failed: {_exc}')
        return {} if force_refresh else _JWKS_CACHE['keys']


def _try_rs256_validate(raw_token):
    """Return a _Rs256Token on success, None if not RS256, raise if bad RS256.

    Canonical (PLAT-675/PLAT-555): alg-allowlist, REQUIRE kid, fail closed on a
    missing/unknown key — never fall back to an arbitrary JWKS key for a kid-less token.
    """
    import jwt as _jwt
    from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
    try:
        header = _jwt.get_unverified_header(raw_token)
    except Exception:
        return None
    alg = header.get('alg')
    if alg not in _ALLOWED_JWT_ALGS:
        raise _InvalidToken(f'Unsupported JWT alg={alg!r}')
    if alg != 'RS256':
        return None
    kid = header.get('kid')
    if not kid:
        raise _InvalidToken('RS256 token missing kid')
    keys = _jwks_fetch_keys()
    key = keys.get(kid)
    if key is None:
        # Re-fetch once in case of key rotation; fail closed on miss/outage.
        keys = _jwks_fetch_keys(force_refresh=True)
        key = keys.get(kid)
    if key is None:
        raise _InvalidToken(f'No JWKS key available for kid={kid!r}')
    try:
        payload = _jwt.decode(
            raw_token, key=key, algorithms=['RS256'],
            options={'verify_aud': False, 'verify_exp': True},
        )
    except _jwt.PyJWTError as exc:
        raise _InvalidToken(f'RS256 validation failed: {exc}')
    return _Rs256Token(payload)

class ShizuhaJWTAuthentication(JWTAuthentication):
    """
    Custom JWT authentication that works with Shizuha ID tokens.

    If the user doesn't exist locally, creates a FederatedUser object
    from the token claims instead of failing with 401.
    """


    def get_validated_token(self, raw_token):
        """Canonical Connect verifier: Shizuha ID RS256/JWKS only.

        CON-122 removes shared-secret HS256 user-token acceptance. Route strictly
        by the declared algorithm and fail closed unless the token is RS256 with
        a known JWKS kid.
        """
        import jwt as _jwt
        from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
        try:
            header = _jwt.get_unverified_header(raw_token)
        except Exception:
            raise _InvalidToken('Malformed JWT header')
        alg = header.get('alg')
        if alg not in _ALLOWED_JWT_ALGS:
            raise _InvalidToken(f'Unsupported JWT alg {alg!r}: only RS256 accepted')
        tok = _try_rs256_validate(raw_token)
        if tok is None:
            raise _InvalidToken('RS256 token failed JWKS validation')
        _oauth_enforce_token_not_revoked(tok, raw_token)
        return tok

    def get_user(self, validated_token):
        """
        Get the user from the validated token.
        If user doesn't exist locally, return a FederatedUser.

        Handles both integer user IDs (from Shizuha ID) and UUID user IDs (from agents).
        """
        _oauth_advisory_claim_check(validated_token)
        _oauth_enforce_audience_claim(validated_token)
        try:
            user_id = validated_token.get('user_id')
            if user_id is None:
                raise InvalidToken('Token contained no recognizable user identification')

            # Extract additional claims for FederatedUser
            email = validated_token.get('email')
            username = validated_token.get('username')
            first_name = validated_token.get('first_name')
            last_name = validated_token.get('last_name')

            # Extract enabled_services from token
            enabled_services = validated_token.get('enabled_services')
            if validated_token.get('is_staff') or validated_token.get('is_superuser'):
                enabled_services = None

            # Check if this is an agent token (agents have UUID user_id and is_agent=True)
            is_agent = validated_token.get('is_agent', False)
            if is_agent:
                logger.debug(f"Agent token detected for {email}, using federated user")
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    enabled_services=enabled_services,
                    is_agent=True,
                )

            # CRITICAL: shizuha-id user_ids collide with local PKs. Trust
            # token claims when `email` is present; only fall back to local
            # DB lookup for legacy tokens without email.
            if email:
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    enabled_services=enabled_services,
                )

            try:
                int_user_id = int(user_id)
                user = User.objects.get(pk=int_user_id)
                return user
            except (User.DoesNotExist, ValueError, TypeError):
                logger.debug(
                    f"User {user_id} not found locally and token has no "
                    f"email claim; returning placeholder FederatedUser"
                )
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    enabled_services=enabled_services,
                )

        except KeyError:
            raise InvalidToken('Token contained no recognizable user identification')


class ShizuhaIDAuthentication(authentication.BaseAuthentication):
    """
    Alternative authentication that validates tokens against Shizuha ID service.

    This can be used to verify tokens by calling the Shizuha ID API directly,
    which is useful for:
    - Getting fresh user data
    - Validating token hasn't been revoked
    - Supporting token refresh flows
    """

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if not auth_header.startswith('Bearer '):
            return None

        token = auth_header.split(' ')[1]

        try:
            authenticator = ShizuhaJWTAuthentication()
            validated_token = authenticator.get_validated_token(token)
            return (authenticator.get_user(validated_token), validated_token)
        except (InvalidToken, TokenError, exceptions.AuthenticationFailed) as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {str(e)}')

    def authenticate_header(self, request):
        return 'Bearer realm="api"'


# ── WebSocket JWT Middleware ─────────────────────────────────────────────────


class JWTAuthMiddleware(BaseMiddleware):
    """
    Channels middleware that authenticates WebSocket connections using a JWT
    token passed as a query parameter (?token=xxx).

    Django's built-in AuthMiddlewareStack only checks HTTP headers/cookies,
    which aren't available for WebSocket upgrades from mobile clients.
    """

    async def __call__(self, scope, receive, send):
        user, token = await self._authenticate(scope)
        scope['user'] = user
        # Stash the validated token so consumers can read agent-identity claims
        # (is_agent, agent_id, organization_id, …) without re-parsing. None for
        # anonymous/unauthenticated connections.
        scope['token'] = token
        return await super().__call__(scope, receive, send)

    @database_sync_to_async
    def _authenticate(self, scope):
        # Extract token from query string (?token=xxx)
        query_string = scope.get('query_string', b'').decode()
        query_params = parse_qs(query_string)
        token_list = query_params.get('token', [])

        if not token_list:
            # Fallback: check headers (e.g. from web clients)
            for header_name, header_value in scope.get('headers', []):
                if header_name == b'authorization':
                    parts = header_value.decode().split()
                    if len(parts) == 2 and parts[0].lower() == 'bearer':
                        token_list = [parts[1]]
                    break

        if not token_list:
            logger.debug("WS auth: no token found in query params or headers")
            return AnonymousUser(), None

        raw_token = token_list[0]

        try:
            # Keep WebSocket auth in parity with DRF auth. The HTTP API already
            # accepts shizuha-id RS256 tokens via ShizuhaJWTAuthentication, but
            # the WS path used AccessToken directly, which only validates the
            # legacy HS256/simplejwt shape. Agent runtimes now self-auth with
            # RS256 shizuha-id JWTs, so direct AccessToken validation rejected
            # every Connect WS upgrade as anonymous -> 403.
            authenticator = ShizuhaJWTAuthentication()
            validated_token = authenticator.get_validated_token(raw_token)
            user = authenticator.get_user(validated_token)
            return user, validated_token

        except (InvalidToken, TokenError, exceptions.AuthenticationFailed) as e:
            logger.warning("WS auth: invalid token — %s", e)
            return AnonymousUser(), None

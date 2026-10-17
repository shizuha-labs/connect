"""
CON-129: shared-organization tenancy gate for Connect messaging.

INCIDENT 2026-07-02: fleet agents (orgs {1,17,76}) DMd an external user who
owns only org 67 — Connect messaging enforced Block-policy only, with no
organization-membership check anywhere in the DM send / conversation create /
participant add / agent-mention paths. Any authenticated principal could
message any registered user.

This module provides the server-side gate: two principals may exchange
messages only when they share at least one ACTIVE organization membership.
Source of truth is the admin service's OrganizationMembership
(`GET {SHIZUHA_ADMIN_API_URL}/internal/users/<id>/memberships/`, the same
projection shizuha-id stamps into JWTs as `organization_memberships`).
The legacy single `organization_id` field is deliberately NOT consulted.

Authorization uses the live/cached Admin projection for BOTH participants.
The JWT projection is identity context, not current authorization authority:
an org grant or revocation after token issuance would otherwise be invisible
until that access token expired.  This matters for fleet provisioning, where
Hive creates the Admin membership after the agent's first token was minted.

Failure semantics are fail-closed but distinguishable: an established
cross-org pair is `cross_org` (HTTP 403 cross_org_not_allowed); a pair whose
membership cannot be established (admin down AND no cached value) is
`unknown` (HTTP 503 org_lookup_unavailable, retryable). Successful lookups
are cached briefly; the last-known-good value is kept longer and served
stale when the live lookup fails, so a short admin-service blip does not
take messaging down.

CON-149: the gate governs tenant-user↔tenant-user edges only. Hive
personal-agent pseudo-users (reserved id band, see
`hive_agents.is_hive_agent_user_id`) are Connect-local bridge endpoints with
no org membership; an edge with an agent on either side is exempted here,
because cross-owner isolation for agent edges is enforced upstream by Hive's
owner-scoped resolution rather than by organization membership.
"""
import logging
import os

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Gate verdicts.
ALLOWED = 'allowed'
CROSS_ORG = 'cross_org'
UNKNOWN = 'unknown'

_FRESH_KEY = 'con129:orgids:{user_id}'
_STALE_KEY = 'con129:orgids:stale:{user_id}'
_PERSONAL_ORG_KEY = 'con613:personal-org:{user_id}'


def _admin_base() -> str:
    return (
        getattr(settings, 'SHIZUHA_ADMIN_API_URL', None)
        or os.environ.get('SHIZUHA_ADMIN_API_URL')
        or 'http://shizuha-admin:8003/api'
    ).rstrip('/')


def _enforcement_enabled() -> bool:
    """Operational kill-switch (default ON). Only for emergency rollback."""
    return bool(getattr(settings, 'CONNECT_ENFORCE_ORG_TENANCY', True))


def _fresh_ttl() -> int:
    return int(getattr(settings, 'CONNECT_ORG_CACHE_TTL_SECONDS', 300) or 300)


def _stale_ttl() -> int:
    return int(getattr(settings, 'CONNECT_ORG_CACHE_STALE_TTL_SECONDS', 3600) or 3600)


def get_user_org_ids(user_id) -> frozenset | None:
    """Return the user's ACTIVE org-id set, or None when it cannot be established.

    Reads through a two-tier cache: a short fresh tier, and a longer
    last-known-good tier served only when the live admin lookup fails.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None

    fresh_key = _FRESH_KEY.format(user_id=user_id)
    cached = cache.get(fresh_key)
    if cached is not None:
        return frozenset(cached)

    stale_key = _STALE_KEY.format(user_id=user_id)
    try:
        resp = requests.get(
            f'{_admin_base()}/internal/users/{user_id}/memberships/',
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=4,
        )
        if resp.status_code == 200:
            memberships = resp.json().get('memberships') or {}
            org_ids = sorted(int(k) for k in memberships.keys())
            cache.set(fresh_key, org_ids, _fresh_ttl())
            cache.set(stale_key, org_ids, _stale_ttl())
            return frozenset(org_ids)
        logger.warning(
            'CON-129 org membership lookup for user %s returned %s',
            user_id, resp.status_code,
        )
    except (requests.RequestException, ValueError) as exc:
        logger.warning('CON-129 org membership lookup failed for user %s: %s', user_id, exc)

    stale = cache.get(stale_key)
    if stale is not None:
        logger.info('CON-129 serving stale org set for user %s', user_id)
        return frozenset(stale)
    return None


def get_user_personal_org_id(user_id) -> int | None:
    """Return Admin's canonical personal organization for a user.

    Hive personal Claws are owned by this organization.  Delivery signs this
    single authority-derived id rather than an arbitrary/non-empty membership
    list, allowing Hive to independently enforce the exact same binding.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    key = _PERSONAL_ORG_KEY.format(user_id=user_id)
    cached = cache.get(key)
    if cached is not None:
        return int(cached) if cached else None
    try:
        response = requests.get(
            f'{_admin_base()}/internal/users/{user_id}/organizations/',
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=4,
        )
        if response.status_code == 200:
            candidates = []
            for organization in (response.json() or {}).get('organizations', []):
                if (organization.get('is_personal') is True and
                        str(organization.get('owner_id')) == str(user_id)):
                    candidates.append(int(organization['id']))
            if len(candidates) == 1:
                cache.set(key, candidates[0], _fresh_ttl())
                return candidates[0]
            logger.warning(
                'CON-613 canonical personal organization ambiguous for user %s: '
                'candidates=%s', user_id, len(candidates),
            )
    except (requests.RequestException, TypeError, ValueError, KeyError) as exc:
        logger.warning(
            'CON-613 personal organization lookup failed for user %s: %s',
            user_id, exc,
        )
    return None


def org_ids_from_token(token) -> frozenset | None:
    """Extract the caller's org set from a validated JWT's
    `organization_memberships` claim (the multi-org projection shizuha-id
    mints from admin). Returns None when the claim is absent/malformed —
    callers then fall back to the admin lookup. The legacy single
    `organization_id` claim is intentionally ignored (CON-129 requirement).
    """
    if token is None:
        return None
    try:
        memberships = token.get('organization_memberships')
    except Exception:
        return None
    if not isinstance(memberships, dict) or not memberships:
        return None
    try:
        return frozenset(int(k) for k in memberships.keys())
    except (TypeError, ValueError):
        return None


def exempt_sender_ids() -> frozenset:
    """Sender ids exempt from the gate (CONNECT_ORG_TENANCY_EXEMPT_SENDER_IDS,
    comma-separated). Default EMPTY — strict for everyone. Exists for the
    platform `system` notification account, whose alerts legitimately reach
    users in every org; production value requires security sign-off (CON-129
    sweep found system/coordinator accounts with zero memberships that would
    otherwise lose messaging entirely).
    """
    raw = getattr(settings, 'CONNECT_ORG_TENANCY_EXEMPT_SENDER_IDS', '') or ''
    ids = set()
    for part in str(raw).split(','):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def exempt_recipient_ids() -> frozenset:
    """Recipient ids allowed to receive cross-org messages.

    Default EMPTY. This is intentionally narrower than sender exemption and is
    meant for sink/service principals such as the platform `system` account:
    agents are required by the bridge contract to reply to `[system]` DMs, but
    `system` has no org memberships and is not a tenant user whose inbox grants
    access to another customer's data.
    """
    raw = getattr(settings, 'CONNECT_ORG_TENANCY_EXEMPT_RECIPIENT_IDS', '') or ''
    ids = set()
    for part in str(raw).split(','):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def check_messaging_allowed(sender_id, recipient_id, *, sender_token=None) -> str:
    """Gate one sender→recipient messaging edge.

    Returns ALLOWED / CROSS_ORG / UNKNOWN. Self-edges are always allowed
    (self-send is rejected elsewhere for other reasons). Both sets come from
    Admin's current membership projection (through the bounded two-tier
    cache). ``sender_token`` remains accepted for call-site compatibility but
    its embedded membership snapshot is deliberately not authorization
    authority: token claims can be stale after a grant or revocation.
    """
    if not _enforcement_enabled():
        return ALLOWED
    try:
        if int(sender_id) == int(recipient_id):
            return ALLOWED
        if int(sender_id) in exempt_sender_ids():
            return ALLOWED
        if int(recipient_id) in exempt_recipient_ids():
            return ALLOWED
    except (TypeError, ValueError):
        pass

    # CON-149: Hive personal-agent pseudo-users are NOT tenant users — they are
    # Connect-local bridge endpoints (reserved id band) with no organization
    # membership. The shared-org user-tenancy check does not apply to an edge
    # where either side is such an agent: an owner messaging their own agent (and
    # the agent's reply) is intra-tenant by construction, and cross-owner
    # isolation for agent edges is enforced UPSTREAM by Hive's owner-scoped
    # resolution (an owner can only ever resolve/address their own agents), not by
    # org membership. Without this, owner↔own-agent DMs 403 cross_org_not_allowed
    # because the agent's org set is empty (regression of the CON-129 gate).
    # An import failure falls through to the strict org check below (fail-closed).
    try:
        from connect.services.hive_agents import is_hive_agent_user_id
        if is_hive_agent_user_id(sender_id) or is_hive_agent_user_id(recipient_id):
            return ALLOWED
    except Exception:  # pragma: no cover - defensive; never fail-open on import
        pass

    sender_orgs = get_user_org_ids(sender_id)
    recipient_orgs = get_user_org_ids(recipient_id)

    if sender_orgs is None or recipient_orgs is None:
        return UNKNOWN
    if sender_orgs & recipient_orgs:
        return ALLOWED
    return CROSS_ORG


def gate_response(verdict, *, sender_id=None, recipient_id=None):
    """Map a non-ALLOWED verdict to the DRF Response the view should return.

    Returns None for ALLOWED so views can write:
        blocked = gate_response(check_messaging_allowed(...), ...)
        if blocked is not None:
            return blocked
    """
    if verdict == ALLOWED:
        return None
    from rest_framework import status
    from rest_framework.response import Response

    if verdict == CROSS_ORG:
        logger.warning(
            'CON-129 cross-org messaging blocked: sender=%s recipient=%s',
            sender_id, recipient_id,
        )
        return Response(
            {'error': 'cross_org_not_allowed',
             'detail': 'Sender and recipient do not share an active organization.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    logger.error(
        'CON-129 org membership unavailable (fail-closed): sender=%s recipient=%s',
        sender_id, recipient_id,
    )
    return Response(
        {'error': 'org_lookup_unavailable',
         'detail': 'Organization membership could not be verified; retry shortly.'},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )

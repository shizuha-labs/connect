"""Hive personal-agent discovery and routing helpers for Connect."""

import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction

from connect.models import Conversation, ConversationParticipant, DirectMessage

logger = logging.getLogger(__name__)

HIVE_AGENT_EMAIL_DOMAIN = 'hive-agent.local'

# Reserved pseudo-user id band for Hive personal-agent principals. Deterministic
# ids are minted into this high band so they never collide with real shizuha-id
# users (whose ids are far smaller). CON-149: the org-tenancy gate uses
# is_hive_agent_user_id() to recognise these bridge endpoints — they are not
# tenant users and carry no organization membership; their cross-owner isolation
# is enforced upstream by Hive's owner-scoped resolution, not by the org gate.
HIVE_AGENT_USER_ID_BASE = 1_500_000_000
HIVE_AGENT_USER_ID_SPAN = 500_000_000


def _agents_url():
    return getattr(
        settings,
        'HIVE_CONNECT_AGENTS_URL',
        'http://shizuha-hive.shizuha.svc.cluster.local:8000/api/v1/connect/agents/',
    )


def _delivery_url():
    return getattr(
        settings,
        'HIVE_CONNECT_DELIVERY_URL',
        'http://hive-connect-delivery.shizuha-hive.svc.cluster.local:8031/api/v1/internal/connect/messages/',
    )


RUNNING_HIVE_AGENT_STATUSES = {'running', 'ready', 'online', 'healthy'}


def hive_agent_status(agent):
    """Normalize the status field returned by Hive's owner-scoped discovery."""
    return str((agent or {}).get('status') or '').strip().lower()


def hive_agent_is_running(agent):
    """True when Hive discovery says the runtime can accept a delivery."""
    status = hive_agent_status(agent)
    return not status or status in RUNNING_HIVE_AGENT_STATUSES


def hive_agent_delivery_error_text(stable_id, *, status=None, detail=None):
    """User-visible delivery failure copy for Hive-agent bridge failures."""
    stable_id = str(stable_id or 'this Hive agent')
    normalized_status = str(status or '').strip().lower()
    detail = str(detail or '').strip()
    if normalized_status and normalized_status not in RUNNING_HIVE_AGENT_STATUSES:
        return (
            f"Delivery failed: Hive agent {stable_id} is currently {normalized_status}. "
            "Start it in Hive and try again."
        )
    if 'stop' in detail.lower():
        return (
            f"Delivery failed: Hive agent {stable_id} is currently stopped. "
            "Start it in Hive and try again."
        )
    return (
        f"Delivery failed: Hive agent {stable_id} is currently unavailable. "
        "Check Hive runtime status and try again."
    )


def record_hive_agent_delivery_error(conversation, message, agent_participant, *, status=None, detail=None):
    """Persist one supportable system message when Hive delivery cannot run.

    The human message is already committed before dispatch runs. Without this row,
    users see their message sit forever with no assistant/error response (CON-172).
    Reply-to idempotency keeps retrying the same dispatch path from duplicating the
    visible failure.
    """
    stable_id = hive_agent_stable_id_from_participant(agent_participant)
    content = hive_agent_delivery_error_text(stable_id, status=status, detail=detail)
    existing = DirectMessage.objects.filter(
        conversation=conversation,
        sender_id=0,
        message_type='system',
        reply_to=message,
        content=content,
    ).first()
    if existing is not None:
        return existing
    return DirectMessage.objects.create(
        conversation=conversation,
        sender_id=0,
        sender_email='system',
        sender_name='System',
        message_type='system',
        content=content,
        reply_to=message,
    )


def _record_local_hive_delivery_failure(
    conversation, message, agent_participant, *, reason, detail,
):
    """Persist and log a bounded pre-dispatch failure.

    Connect commits the human message before this callback runs.  A local
    authority/configuration failure therefore needs the same durable,
    reply-linked terminal outcome as a downstream HTTP failure; logging alone
    leaves the already-persisted message stuck forever.
    """
    logger.error(
        'HIVE-613 Hive delivery rejected locally reason=%s conversation=%s '
        'message=%s sender=%s agent=%s',
        reason,
        getattr(conversation, 'id', None),
        getattr(message, 'id', None),
        getattr(message, 'sender_id', None),
        getattr(agent_participant, 'user_id', None),
    )
    return record_hive_agent_delivery_error(
        conversation,
        message,
        agent_participant,
        detail=detail,
    )


def auth_headers_from_request(request):
    auth = request.META.get('HTTP_AUTHORIZATION') or ''
    return {'Authorization': auth} if auth else {}


def list_hive_agents(request):
    """Return Hive agents visible to this caller, relying on Hive for owner scope."""
    try:
        response = requests.get(_agents_url(), headers=auth_headers_from_request(request), timeout=4)
        response.raise_for_status()
        payload = response.json() or {}
    except Exception as exc:
        logger.warning('CON-118 Hive agent discovery failed: %s', exc)
        return []
    agents = payload.get('results') if isinstance(payload, dict) else payload
    if not isinstance(agents, list):
        return []
    return [agent for agent in agents if isinstance(agent, dict) and agent.get('stable_id')]


def hive_agent_user_id(stable_id):
    """Map a Hive stable id to a deterministic pseudo user_id in the reserved band."""
    digest = hashlib.sha256(str(stable_id).encode('utf-8')).hexdigest()
    return HIVE_AGENT_USER_ID_BASE + (int(digest[:8], 16) % HIVE_AGENT_USER_ID_SPAN)


def is_hive_agent_user_id(user_id) -> bool:
    """True when user_id falls in the reserved Hive personal-agent pseudo-user band.

    Every id minted by hive_agent_user_id() lands here; real shizuha-id users
    (including fleet-agent accounts) never do. Used by the CON-129 org-tenancy
    gate (CON-149) to recognise Hive-agent bridge endpoints.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return HIVE_AGENT_USER_ID_BASE <= uid < HIVE_AGENT_USER_ID_BASE + HIVE_AGENT_USER_ID_SPAN


def hive_agent_email(agent):
    return f"{agent.get('connect_username') or quote(agent['stable_id'], safe='')}@{HIVE_AGENT_EMAIL_DOMAIN}"


def find_hive_agent(request, stable_id):
    stable_id = str(stable_id or '')
    for agent in list_hive_agents(request):
        if str(agent.get('stable_id') or agent.get('id')) == stable_id:
            return agent
    return None


def _lookup_tokens(agent):
    """Return owner-scoped names that may be used to address one Hive agent in Connect."""
    tokens = set()
    for field in (
        'stable_id', 'id', 'connect_username', 'display_name', 'agent_type', 'runtime_kind'
    ):
        value = str(agent.get(field) or '').strip().lower()
        if value:
            tokens.add(value)
            tokens.add(re.sub(r'[^a-z0-9]+', '-', value).strip('-'))
            tokens.add(re.sub(r'[^a-z0-9]+', '', value))
            if value.startswith('hive:'):
                parts = [part for part in value.split(':') if part]
                if len(parts) >= 2:
                    tokens.add(parts[1])
    email = str(agent.get('email') or '').strip().lower()
    if email:
        tokens.add(email)
        tokens.add(email.split('@', 1)[0])
    connect_username = str(agent.get('connect_username') or '').strip().lower()
    if connect_username and connect_username.endswith('@' + HIVE_AGENT_EMAIL_DOMAIN):
        tokens.add(connect_username.split('@', 1)[0])
    return {token for token in tokens if token}


def find_hive_agent_by_lookup(request, lookup):
    """Find a visible Hive agent by stable id, connect username, or ergonomic alias.

    Hive remains the owner-scope authority; this helper only searches the list
    already visible to the caller so another user's personal Hermes/Claw is not
    exposed by a global Connect username.
    """
    raw_lookup = str(lookup or '').strip().lower()
    if not raw_lookup:
        return None
    lookup_tokens = {
        raw_lookup,
        raw_lookup.split('@', 1)[0],
        re.sub(r'[^a-z0-9]+', '-', raw_lookup).strip('-'),
        re.sub(r'[^a-z0-9]+', '', raw_lookup),
    }
    lookup_tokens = {token for token in lookup_tokens if token}
    agents = list_hive_agents(request)
    matches = [agent for agent in agents if _lookup_tokens(agent) & lookup_tokens]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning('CON-120 ambiguous Hive agent lookup lookup=%s matches=%s', raw_lookup, len(matches))
    return None


def get_or_create_hive_agent_conversation(request, agent):
    """Create/open the direct conversation between the caller and one Hive agent."""
    caller_id = int(request.user.id)
    stable_id = str(agent['stable_id'])
    agent_user_id = hive_agent_user_id(stable_id)
    direct_key = Conversation.build_direct_key(caller_id, agent_user_id)
    display_name = agent.get('display_name') or agent.get('connect_username') or stable_id
    role = agent.get('runtime_kind') or agent.get('agent_type') or 'hive-agent'

    with transaction.atomic():
        conversation, created = Conversation.objects.get_or_create(
            conversation_type='direct',
            direct_key=direct_key,
            defaults={'created_by_id': caller_id},
        )
        if created:
            ConversationParticipant.objects.create(
                conversation=conversation,
                user_id=caller_id,
                user_email=getattr(request.user, 'email', '') or '',
                user_name=(
                    getattr(request.user, 'get_full_name', lambda: '')()
                    or getattr(request.user, 'username', '')
                    or getattr(request.user, 'email', '')
                ),
                is_admin=True,
                participant_type='human',
            )
            ConversationParticipant.objects.create(
                conversation=conversation,
                user_id=agent_user_id,
                user_email=hive_agent_email(agent),
                user_name=f'{display_name} ({role})',
                participant_type='agent',
                agent_id=agent.get('hive_claw_id') or None,
                agent_role=role,
                nickname=stable_id[:50],
            )
        else:
            ConversationParticipant.objects.filter(
                conversation=conversation,
                user_id=agent_user_id,
            ).update(
                user_email=hive_agent_email(agent),
                user_name=f'{display_name} ({role})',
                participant_type='agent',
                agent_role=role,
                nickname=stable_id[:50],
                has_left=False,
                left_at=None,
            )
    return conversation, created


def hive_agent_stable_id_from_participant(participant):
    """Return the Hive agent's stable_id stored on its conversation participant.

    The agent participant is created (ensure_hive_agent_conversation /
    get_or_create_hive_agent_conversation) with ``nickname = stable_id[:50]`` — the
    RAW stable_id, no prefix. CON-151: the old code required a ``hive:`` prefix that
    the creation path never writes (and cannot, without overflowing nickname's
    50-char limit and truncating the id), so this ALWAYS returned '' →
    ``dispatch_hive_agent_message`` silently no-op'd and NO owner→claw DM was ever
    delivered (no CON-118 log, no assistant reply). Return the stored stable_id for
    agent-type participants.
    """
    if getattr(participant, 'participant_type', '') != 'agent':
        return ''
    return (getattr(participant, 'nickname', '') or '').strip()


def _hmac_sign_payload(payload: dict) -> dict:
    """Sign a payload dict with HMAC-SHA256 and return headers.

    Returns a dict of ``X-Hive-Signature-256`` and ``X-Hive-Timestamp`` headers
    to attach to the request. If no secret is configured, returns empty headers
    (the Hive side will reject the request, which is the correct fail-closed
    behaviour).
    """
    secret = getattr(settings, 'HIVE_CONNECT_HMAC_SECRET', '')
    if not secret:
        return {}

    timestamp = str(int(time.time()))
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    signature = hmac.new(
        secret.encode('utf-8'),
        f'{timestamp}||{body}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return {
        'X-Hive-Signature-256': signature,
        'X-Hive-Timestamp': timestamp,
    }


def _authoritative_delivery_context(conversation, message, agent_participant):
    """Return owner/org assertions from persisted Connect state, or ``None``.

    No caller-supplied agent metadata is trusted here.  The sender and target
    must both be active participants in the persisted direct conversation, and
    the organization scope is resolved from Admin's active memberships.  This
    also keeps retry dispatches (which do not carry the discovery response)
    identical to first delivery.
    """
    if getattr(conversation, 'conversation_type', None) != 'direct':
        return None
    owner_id = getattr(message, 'sender_id', None)
    agent_user_id = getattr(agent_participant, 'user_id', None)
    if owner_id is None or agent_user_id is None:
        return None
    stable_id = hive_agent_stable_id_from_participant(agent_participant)
    if not stable_id or agent_user_id != hive_agent_user_id(stable_id):
        return None
    try:
        expected_direct_key = Conversation.build_direct_key(owner_id, agent_user_id)
    except (TypeError, ValueError):
        return None
    if getattr(conversation, 'direct_key', None) != expected_direct_key:
        return None
    participants = ConversationParticipant.objects.filter(
        conversation=conversation,
        has_left=False,
    )
    if not participants.filter(
        user_id=owner_id, participant_type='human',
    ).exists():
        return None
    if not participants.filter(
        user_id=agent_user_id, participant_type='agent',
    ).exists():
        return None

    from connect.services.org_tenancy import get_user_personal_org_id
    organization_id = get_user_personal_org_id(owner_id)
    if organization_id is None:
        return None
    return {
        'owner_id': str(owner_id),
        'organization_id': int(organization_id),
        'agent_participant_id': str(agent_user_id),
    }


def dispatch_hive_agent_message(conversation, message, agent_participant, agent=None):
    """Best-effort handoff to the Hive runtime bridge; bridge owns assistant reply idempotency.

    HIVE-613: Signs the request with HMAC-SHA256 using the shared secret. Owner,
    organization and participant assertions are derived from persisted Connect
    state; caller/discovery metadata is never copied into the signed contract.
    """
    stable_id = hive_agent_stable_id_from_participant(agent_participant)
    if not stable_id:
        # CON-151 / PLAT-1352: fail LOUD — a missing stable_id means the message is
        # silently never delivered to the Hive runtime (the invisible dead-drop this
        # bug was). Surface it so any regression is caught immediately.
        _record_local_hive_delivery_failure(
            conversation,
            message,
            agent_participant,
            reason='missing_stable_id',
            detail='Hive agent delivery identity is unavailable',
        )
        return False
    delivery_context = _authoritative_delivery_context(
        conversation, message, agent_participant,
    )
    if delivery_context is None:
        _record_local_hive_delivery_failure(
            conversation,
            message,
            agent_participant,
            reason='authority_unavailable',
            detail='Hive delivery owner and participant authority is unavailable',
        )
        return False

    payload = {
        'stable_id': stable_id,
        'conversation_id': str(conversation.id),
        'message_id': str(message.id),
        'sender_id': message.sender_id,
        'content': message.content,
        'client_message_id': str(getattr(message, 'client_message_id', None) or message.id),
        **delivery_context,
    }
    try:
        headers = {'Content-Type': 'application/json'}
        headers.update(_hmac_sign_payload(payload))
        if 'X-Hive-Signature-256' not in headers:
            _record_local_hive_delivery_failure(
                conversation,
                message,
                agent_participant,
                reason='signing_secret_unavailable',
                detail='Hive delivery signing authority is unavailable',
            )
            return False
        # Send the exact canonical bytes that were signed. ``requests.post(json=)``
        # re-serializes with different whitespace, which would make Hive verify a
        # different body and correctly reject every request.
        body = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        response = requests.post(
            _delivery_url(), data=body, headers=headers, timeout=5,
        )
        body = response.text[:500]
        try:
            response_payload = response.json() or {}
        except Exception:
            response_payload = {}
        delivery_status = str(response_payload.get('status') or '').strip().lower()
        if response.status_code >= 400 or delivery_status == 'agent_not_running':
            logger.warning(
                'CON-118 Hive agent delivery failed stable_id=%s message=%s status=%s body=%s',
                stable_id, message.id, response.status_code, body[:200],
            )
            record_hive_agent_delivery_error(
                conversation,
                message,
                agent_participant,
                status=response_payload.get('claw_status') or response_payload.get('status'),
                detail=body,
            )
            return False
        return True
    except Exception as exc:
        logger.warning('CON-118 Hive agent delivery failed stable_id=%s message=%s: %s', stable_id, message.id, exc)
        record_hive_agent_delivery_error(
            conversation, message, agent_participant, detail=str(exc),
        )
        return False

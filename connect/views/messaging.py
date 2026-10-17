"""
Messaging views for Shizuha Connect.
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Dict
from datetime import timedelta

logger = logging.getLogger(__name__)

# Exact "pong" is the forged always-on voice-prompt example
# (`[hritik] Hi <agent>, please reply with the word: pong`) — not a live ping.
# Ni/Shion 2026-09-15 also DMd the operator "Test message" / "I'm stuck in a
# loop" after the harness asked them to message_user on a no-progress guard.
_ACK_ONLY_RE = re.compile(
    r'^(replied|done|sent|ok|okay|noted|pong( sent)?|ping|ack|acknowledged|copy|'
    r'will do|on it|standing by|thanks|thank you)[.!]?$', re.I,
)
_LIVENESS_RE = re.compile(
    r'(?is)^(?:test message\b|i[\'’]m here\b.{0,80}what would you like me to do'
    r'|please confirm you can see this)'
)
_GENERIC_LOOP_RE = re.compile(r"(?is)^i['’]m stuck in a loop\b")
_TASK_KEY_RE = re.compile(r'\b[A-Z]{2,}-\d+\b')


def is_ack_only_message(content: str) -> bool:
    """Talk-seat leftover, prompt-example token, or liveness/test/loop spam."""
    text = (content or '').strip()
    if not text:
        return True
    if _ACK_ONLY_RE.match(text):
        return True
    if _LIVENESS_RE.search(text):
        return True
    if _GENERIC_LOOP_RE.match(text) and not _TASK_KEY_RE.search(text):
        return True
    return False


def _qa_cannot_dm_production_agent(sender, recipient_participant) -> bool:
    """True when a QA/test identity is about to inject into a production seat."""
    from connect.services.qa_probe import is_production_fleet_agent, is_qa_fixture_identity
    if not is_production_fleet_agent(
        participant_type=getattr(recipient_participant, 'participant_type', ''),
        email=getattr(recipient_participant, 'user_email', '') or '',
        username=getattr(recipient_participant, 'user_name', '') or '',
    ):
        return False
    return is_qa_fixture_identity(
        email=getattr(sender, 'email', '') or '',
        username=getattr(sender, 'username', '') or '',
    )


VOICE_TURN_DEDUP_WINDOW_S = 20
# Sentinel origin_channel — never a real Channels name. Voice wrote the
# speaker's STT on their behalf (no optimistic Home insert). PLAT-965
# delivers any origin_channel != this socket to the caller; without it,
# UserChatConsumer drops HTTP self-echo and Live history shows only her.
VOICE_TURN_ORIGIN_CHANNEL = 'voice-turn'


def _norm_voice_text(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def _near_dup_voice_text(left: str, right: str) -> bool:
    a = _norm_voice_text(left)
    b = _norm_voice_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and (a in b or b in a):
        return True
    aw = {w for w in a.split(' ') if len(w) > 2}
    bw = {w for w in b.split(' ') if len(w) > 2}
    if not aw or not bw:
        return False
    overlap = len(aw & bw)
    return overlap / min(len(aw), len(bw)) >= 0.7


def _recent_voice_dup(conversation, sender_id, text, *, now=None) -> bool:
    last = (
        DirectMessage.objects.filter(conversation=conversation, sender_id=sender_id)
        .order_by('-created_at')
        .first()
    )
    if last is None:
        return False
    stamp = last.created_at
    if timezone.is_naive(stamp):
        stamp = timezone.make_aware(stamp, timezone.get_current_timezone())
    age = ((now or timezone.now()) - stamp).total_seconds()
    if age > VOICE_TURN_DEDUP_WINDOW_S:
        return False
    return _near_dup_voice_text(last.content, text)

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Count
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

import re
import requests as http_requests

def _resolve_user_details(user_id: int) -> Dict[str, str]:
    """Fetch user name/email from shizuha-id for participant creation."""
    from connect.services.display_names import label_from_identity, resolve_identity_users
    from connect.services.profile_sync import ensure_social_profile

    rows = resolve_identity_users([user_id])
    data = rows.get(int(user_id)) or {}
    name = label_from_identity(data, user_id)
    email = (data.get('email') or '') if data else ''
    if name:
        try:
            ensure_social_profile(
                user_id,
                username=data.get('username') or '',
                display_name=name,
                email=email,
                resolve=False,
            )
        except Exception:
            logger.warning("Failed to seed SocialProfile for user %s", user_id, exc_info=True)
        return {'name': name, 'email': email}
    return {'name': f'User {user_id}', 'email': email}


def _resolve_user_id_by_username(username: str):
    """CON-77: resolve a shizuha-id username -> user_id. Returns None if unknown."""
    # CON-480: tolerate the @agents.shizuha.io principal display form.
    username = normalize_username(username)
    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    try:
        resp = http_requests.get(
            f"{id_base}/internal/users/by-username/",
            params={'username': username},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            return data.get('id') or data.get('user_id')
    except Exception as e:
        logger.warning("CON-77 username->id lookup failed for %s: %s", username, e)
    return None


def _resolve_usernames_by_ids(user_ids):
    """CON-77: batch-resolve user_ids -> {id: username} via shizuha-id."""
    ids = [str(i) for i in user_ids if i is not None]
    if not ids:
        return {}
    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    out = {}
    try:
        resp = http_requests.get(
            f"{id_base}/internal/users/",
            params={'ids': ','.join(ids)},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=3,
        )
        if resp.status_code == 200:
            for data in resp.json() or []:
                uid = data.get('id') or data.get('user_id')
                if uid is not None:
                    out[int(uid)] = data.get('username', '')
    except Exception as e:
        logger.warning("CON-77 id->username batch lookup failed: %s", e)
    return out

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage, MessageRead,
    MessageDelivery, MessageDeletion, DirectMessageIdempotency,
    VALID_NOTIFICATION_CLASSES,
)
from connect.services.agent_identity import normalize_username
from connect.serializers.messaging import (
    ConversationSerializer, ConversationCreateSerializer,
    ConversationParticipantSerializer,
    DirectMessageSerializer, DirectMessageCreateSerializer,
    PresenceStatusSerializer,
)
from connect.services.presence import (
    MAX_SESSION_KEY_LEN,
    get_presence_for_users,
    publish_presence_bridge,
    touch_presence,
)
from connect.services.hive_agents import (
    dispatch_hive_agent_message,
    find_hive_agent,
    find_hive_agent_by_lookup,
    get_or_create_hive_agent_conversation,
    hive_agent_email,
    hive_agent_user_id,
    hive_agent_stable_id_from_participant,
    hive_agent_is_running,
    hive_agent_status,
    list_hive_agents,
    record_hive_agent_delivery_error,
)
from connect.services.privacy import (
    active_participant_ids,
    actor_can_reach_recipient,
    blocked_counterparty_ids,
    can_access_conversation,
    conversation_blocked_for_user,
    filter_participants_readable_to,
    visible_conversation_ids_for_user,
)
from connect.services.org_tenancy import (
    ALLOWED as ORG_ALLOWED,
    check_messaging_allowed,
    gate_response as org_gate_response,
)


def _detect_agent_mentions(content, conversation_id):
    """
    Detect @mentions of agent participants in a message.

    Returns list of (agent_uuid, username, display_name, user_id) tuples for
    each mentioned agent that is an active participant in the conversation.
    """
    if not content:
        return []

    # Extract all @mentions from message content
    mention_pattern = re.compile(r'@(\w[\w-]*)', re.IGNORECASE)
    mentioned_names = mention_pattern.findall(content)
    if not mentioned_names:
        return []

    # Get all active agent participants in this conversation
    agent_participants = ConversationParticipant.objects.filter(
        conversation_id=conversation_id,
        participant_type='agent',
        has_left=False,
    ).exclude(agent_id__isnull=True)

    results = []
    mentioned_lower = {name.lower() for name in mentioned_names}

    for participant in agent_participants:
        # Match against first word of user_name (case-insensitive)
        first_name = participant.user_name.split()[0].lower() if participant.user_name else ''
        if first_name and first_name in mentioned_lower:
            results.append((
                str(participant.agent_id),
                first_name,
                participant.user_name,
                participant.user_id,
            ))

    return results


# ── Throttle classes ─────────────────────────────────────────────────────────

def _request_is_agent(request) -> bool:
    """Talk seats and fleet agents must never share the human send bucket."""
    token = getattr(request, 'auth', None)
    if isinstance(token, dict) and token.get('is_agent'):
        return True
    user = getattr(request, 'user', None)
    return bool(getattr(user, 'is_agent', False))


class MessageSendThrottle(UserRateThrottle):
    """Human anti-spam only. Agents and reads are not billed.

    The 20s 'Expected available in …' wait is this 60/min window sliding —
    not a talk-seat setting. CEO Office / any is_agent JWT is exempt.
    """
    rate = '60/min'

    def allow_request(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        if _request_is_agent(request):
            return True
        return super().allow_request(request, view)

class MediaUploadThrottle(UserRateThrottle):
    """Limit media uploads to 20/minute per user."""
    rate = '20/min'

class AckThrottle(UserRateThrottle):
    """Delivery/read acks are high-volume — a reconnect backfill acks every
    received message — so they get their own generous bucket instead of being
    billed against the 60/min send throttle (which would 429 a backfill storm
    and leave ack state partially recorded). CON-10."""
    rate = '600/min'


class PresenceView(APIView):
    """Read/update presence state for the authenticated user's visible users.

    GET /presence/?user_ids=1,2 returns deterministic aggregate presence rows.
    POST /presence/ records an explicit heartbeat/away/degraded status for the
    caller without coupling presence to message fanout.
    """

    permission_classes = [IsAuthenticated]

    def _visible_user_ids(self, request, requested_ids):
        if getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return requested_ids
        requested = set(requested_ids)
        requested.add(request.user.id)
        visible = set(
            ConversationParticipant.objects
            .filter(
                conversation_id__in=ConversationParticipant.objects.filter(
                    user_id=request.user.id,
                    has_left=False,
                ).values('conversation_id'),
                has_left=False,
                user_id__in=requested,
            )
            .values_list('user_id', flat=True)
        )
        visible.add(request.user.id)
        return [uid for uid in requested_ids if uid in visible]

    def get(self, request):
        raw_ids = request.query_params.get('user_ids', '')
        if raw_ids:
            try:
                user_ids = [int(item.strip()) for item in raw_ids.split(',') if item.strip()]
            except ValueError:
                return Response({'error': 'user_ids must be comma-separated integers'}, status=400)
        else:
            user_ids = [request.user.id]
        user_ids = self._visible_user_ids(request, list(dict.fromkeys(user_ids))[:100])
        statuses = get_presence_for_users(user_ids)
        return Response(PresenceStatusSerializer(statuses, many=True).data)

    def post(self, request):
        status_value = request.data.get('status', 'online')
        if status_value not in {'online', 'away', 'degraded'}:
            return Response({'error': 'status must be online, away, or degraded'}, status=400)
        client_id = str(request.data.get('client_id') or f'api:{request.user.id}')
        client_type = str(request.data.get('client_type') or 'unknown')
        client_label = str(request.data.get('client_label') or '')
        session_key = f'api:{request.user.id}:{client_id}'
        if len(client_id) > 200:
            return Response({'error': 'client_id must be at most 200 characters'}, status=400)
        if len(client_label) > 200:
            return Response({'error': 'client_label must be at most 200 characters'}, status=400)
        if len(session_key) > MAX_SESSION_KEY_LEN:
            return Response({
                'error': f'api presence session key must be at most {MAX_SESSION_KEY_LEN} characters including prefix'
            }, status=400)
        # Prefer the validated JWT's organization_id claim: federated users have
        # no organization_id attribute, so getattr(request.user, ...) wrongly
        # defaults every federated caller to org 1 and cross-publishes presence
        # into the wrong tenant. Fall back to the user attribute, then org 1.
        # Computed BEFORE touch_presence so the session persists this org for the
        # context-free stale-cleanup worker to publish to the same channel.
        token = getattr(request, 'auth', None)
        org_id = None
        if token is not None:
            try:
                org_id = token.get('organization_id')
            except Exception:
                org_id = None
        if org_id is None:
            org_id = getattr(request.user, 'organization_id', None) or 1
        snapshot = touch_presence(
            user_id=request.user.id,
            session_key=session_key,
            client_id=client_id,
            client_type=client_type,
            client_label=client_label,
            is_agent=bool(getattr(request.user, 'is_agent', False)),
            status=status_value,
            metadata=request.data.get('metadata') if isinstance(request.data.get('metadata'), dict) else {},
            organization_id=org_id,
        )
        event = snapshot.as_event()
        event['type'] = 'presence_update'
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)('presence_global', event)
        except Exception:
            logger.exception("Failed to broadcast REST presence update for user %s", request.user.id)
        publish_presence_bridge(snapshot, org_id=org_id)
        return Response(snapshot.as_event())

# ── Constants ────────────────────────────────────────────────────────────────

DELETE_FOR_EVERYONE_WINDOW = timedelta(hours=60)
EDIT_WINDOW = timedelta(minutes=15)
DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 200
MAX_CHANGES_LIMIT = 200  # CON-10: cap on conversations returned by /changes per page
ALLOWED_MEDIA_TYPES = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg',  # images
    '.mp4', '.webm', '.mov',                            # video
    '.mp3', '.ogg', '.wav', '.m4a', '.aac',             # audio
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.pptx',  # documents
    '.txt', '.csv', '.zip', '.tar', '.gz',              # misc
}
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB

# CON-247: the System collector identity. System (the pseudo-user that Pulse/
# scheduler/retro emitters send prompts AS) has no organization membership, so
# an agent replying to `recipient_username="system"` would otherwise 403 on the
# CON-129 cross-org gate even though the reply targets the agent's own collector
# conversation. These constants identify that identity for org-safe routing.
SYSTEM_USERNAME = "system"
SYSTEM_EMAIL = "system@shizuha.com"


def _find_existing_direct(user_id, target_id):
    """Find existing direct conversation between two users."""
    # Find conversations where both users are active participants and type is direct
    user_convs = ConversationParticipant.objects.filter(
        user_id=user_id, has_left=False,
        conversation__conversation_type='direct'
    ).values_list('conversation_id', flat=True)

    match = ConversationParticipant.objects.filter(
        conversation_id__in=user_convs,
        user_id=target_id,
        has_left=False
    ).select_related('conversation').first()

    return match.conversation if match else None


def _find_collector_conversation(sender_id: int, system_user_id: int):
    """CON-247: find the sender's org-safe collector conversation.

    The per-user System collector DM (sender + System) is the configured
    in-org fallback collector channel. The sender is a participant by
    construction, so a reply routed here is NOT a cross-org send — it is the
    sender messaging their own collector conversation. Returns None when no
    such conversation exists (a genuine cross-org attempt to an unrelated
    System user falls through to the normal CON-129 gate).
    """
    if not sender_id or not system_user_id or sender_id == system_user_id:
        return None
    return _find_existing_direct(sender_id, system_user_id)


def _is_system_collector(recipient_id: int) -> bool:
    """CON-247: is `recipient_id` the platform System collector identity?

    Best-effort: resolve via the same cached shizuha-id lookup the rest of the
    pipeline uses. Resolver failure is NOT treated as "not the collector" —
    it returns False so the caller's normal gate still applies fail-closed.
    """
    if not recipient_id:
        return False
    try:
        from connect.views.internal_api import _get_system_user_id
        return int(recipient_id) == _get_system_user_id()
    except Exception:  # noqa: BLE001 — resolver failure stays fail-closed
        return False


def _safe_broadcast(group, event):
    """Broadcast via channel layer, swallowing failures so the HTTP response is never lost."""
    from connect.metrics import record_ws_fanout

    start = time.monotonic()
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(group, event)
        record_ws_fanout(group, 'success', time.monotonic() - start)
    except Exception:
        record_ws_fanout(group, 'failure', time.monotonic() - start)
        logger.exception("Failed to broadcast %s to %s", event.get('type', '?'), group)

    # Also publish to Redis bridge for shizuha-agent unified WebSocket
    _publish_chat_event_to_bridge(group, event)


def _dm_request_hash(*, sender_id, recipient_id, content, reply_to_message_id):
    """Stable hash for CON-4 idempotency conflict detection."""
    payload = {
        'sender_id': int(sender_id),
        'recipient_id': int(recipient_id),
        'content': content,
        'reply_to_message_id': str(reply_to_message_id or ''),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _dm_response_payload(conversation, message, recipient, *, created_new_conversation, idempotent_replay, sender_is_agent, body_hash=None, delivered_via_fallback=None):
    payload = {
        'conversation_id': str(conversation.id),
        'message_id': str(message.id),
        'recipient': recipient,
        'sender_is_agent': sender_is_agent,
        'created_new_conversation': created_new_conversation,
        'idempotent_replay': idempotent_replay,
    }
    if body_hash:
        payload['body_hash'] = body_hash
    # CON-247: explicit delivered-via-fallback receipt when a collector reply
    # was routed into the sender's own in-org collector conversation instead of
    # the original (cross-org) thread target.
    if delivered_via_fallback:
        payload['delivered_via_fallback'] = True
    return payload


class _DMIdempotencyResponse(Exception):
    """Carry an idempotency response out of an atomic block with rollback."""

    def __init__(self, response):
        self.response = response


def _dm_recipient_payload_from_conversation(conversation, sender_id, recipient_id=None):
    participant = (
        ConversationParticipant.objects
        .filter(conversation=conversation, has_left=False)
        .exclude(user_id=sender_id)
        .order_by('id')
        .first()
    )
    if participant:
        return {
            'user_id': participant.user_id,
            'email': participant.user_email,
            'name': participant.user_name or participant.user_email or f'User {participant.user_id}',
        }
    fallback_id = recipient_id or sender_id
    fallback = _resolve_user_details(fallback_id)
    return {
        'user_id': fallback_id,
        'email': fallback.get('email', ''),
        'name': fallback.get('name', f'User {fallback_id}'),
    }


def _dm_idempotency_response(idempotency_record, *, request_hash=None, recipient_id=None, recipient_payload=None):
    """Return replay/conflict/pending response for a stored idempotency row."""
    if recipient_id is not None and request_hash is not None and (
        idempotency_record.recipient_id != recipient_id
        or idempotency_record.request_hash != request_hash
    ):
        return Response(
            {
                'error': 'Idempotency conflict',
                'code': 'idempotency_conflict',
                'message': 'client_message_id was already used with a different body or recipient',
            },
            status=status.HTTP_409_CONFLICT,
        )

    if idempotency_record.message_id:
        conversation = Conversation.objects.get(pk=idempotency_record.conversation_id)
        message = DirectMessage.objects.get(pk=idempotency_record.message_id)
        recipient_payload = recipient_payload or _dm_recipient_payload_from_conversation(
            conversation,
            idempotency_record.sender_id,
            idempotency_record.recipient_id,
        )
        return Response(
            _dm_response_payload(
                conversation,
                message,
                recipient_payload,
                created_new_conversation=False,
                idempotent_replay=True,
                sender_is_agent=message.sender_is_agent,
                body_hash=idempotency_record.request_hash,
            ),
            status=status.HTTP_200_OK,
        )

    return Response(
        {
            'error': 'Idempotency key is reserved but message is not persisted',
            'code': 'idempotency_pending',
        },
        status=status.HTTP_409_CONFLICT,
    )




def _hive_agent_recipient_payload(agent):
    stable_id = str(agent['stable_id'])
    display_name = agent.get('display_name') or agent.get('connect_username') or stable_id
    role = agent.get('runtime_kind') or agent.get('agent_type') or 'hive-agent'
    return {
        'user_id': hive_agent_user_id(stable_id),
        'email': hive_agent_email(agent),
        'name': f'{display_name} ({role})',
    }


def _send_dm_to_hive_agent(request, agent, *, content, client_message_id, reply_to_message_id, caller_id, caller_email, caller_name, sender_is_agent):
    """Create a direct DM to a Hive personal agent and dispatch it to Hive once."""
    recipient_payload = _hive_agent_recipient_payload(agent)
    recipient_id = recipient_payload['user_id']

    # CON-129/CON-149: hive personal-agents are owner-scoped pseudo-users, not
    # tenant users — the org_tenancy gate recognises the reserved id band and
    # returns ALLOWED for owner↔own-agent edges (this `agent` was already
    # resolved from the caller's owner-scoped Hive discovery). The gate call is
    # kept for uniformity and defence-in-depth.
    org_blocked = org_gate_response(
        check_messaging_allowed(
            caller_id, recipient_id, sender_token=getattr(request, 'auth', None),
        ),
        sender_id=caller_id, recipient_id=recipient_id,
    )
    if org_blocked is not None:
        return org_blocked
    request_hash = _dm_request_hash(
        sender_id=caller_id,
        recipient_id=recipient_id,
        content=content,
        reply_to_message_id=reply_to_message_id,
    )

    try:
        with transaction.atomic():
            if client_message_id:
                existing_idempotency = (
                    DirectMessageIdempotency.objects
                    .select_for_update()
                    .filter(sender_id=caller_id, client_message_id=client_message_id)
                    .first()
                )
                if existing_idempotency:
                    return _dm_idempotency_response(
                        existing_idempotency,
                        request_hash=request_hash,
                        recipient_id=recipient_id,
                        recipient_payload=recipient_payload,
                    )

            conversation, created_new_conversation = get_or_create_hive_agent_conversation(request, agent)
            agent_participant = ConversationParticipant.objects.get(
                conversation=conversation,
                user_id=recipient_id,
                participant_type='agent',
                has_left=False,
            )

            idempotency_record = None
            if client_message_id:
                idempotency_record, created_idempotency = DirectMessageIdempotency.objects.get_or_create(
                    sender_id=caller_id,
                    client_message_id=client_message_id,
                    defaults={
                        'recipient_id': recipient_id,
                        'conversation': conversation,
                        'request_hash': request_hash,
                    },
                )
                if not created_idempotency:
                    return _dm_idempotency_response(
                        idempotency_record,
                        request_hash=request_hash,
                        recipient_id=recipient_id,
                        recipient_payload=recipient_payload,
                    )

            _dedup_window = int(getattr(settings, 'CONNECT_DM_DEDUP_WINDOW_SECONDS', 15) or 0)
            if _dedup_window > 0 and not created_new_conversation:
                _recent_dup = (
                    DirectMessage.objects
                    .filter(
                        conversation=conversation,
                        sender_id=caller_id,
                        content=content,
                        created_at__gte=timezone.now() - timedelta(seconds=_dedup_window),
                    )
                    .order_by('-created_at')
                    .first()
                )
                if _recent_dup is not None:
                    return Response(
                        _dm_response_payload(
                            conversation,
                            _recent_dup,
                            recipient_payload,
                            created_new_conversation=False,
                            idempotent_replay=True,
                            sender_is_agent=_recent_dup.sender_is_agent,
                        ),
                        status=status.HTTP_200_OK,
                    )

            reply_to = None
            if reply_to_message_id:
                reply_to = DirectMessage.objects.filter(
                    pk=reply_to_message_id,
                    conversation=conversation,
                ).first()

            message = DirectMessage.objects.create(
                conversation=conversation,
                sender_id=caller_id,
                sender_email=caller_email,
                sender_name=caller_name,
                sender_username=getattr(request.user, 'username', '') or '',
                message_type='text',
                content=content,
                sender_is_agent=sender_is_agent,
                client_message_id=client_message_id or None,
                reply_to=reply_to,
            )
            if idempotency_record:
                idempotency_record.message = message
                idempotency_record.save(update_fields=['message', 'updated_at'])
            from connect.metrics import record_inbound_message
            record_inbound_message('api', 'hive', 'persisted')

            ConversationParticipant.objects.filter(
                conversation=conversation,
                user_id=recipient_id,
                has_left=False,
            ).update(unread_count=F('unread_count') + 1)
            conversation.refresh_from_db()
            agent_status = hive_agent_status(agent)
            if not hive_agent_is_running(agent):
                transaction.on_commit(
                    lambda conv=conversation, msg=message, part=agent_participant, st=agent_status: (
                        record_hive_agent_delivery_error(conv, msg, part, status=st)
                    )
                )
            else:
                transaction.on_commit(lambda: dispatch_hive_agent_message(conversation, message, agent_participant, agent=agent))
    except _DMIdempotencyResponse as exc:
        return exc.response

    if created_new_conversation:
        try:
            conv_payload = ConversationSerializer(conversation, context={'request': request}).data
            channel_layer = get_channel_layer()
            for pid in {caller_id, recipient_id}:
                async_to_sync(channel_layer.group_send)(
                    f'user_inbox_{pid}',
                    {
                        'type': 'conversation_created',
                        'conversation_id': str(conversation.id),
                        'conversation': conv_payload,
                    },
                )
        except Exception:
            logger.exception('Failed to broadcast hive-agent conversation_created')

    message_data = DirectMessageSerializer(message).data
    _safe_broadcast(f'chat_{conversation.id}', {'type': 'new_message', 'message_data': message_data})
    logger.info(
        'DM from user_id=%s to Hive agent %s in conversation %s (new=%s, sender_agent=%s)',
        caller_id,
        agent.get('stable_id'),
        conversation.id,
        created_new_conversation,
        sender_is_agent,
    )
    return Response(
        _dm_response_payload(
            conversation,
            message,
            recipient_payload,
            created_new_conversation=created_new_conversation,
            idempotent_replay=False,
            sender_is_agent=sender_is_agent,
            body_hash=request_hash if client_message_id else None,
        ),
        status=status.HTTP_201_CREATED,
    )


def _publish_chat_event_to_bridge(group, event):
    """Publish chat event to Redis bridge for mobile clients via shizuha-agent."""
    import json
    try:
        import redis as redis_sync
        r = redis_sync.from_url(
            settings.CHANNEL_REDIS_URL,
            socket_timeout=settings.CHANNEL_REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=settings.CHANNEL_REDIS_CONNECT_TIMEOUT,
        )

        # Extract conversation_id from group name (format: "chat_{uuid}")
        conv_id = group.replace("chat_", "", 1) if group.startswith("chat_") else None
        if not conv_id:
            r.close()
            return

        event_type = event.get('type', '')
        # Normalize event type (channel layer uses dots, we use underscores)
        event_type = event_type.replace('.', '_')

        # Build bridge payload
        bridge_data = {}
        if 'message_data' in event:
            bridge_data = event['message_data']
        elif 'message_id' in event:
            bridge_data = {'message_id': event['message_id']}
        elif 'user_id' in event:
            bridge_data = {'user_id': event['user_id']}
        if 'participant' in event:
            bridge_data = event['participant']
        if 'name' in event or 'description' in event:
            bridge_data = {k: v for k, v in event.items() if k != 'type'}

        # Publish to each participant's bridge channel
        from connect.models import ConversationParticipant
        participant_ids = list(
            ConversationParticipant.objects.filter(
                conversation_id=conv_id, has_left=False
            ).values_list('user_id', flat=True)
        )

        bridge_payload = json.dumps({
            'conv_id': conv_id,
            'event_type': event_type,
            'data': bridge_data,
        })

        for user_id in participant_ids:
            r.publish(f"shizuha:chat_events:{user_id}", bridge_payload)

        r.close()
    except Exception:
        logger.debug("Failed to publish chat event to bridge")


def _create_system_message(conversation, content, notification_class='informational'):
    """Create a system message and broadcast via WebSocket.

    CON-429 S2 (design §2.1): this leg stamps the notification taxonomy as
    the FALLBACK so direct callers cannot mint unclassified rows — the
    default is `informational` (design §2: default for system-channel
    posts). Callers that know better (e.g. task-assignment producers) pass
    `notification_class='actionable'`; the value is validated against the
    server-side allowlist and unknown values are rejected, never coerced.
    """
    if notification_class not in VALID_NOTIFICATION_CLASSES:
        raise ValueError(
            f'invalid notification_class {notification_class!r} '
            f'(allowed: {sorted(VALID_NOTIFICATION_CLASSES)})'
        )
    msg = DirectMessage.objects.create(
        conversation=conversation,
        sender_id=0, sender_email='system', sender_name='',
        message_type='system', content=content,
        notification_class=notification_class,
    )
    # Note: post_save signal on DirectMessage already updates
    # message_count, last_message_at, last_message_preview
    conversation.refresh_from_db()

    _safe_broadcast(
        f'chat_{conversation.id}',
        {'type': 'new_message', 'message_data': DirectMessageSerializer(msg).data}
    )
    return msg


class HivePersonalAgentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        agents = list_hive_agents(request)
        return Response({'count': len(agents), 'results': agents})


class HiveAgentConversationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        stable_id = request.data.get('stable_id') or request.query_params.get('stable_id')
        agent = find_hive_agent(request, stable_id)
        if not agent:
            return Response({'error': 'Hive agent not found'}, status=status.HTTP_404_NOT_FOUND)
        conversation, created = get_or_create_hive_agent_conversation(request, agent)
        output = ConversationSerializer(conversation, context={'request': request})
        return Response(output.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ConversationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing conversations.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [MessageSendThrottle]
    pagination_class = None

    def get_queryset(self):
        # Get conversations where user is a participant (exclude archived unless requested)
        participant_conversations = visible_conversation_ids_for_user(
            self.request.user.id,
            include_archived=bool(self.request.query_params.get('include_archived')),
        )

        qs = Conversation.objects.filter(
            id__in=participant_conversations
        ).prefetch_related('participants')
        if getattr(self, 'action', None) == 'list':
            # Empty DMs (open-agent with no send) used to float to the top
            # because Postgres sorts NULL last_message_at first under DESC.
            # Same rule for every counterpart — no hive/openclaw/hermes boost.
            qs = qs.exclude(last_message_at__isnull=True, message_count=0)
            # CON-425 S1: unread-only triage view — restrict to conversations
            # where the caller's own participant row still has unread > 0.
            if self.request.query_params.get('unread') in ('1', 'true', 'True'):
                qs = qs.filter(
                    participants__user_id=self.request.user.id,
                    participants__unread_count__gt=0,
                )
        return qs.order_by(F('last_message_at').desc(nulls_last=True), '-id')

    def get_serializer_class(self):
        if self.action == 'create':
            return ConversationCreateSerializer
        return ConversationSerializer

    def get_throttles(self):
        # CON-10 (P2): ack_message is high-volume (a reconnect backfill acks each
        # message); bill it against AckThrottle, not the 60/min send throttle, or a
        # backfill ack-storm returns 429s and leaves ack state partially recorded.
        if getattr(self, 'action', None) == 'ack_message':
            return [AckThrottle()]
        return super().get_throttles()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        participant_ids = request.data.get('participant_ids', [])
        participant_details = request.data.get('participant_details', {})
        conv_type = request.data.get('conversation_type', 'direct')

        # For direct conversations, use direct_key + get_or_create so a race
        # between two clients creating a DM for the same pair can't produce
        # duplicates. The partial unique index on (direct, direct_key) makes
        # concurrent inserts return the same row.
        if conv_type == 'direct' and len(participant_ids) == 1:
            target_id = participant_ids[0]
            # Block enforcement: a block in either direction is a hard send/delivery boundary.
            # The org check is delegated to the CON-129 org_tenancy gate below
            # (single canonical org gate), so this stays block-only.
            if not actor_can_reach_recipient(request.user.id, target_id):
                return Response({'error': 'Cannot start conversation with this user'}, status=status.HTTP_403_FORBIDDEN)

            # CON-129: Direct conversation creation requires a shared active org.
            org_blocked = org_gate_response(
                check_messaging_allowed(
                    request.user.id, target_id,
                    sender_token=getattr(request, 'auth', None),
                ),
                sender_id=request.user.id, recipient_id=target_id,
            )
            if org_blocked is not None:
                return org_blocked

            direct_key = Conversation.build_direct_key(request.user.id, target_id)
            with transaction.atomic():
                conversation, created = Conversation.objects.get_or_create(
                    conversation_type='direct',
                    direct_key=direct_key,
                    defaults={'created_by_id': request.user.id},
                )
                if created:
                    # Add both participants (caller admin, recipient non-admin)
                    ConversationParticipant.objects.create(
                        conversation=conversation,
                        user_id=request.user.id,
                        user_email=request.user.email,
                        # CON-52: agents have no first/last name, so get_full_name()
                        # is "" and the caller rendered blank. Fall back to username
                        # (then email) so the participant always has a label.
                        user_name=(
                            request.user.get_full_name()
                            or getattr(request.user, 'username', '')
                            or request.user.email
                        ),
                        is_admin=True,
                    )
                    details = participant_details.get(str(target_id), {})
                    from connect.services.display_names import is_placeholder_name
                    p_name = details.get('name', '')
                    p_email = details.get('email', '')
                    if is_placeholder_name(p_name, target_id):
                        p_name = ''
                    if not p_name:
                        resolved = _resolve_user_details(target_id)
                        p_name = resolved['name']
                        p_email = p_email or resolved['email']
                    ConversationParticipant.objects.create(
                        conversation=conversation,
                        user_id=target_id,
                        user_name=p_name,
                        user_email=p_email,
                    )
            try:
                from connect.services.security_monitor import (
                    emit_direct_conversation_anomaly,
                    normalize_org_ids as normalize_security_org_ids,
                )
                target_details = (
                    participant_details.get(str(target_id), {})
                    if isinstance(participant_details, dict) else {}
                )
                emit_direct_conversation_anomaly(
                    request=request,
                    conversation=conversation,
                    target_user_id=target_id,
                    target_org_ids=normalize_security_org_ids(target_details),
                    created=created,
                )
            except Exception:
                logger.exception("PLAT-1237 direct conversation anomaly detector failed")
            if not created:
                output = ConversationSerializer(conversation, context={'request': request})
                return Response(output.data, status=status.HTTP_200_OK)
            # Fall through to the broadcast block with `conversation` bound.
        else:
            blocked_ids = blocked_counterparty_ids(request.user.id)
            if any(int(pid) in blocked_ids for pid in participant_ids if int(pid) != request.user.id):
                return Response({'error': 'Cannot add blocked user to group'}, status=status.HTTP_403_FORBIDDEN)

            # CON-129: every initial group participant must share an active
            # org with the creator.
            for pid in participant_ids:
                if int(pid) == request.user.id:
                    continue
                org_blocked = org_gate_response(
                    check_messaging_allowed(
                        request.user.id, pid,
                        sender_token=getattr(request, 'auth', None),
                    ),
                    sender_id=request.user.id, recipient_id=pid,
                )
                if org_blocked is not None:
                    return org_blocked

            # Group — no uniqueness constraint (groups can legitimately share a name)
            conversation = serializer.save(created_by_id=request.user.id)

            # Add creator as participant (admin)
            ConversationParticipant.objects.create(
                conversation=conversation,
                user_id=request.user.id,
                user_email=request.user.email,
                user_name=request.user.get_full_name(),
                is_admin=True
            )

            for pid in participant_ids:
                if pid != request.user.id:
                    details = participant_details.get(str(pid), {})
                    from connect.services.display_names import is_placeholder_name
                    p_name = details.get('name', '')
                    p_email = details.get('email', '')
                    if is_placeholder_name(p_name, pid):
                        p_name = ''
                    if not p_name:
                        resolved = _resolve_user_details(pid)
                        p_name = resolved['name']
                        p_email = p_email or resolved['email']
                    ConversationParticipant.objects.create(
                        conversation=conversation,
                        user_id=pid,
                        user_name=p_name,
                        user_email=p_email,
                    )

        # For groups, create system message "X created group 'Name'"
        if conv_type == 'group' and conversation.name:
            _create_system_message(
                conversation,
                f"{request.user.get_full_name()} created group \"{conversation.name}\""
            )

        # Notify newly-added participants via their user_inbox_<user_id> WS
        # group so their already-open WebSocket auto-joins the new
        # conversation's chat group. Without this broadcast, participants who
        # created their WS before the conversation existed would only pick it
        # up on the 5-minute keepalive refresh — messages sent in that window
        # would silently drop. Agents and humans share this single inbox group.
        try:
            channel_layer = get_channel_layer()
            conv_payload = ConversationSerializer(conversation, context={'request': request}).data
            for pid in set(participant_ids + [request.user.id]):
                async_to_sync(channel_layer.group_send)(
                    f'user_inbox_{pid}',
                    {
                        'type': 'conversation_created',
                        'conversation_id': str(conversation.id),
                        'conversation': conv_payload,
                    },
                )
        except Exception as e:
            logger.warning(f"Failed to broadcast conversation_created: {e}")

        output = ConversationSerializer(conversation, context={'request': request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get', 'post'])
    def messages(self, request, pk=None):
        """Get or send messages in conversation."""
        conversation = self.get_object()

        # Verify user is participant and no active participant is separated by a block.
        participant = ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id,
            has_left=False
        ).first()

        if not participant:
            return Response(
                {'error': 'Not a participant'},
                status=status.HTTP_403_FORBIDDEN
            )
        if conversation_blocked_for_user(conversation, request.user.id):
            return Response(
                {'error': 'Conversation blocked by privacy policy'},
                status=status.HTTP_403_FORBIDDEN
            )

        if request.method == 'GET':
            # Include deleted messages — the serializer returns empty content for
            # them, and the client renders a "This message was deleted" tombstone.
            # Previously they were filtered out, which was inconsistent with the
            # chat-list preview (which shows the tombstone) and also confusing
            # when the deleted message was referenced by reply/forward chains.
            messages = DirectMessage.objects.filter(
                conversation=conversation,
            ).select_related('reply_to').prefetch_related('read_receipts')

            # Filter by cleared_at if set on participant
            if hasattr(participant, 'cleared_at') and participant.cleared_at:
                messages = messages.filter(created_at__gt=participant.cleared_at)

            # Filter out per-user deletions
            if hasattr(DirectMessage, 'deletions'):
                deleted_msg_ids = DirectMessage.objects.filter(
                    conversation=conversation,
                    deletions__user_id=request.user.id
                ).values_list('id', flat=True)
                messages = messages.exclude(id__in=deleted_msg_ids)

            # Search support
            search = request.query_params.get('search')
            if search:
                messages = messages.filter(content__icontains=search)

            # Pagination cursors (limit shared):
            #   ?before=<message_id>            — backward history scroll (newest-first window)
            #   ?after_cursor=<conversation_seq_num> — CON-10 forward backfill: reconnect
            #     recovery of messages persisted AFTER the client's last-seen seq. Ordered
            #     by the row-locked monotonic conversation_seq_num so the page is
            #     gap/duplicate-free even under concurrent sends (HLD CON-4 §5/§7).
            before = request.query_params.get('before')
            after_cursor = request.query_params.get('after_cursor')
            limit = min(
                int(request.query_params.get('limit', DEFAULT_MESSAGE_LIMIT)),
                MAX_MESSAGE_LIMIT
            )

            if after_cursor is not None:
                try:
                    after_seq = int(after_cursor)
                except (TypeError, ValueError):
                    return Response(
                        {'error': 'after_cursor must be an integer conversation_seq_num'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # Forward, ascending, gap-free: only messages with a seq strictly
                # greater than the client's cursor. Already chronological — no reverse.
                messages = messages.filter(
                    conversation_seq_num__gt=after_seq
                ).order_by('conversation_seq_num')[:limit]
                message_list = list(messages)
            elif before:
                try:
                    cursor_msg = DirectMessage.objects.get(pk=before)
                    messages = messages.filter(
                        created_at__lt=cursor_msg.created_at
                    ).order_by('-created_at')[:limit]
                    # Reverse to chronological order
                    message_list = list(messages)
                    message_list.reverse()
                except DirectMessage.DoesNotExist:
                    messages = messages.order_by('-created_at')[:limit]
                    message_list = list(messages)
                    message_list.reverse()
            else:
                # No cursor — return the latest N messages
                messages = messages.order_by('-created_at')[:limit]
                message_list = list(messages)
                message_list.reverse()

            # Mark as read — but NOT on the forward backfill path. CON-10 (P2):
            # an after_cursor fetch is a background resync, not the user opening
            # the chat, so it must not clear unread_count / advance last_read_at.
            if after_cursor is None:
                ConversationParticipant.objects.filter(
                    conversation=conversation,
                    user_id=request.user.id
                ).update(
                    last_read_at=timezone.now(),
                    unread_count=0
                )

            serializer = DirectMessageSerializer(message_list, many=True)
            has_more = len(message_list) == limit
            # CON-10 (P2): on a full page, hand back the next forward cursor (the
            # last row's monotonic conversation_seq_num) so a client that paged
            # via after_cursor can fetch the next page without re-deriving its own
            # bookmark.
            next_cursor = (
                message_list[-1].conversation_seq_num
                if has_more and message_list else None
            )
            return Response({
                'messages': serializer.data,
                'has_more': has_more,
                'next_cursor': next_cursor,
            })

        # POST - send message
        # Resolve the other participant for direct conversations (needed for both
        # block-check and idempotency-row write below).
        other_participant = None
        if conversation.conversation_type == 'direct':
            other_participant = ConversationParticipant.objects.filter(
                conversation=conversation, has_left=False
            ).exclude(user_id=request.user.id).first()
            if other_participant and not actor_can_reach_recipient(
                request.user.id, other_participant.user_id
            ):
                return Response({'error': 'Cannot send message to this user'}, status=status.HTTP_403_FORBIDDEN)
            # CON-129: send-time org gate for legacy direct conversations that
            # may still hold a cross-org participant pair. Uses the single
            # canonical org_tenancy gate (403 cross_org vs 503 lookup-unavailable).
            if other_participant:
                org_blocked = org_gate_response(
                    check_messaging_allowed(
                        request.user.id, other_participant.user_id,
                        sender_token=getattr(request, 'auth', None),
                    ),
                    sender_id=request.user.id, recipient_id=other_participant.user_id,
                )
                if org_blocked is not None:
                    return org_blocked
        else:
            # CON-142 P2: legacy groups may predate the create/add org gate.
            # Enforce the same per-recipient backstop at send time.
            recipients = ConversationParticipant.objects.filter(
                conversation=conversation, has_left=False, participant_type='human'
            ).exclude(user_id=request.user.id)
            for recipient in recipients:
                org_blocked = org_gate_response(
                    check_messaging_allowed(
                        request.user.id, recipient.user_id,
                        sender_token=getattr(request, 'auth', None),
                    ),
                    sender_id=request.user.id, recipient_id=recipient.user_id,
                )
                if org_blocked is not None:
                    return org_blocked

        serializer = DirectMessageCreateSerializer(data=request.data)
        if serializer.is_valid():
            if is_ack_only_message(serializer.validated_data.get('content') or ''):
                return Response(
                    {
                        'error': (
                            'content must be the words the recipient should see. '
                            'Do not send "Replied." — put the actual reply in content.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if other_participant and _qa_cannot_dm_production_agent(
                request.user, other_participant,
            ):
                return Response(
                    {
                        'error': (
                            'QA fixtures and test accounts cannot DM production '
                            'fleet agents'
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            # Idempotency: if client_message_id provided, return existing message
            client_msg_id = serializer.validated_data.get('client_message_id')
            if client_msg_id:
                existing = DirectMessage.objects.filter(
                    conversation=conversation,
                    client_message_id=client_msg_id,
                ).first()
                if existing:
                    return Response(
                        DirectMessageSerializer(existing).data,
                        status=status.HTTP_200_OK,
                    )

            try:
                with transaction.atomic():
                    message = serializer.save(
                        conversation=conversation,
                        sender_id=request.user.id,
                        sender_email=request.user.email,
                        sender_username=request.user.username,
                        sender_name=request.user.get_full_name()
                    )

                    # post_save signal updates message_count, last_message_at, last_message_preview.
                    # For groups, override preview with sender prefix.
                    first_name = request.user.get_full_name().split()[0] if request.user.get_full_name() else ''
                    if conversation.conversation_type == 'group' and first_name:
                        preview = message.content[:100] if message.content else ''
                        preview = f"{first_name}: {preview}"[:100]
                        Conversation.objects.filter(pk=conversation.pk).update(
                            last_message_preview=preview
                        )

                    # Update unread counts only for recipients allowed by block policy.
                    allowed_recipient_ids = [
                        uid for uid in active_participant_ids(conversation)
                        if uid != request.user.id and actor_can_reach_recipient(request.user.id, uid)
                    ]
                    if allowed_recipient_ids:
                        ConversationParticipant.objects.filter(
                            conversation=conversation,
                            has_left=False,
                            user_id__in=allowed_recipient_ids,
                        ).update(
                            unread_count=F('unread_count') + 1
                        )

                    # Write idempotency row for direct DMs so a later retry via
                    # /api/messaging/dm/ can replay instead of hitting the
                    # unique_client_message_per_conversation IntegrityError.
                    if client_msg_id and other_participant is not None:
                        idempotency_hash = _dm_request_hash(
                            sender_id=request.user.id,
                            recipient_id=other_participant.user_id,
                            content=message.content or '',
                            reply_to_message_id=message.reply_to_id,
                        )
                        DirectMessageIdempotency.objects.get_or_create(
                            sender_id=request.user.id,
                            client_message_id=client_msg_id,
                            defaults={
                                'recipient_id': other_participant.user_id,
                                'conversation': conversation,
                                'request_hash': idempotency_hash,
                                'message': message,
                            },
                        )
            except IntegrityError:
                # Race condition: another request created the message between
                # the idempotency check and save. Return the existing message.
                if client_msg_id:
                    existing = DirectMessage.objects.filter(
                        conversation=conversation,
                        client_message_id=client_msg_id,
                    ).first()
                    if existing:
                        return Response(
                            DirectMessageSerializer(existing).data,
                            status=status.HTTP_200_OK,
                        )
                return Response(
                    {'error': 'Duplicate message'},
                    status=status.HTTP_409_CONFLICT,
                )

            conversation.refresh_from_db()

            # Broadcast via WebSocket (non-fatal: message is already persisted)
            message_data = DirectMessageSerializer(message).data
            _safe_broadcast(
                f'chat_{conversation.id}',
                {'type': 'new_message', 'message_data': message_data}
            )

            # Trigger push notification for offline/backgrounded recipients
            from connect.tasks import send_dm_notification
            send_dm_notification.delay(str(message.id))

            if conversation.conversation_type == 'direct' and other_participant is not None and hive_agent_stable_id_from_participant(other_participant):
                transaction.on_commit(
                    lambda conv=conversation, msg=message, part=other_participant: dispatch_hive_agent_message(conv, msg, part)
                )

            # Detect @mentions of AI agents in group conversations
            if conversation.conversation_type == 'group':
                agent_mentions = _detect_agent_mentions(message.content, conversation.id)
                if agent_mentions:
                    from connect.tasks import notify_agent_mention
                    sender_name = request.user.get_full_name()
                    for agent_uuid, username, display_name, agent_user_id in agent_mentions:
                        # CON-129: mention delivery only fans out to agents
                        # sharing an active org with the sender. Legacy
                        # conversations may still hold cross-org participants;
                        # the gate stops delivery even there.
                        verdict = check_messaging_allowed(
                            request.user.id, agent_user_id,
                            sender_token=getattr(request, 'auth', None),
                        )
                        if verdict != ORG_ALLOWED:
                            logger.warning(
                                'CON-129 suppressed agent-mention delivery: '
                                'sender=%s agent_user=%s verdict=%s',
                                request.user.id, agent_user_id, verdict,
                            )
                            continue
                        notify_agent_mention.delay(
                            str(conversation.id),
                            str(message.id),
                            agent_uuid,
                            sender_name,
                            request.user.id,
                        )

            return Response(
                message_data,
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        """Mark conversation as read."""
        conversation = self.get_object()

        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id
        ).update(
            last_read_at=timezone.now(),
            unread_count=0
        )

        return Response({'status': 'read'})

    @action(detail=True, methods=['post'])
    def mute(self, request, pk=None):
        """Mute conversation."""
        conversation = self.get_object()
        duration = request.data.get('duration')  # in hours

        muted_until = None
        if duration:
            muted_until = timezone.now() + timezone.timedelta(hours=int(duration))

        ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id
        ).update(
            is_muted=True,
            muted_until=muted_until
        )

        return Response({'status': 'muted'})

    @action(detail=True, methods=['post'])
    def unmute(self, request, pk=None):
        """Unmute conversation."""
        conversation = self.get_object()

        ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id
        ).update(
            is_muted=False,
            muted_until=None
        )

        return Response({'status': 'unmuted'})

    @action(detail=True, methods=['post'])
    def leave(self, request, pk=None):
        """Leave a group conversation."""
        conversation = self.get_object()

        if conversation.conversation_type != 'group':
            return Response(
                {'error': 'Cannot leave direct message'},
                status=status.HTTP_400_BAD_REQUEST
            )

        participant = ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id,
            has_left=False
        ).first()

        if not participant:
            return Response({'error': 'Not a participant'}, status=status.HTTP_400_BAD_REQUEST)

        # If sole admin, auto-promote oldest remaining member
        if participant.is_admin:
            other_admins = ConversationParticipant.objects.filter(
                conversation=conversation, is_admin=True, has_left=False
            ).exclude(user_id=request.user.id).exists()

            if not other_admins:
                next_member = ConversationParticipant.objects.filter(
                    conversation=conversation, has_left=False
                ).exclude(user_id=request.user.id).order_by('joined_at').first()

                if next_member:
                    next_member.is_admin = True
                    next_member.save(update_fields=['is_admin'])
                    _create_system_message(
                        conversation,
                        f"{next_member.user_name or 'A member'} is now an admin"
                    )

        participant.has_left = True
        participant.left_at = timezone.now()
        participant.save(update_fields=['has_left', 'left_at'])

        _create_system_message(conversation, f"{request.user.get_full_name()} left")

        # Broadcast participant removed
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'participant_removed', 'user_id': request.user.id}
        )

        return Response({'status': 'left'})

    @action(detail=True, methods=['get', 'post'])
    def participants(self, request, pk=None):
        """Get or add participants."""
        conversation = self.get_object()

        if request.method == 'GET':
            participants = ConversationParticipant.objects.filter(
                conversation=conversation,
                has_left=False
            )
            participants = filter_participants_readable_to(request.user.id, participants)
            serializer = ConversationParticipantSerializer(participants, many=True)
            return Response(serializer.data)

        # POST - add participant (group only)
        if conversation.conversation_type != 'group':
            return Response(
                {'error': 'Cannot add participants to direct message'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify user is admin
        is_admin = ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response(
                {'error': 'Not authorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        user_id = request.data.get('user_id')
        user_email = request.data.get('user_email', '')
        user_name = request.data.get('user_name', '')
        participant_type = request.data.get('participant_type', 'human')
        agent_id_str = request.data.get('agent_id')
        agent_role = request.data.get('agent_role', '')

        # CON-129: group participant add requires a shared active org between
        # the admin performing the add and the user being added.
        org_blocked = org_gate_response(
            check_messaging_allowed(
                request.user.id, user_id,
                sender_token=getattr(request, 'auth', None),
            ),
            sender_id=request.user.id, recipient_id=user_id,
        )
        if org_blocked is not None:
            return org_blocked

        defaults = {
            'user_email': user_email,
            'user_name': user_name,
        }
        if participant_type == 'agent' and agent_id_str:
            import uuid as _uuid
            defaults['participant_type'] = 'agent'
            defaults['agent_id'] = _uuid.UUID(agent_id_str)
            defaults['agent_role'] = agent_role

        participant, created = ConversationParticipant.objects.get_or_create(
            conversation=conversation,
            user_id=user_id,
            defaults=defaults,
        )

        if not created and participant.has_left:
            participant.has_left = False
            participant.left_at = None
            if participant_type == 'agent' and agent_id_str:
                import uuid as _uuid
                participant.participant_type = 'agent'
                participant.agent_id = _uuid.UUID(agent_id_str)
                participant.agent_role = agent_role
            participant.save()

        # System message + broadcast. Agents and humans share the same
        # participant experience — no differentiating suffix on display names.
        added_name = user_name or user_email or f"User {user_id}"
        _create_system_message(
            conversation,
            f"{request.user.get_full_name()} added {added_name}"
        )
        _safe_broadcast(
            f'chat_{conversation.id}',
            {
                'type': 'participant_added',
                'participant': ConversationParticipantSerializer(participant).data
            }
        )

        return Response({
            'status': 'added' if created else 'rejoined',
            'participant': ConversationParticipantSerializer(participant).data
        })

    # ── Group management actions ─────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='participants/remove')
    def remove_participant(self, request, pk=None):
        """Remove a participant from group (admin only)."""
        conversation = self.get_object()
        if conversation.conversation_type != 'group':
            return Response({'error': 'Not a group'}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, is_admin=True, has_left=False
        ).exists():
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        target_id = request.data.get('user_id')
        if not target_id:
            return Response({'error': 'user_id required'}, status=status.HTTP_400_BAD_REQUEST)

        target = ConversationParticipant.objects.filter(
            conversation=conversation, user_id=target_id, has_left=False
        ).first()
        if not target:
            return Response({'error': 'User not in group'}, status=status.HTTP_404_NOT_FOUND)

        target.has_left = True
        target.left_at = timezone.now()
        target.save(update_fields=['has_left', 'left_at'])

        removed_name = target.user_name or f"User {target_id}"
        _create_system_message(
            conversation,
            f"{request.user.get_full_name()} removed {removed_name}"
        )

        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'participant_removed', 'user_id': int(target_id)}
        )

        return Response({'status': 'removed'})

    @action(detail=True, methods=['post'], url_path='participants/(?P<uid>[0-9]+)/make_admin')
    def make_admin(self, request, pk=None, uid=None):
        """Promote a participant to admin (admin only)."""
        conversation = self.get_object()
        if conversation.conversation_type != 'group':
            return Response({'error': 'Not a group'}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, is_admin=True, has_left=False
        ).exists():
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        target = ConversationParticipant.objects.filter(
            conversation=conversation, user_id=uid, has_left=False
        ).first()
        if not target:
            return Response({'error': 'User not in group'}, status=status.HTTP_404_NOT_FOUND)

        target.is_admin = True
        target.save(update_fields=['is_admin'])

        target_name = target.user_name or f"User {uid}"
        _create_system_message(
            conversation,
            f"{request.user.get_full_name()} made {target_name} an admin"
        )

        return Response({'status': 'promoted'})

    @action(detail=True, methods=['post'], url_path='participants/(?P<uid>[0-9]+)/remove_admin')
    def remove_admin(self, request, pk=None, uid=None):
        """Demote a participant from admin (admin only)."""
        conversation = self.get_object()
        if conversation.conversation_type != 'group':
            return Response({'error': 'Not a group'}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, is_admin=True, has_left=False
        ).exists():
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        target = ConversationParticipant.objects.filter(
            conversation=conversation, user_id=uid, has_left=False, is_admin=True
        ).first()
        if not target:
            return Response({'error': 'User not an admin'}, status=status.HTTP_404_NOT_FOUND)

        target.is_admin = False
        target.save(update_fields=['is_admin'])

        target_name = target.user_name or f"User {uid}"
        _create_system_message(
            conversation,
            f"{request.user.get_full_name()} removed {target_name} as admin"
        )

        return Response({'status': 'demoted'})

    def partial_update(self, request, *args, **kwargs):
        """Update group name/description (admin only)."""
        conversation = self.get_object()
        if conversation.conversation_type != 'group':
            return Response({'error': 'Not a group'}, status=status.HTTP_400_BAD_REQUEST)

        if not ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, is_admin=True, has_left=False
        ).exists():
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)

        changes = []
        new_name = request.data.get('name')
        new_desc = request.data.get('description')

        if new_name is not None and new_name != conversation.name:
            old_name = conversation.name
            conversation.name = new_name
            changes.append(f"{request.user.get_full_name()} changed the group name to \"{new_name}\"")

        if new_desc is not None and new_desc != conversation.description:
            conversation.description = new_desc
            changes.append(f"{request.user.get_full_name()} changed the group description")

        if changes:
            update_fields = ['name', 'description', 'updated_at']
            conversation.save(update_fields=update_fields)
            for msg_content in changes:
                _create_system_message(conversation, msg_content)

            _safe_broadcast(
                f'chat_{conversation.id}',
                {
                    'type': 'group_updated',
                    'name': conversation.name,
                    'description': conversation.description,
                }
            )

        output = ConversationSerializer(conversation, context={'request': request})
        return Response(output.data)

    # ── Phase 2: Conversation actions ────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def pin(self, request, pk=None):
        """Pin conversation."""
        conversation = self.get_object()
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(is_pinned=True)
        return Response({'status': 'pinned'})

    @action(detail=True, methods=['post'])
    def unpin(self, request, pk=None):
        """Unpin conversation."""
        conversation = self.get_object()
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(is_pinned=False)
        return Response({'status': 'unpinned'})

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Archive conversation."""
        conversation = self.get_object()
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(is_archived=True)
        return Response({'status': 'archived'})

    @action(detail=True, methods=['post'])
    def unarchive(self, request, pk=None):
        """Unarchive conversation."""
        # Must include archived conversations in lookup (default queryset excludes them)
        participant_convs = ConversationParticipant.objects.filter(
            user_id=request.user.id, has_left=False
        ).values_list('conversation_id', flat=True)
        conversation = get_object_or_404(Conversation, pk=pk, id__in=participant_convs)
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(is_archived=False)
        return Response({'status': 'unarchived'})

    @action(detail=True, methods=['post'])
    def clear(self, request, pk=None):
        """Clear chat history for current user."""
        conversation = self.get_object()
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(cleared_at=timezone.now())
        return Response({'status': 'cleared'})

    @action(detail=True, methods=['get'], url_path='messages/(?P<msg_id>[^/.]+)/info')
    def message_info(self, request, pk=None, msg_id=None):
        """Get detailed read/delivery info for a message."""
        conversation = self.get_object()

        # Verify user is participant and block-visible before exposing receipts.
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)

        # Verify user is participant
        if not ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=request.user.id,
            has_left=False
        ).exists():
            return Response({'error': 'Not a participant'}, status=status.HTTP_403_FORBIDDEN)

        message = get_object_or_404(DirectMessage, pk=msg_id, conversation=conversation)

        from connect.models import MessageRead, MessageDelivery
        reads = MessageRead.objects.filter(message=message).values('user_id', 'read_at')
        deliveries = MessageDelivery.objects.filter(message=message).values('user_id', 'delivered_at')

        # Get participant names for display
        participants = {p.user_id: p.user_name for p in ConversationParticipant.objects.filter(conversation=conversation)}

        return Response({
            'message_id': str(message.id),
            'sent_at': message.created_at,
            'sender_id': message.sender_id,
            'read_by': [{'user_id': r['user_id'], 'user_name': participants.get(r['user_id'], ''), 'read_at': r['read_at']} for r in reads],
            'delivered_to': [{'user_id': d['user_id'], 'user_name': participants.get(d['user_id'], ''), 'delivered_at': d['delivered_at']} for d in deliveries],
        })

    @action(detail=True, methods=['post'], url_path='delete_for_me')
    def delete_for_me(self, request, pk=None):
        """Delete conversation for current user (archive + clear)."""
        conversation = self.get_object()
        ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id
        ).update(is_archived=True, cleared_at=timezone.now())
        return Response({'status': 'deleted'})

    # ── Phase 3: Message actions ─────────────────────────────────────────────

    @action(detail=True, methods=['post'], url_path='messages/(?P<msg_id>[0-9a-f-]+)/react')
    def react_message(self, request, pk=None, msg_id=None):
        """Toggle emoji reaction on a message."""
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        message = get_object_or_404(
            DirectMessage, id=msg_id, conversation=conversation
        )
        emoji = request.data.get('emoji')
        if not emoji:
            return Response(
                {'error': 'emoji is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user_id = request.user.id
        reactions = message.reactions or {}
        user_list = reactions.get(emoji, [])

        if user_id in user_list:
            user_list.remove(user_id)
            if not user_list:
                reactions.pop(emoji, None)
            action_type = 'removed'
        else:
            user_list.append(user_id)
            reactions[emoji] = user_list
            action_type = 'added'

        message.reactions = reactions
        message.save(update_fields=['reactions', 'updated_at'])

        # Broadcast reaction update via WebSocket
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {
                'type': 'message_edited',
                'message_data': message_data
            }
        )

        return Response({
            'status': action_type,
            'reactions': reactions
        })

    @action(detail=True, methods=['post'], url_path='messages/(?P<msg_id>[0-9a-f-]+)/forward')
    def forward_message(self, request, pk=None, msg_id=None):
        """Forward a message to another conversation."""
        source_conversation = self.get_object()
        message = get_object_or_404(
            DirectMessage, id=msg_id, conversation=source_conversation
        )

        target_conversation_id = request.data.get('target_conversation_id')
        if not target_conversation_id:
            return Response(
                {'error': 'target_conversation_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify user is participant in target conversation
        target_conv = get_object_or_404(Conversation, id=target_conversation_id)
        if not can_access_conversation(source_conversation, request.user.id) or not can_access_conversation(target_conv, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        if not ConversationParticipant.objects.filter(
            conversation=target_conv,
            user_id=request.user.id,
            has_left=False
        ).exists():
            return Response(
                {'error': 'Not a participant in target conversation'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Create forwarded message in target conversation
        forwarded = DirectMessage.objects.create(
            conversation=target_conv,
            sender_id=request.user.id,
            sender_email=request.user.email,
            sender_username=request.user.username,
            sender_name=request.user.get_full_name(),
            message_type=message.message_type,
            content=message.content,
            media_url=message.media_url,
            media_metadata=message.media_metadata,
            forwarded_from=message,
        )

        # Update target conversation stats
        target_conv.refresh_from_db()

        # Update unread counts for recipients allowed by block policy.
        allowed_recipient_ids = [
            uid for uid in active_participant_ids(target_conv)
            if uid != request.user.id and actor_can_reach_recipient(request.user.id, uid)
        ]
        if allowed_recipient_ids:
            ConversationParticipant.objects.filter(
                conversation=target_conv,
                has_left=False,
                user_id__in=allowed_recipient_ids,
            ).update(
                unread_count=F('unread_count') + 1
            )

        # Broadcast to target conversation
        forwarded_data = DirectMessageSerializer(forwarded).data
        _safe_broadcast(
            f'chat_{target_conv.id}',
            {
                'type': 'new_message',
                'message_data': forwarded_data
            }
        )

        # Trigger push notification
        from connect.tasks import send_dm_notification
        send_dm_notification.delay(str(forwarded.id))

        return Response(forwarded_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='messages/(?P<msg_id>[0-9a-f-]+)/edit')
    def edit_message(self, request, pk=None, msg_id=None):
        """Edit own message."""
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        message = get_object_or_404(
            DirectMessage, id=msg_id, conversation=conversation
        )
        if message.sender_id != int(request.user.id):
            return Response(
                {'error': 'Can only edit your own messages'},
                status=status.HTTP_403_FORBIDDEN
            )
        if message.created_at < timezone.now() - EDIT_WINDOW:
            return Response({'error': 'Message can only be edited within 15 minutes'}, status=status.HTTP_400_BAD_REQUEST)
        new_content = request.data.get('content')
        if not new_content:
            return Response(
                {'error': 'Content is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        message.content = new_content
        message.is_edited = True
        message.save(update_fields=['content', 'is_edited', 'updated_at'])

        # If this was the conversation's latest message, update the chat-list
        # preview too — otherwise the chat list keeps showing stale text.
        latest = DirectMessage.objects.filter(
            conversation=conversation
        ).order_by('-created_at').first()
        if latest and latest.id == message.id:
            conversation.last_message_preview = (new_content or '')[:100]
            conversation.save(update_fields=['last_message_preview'])

        # Broadcast edit via WebSocket
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {
                'type': 'message_edited',
                'message_data': message_data
            }
        )

        return Response(message_data)

    @action(detail=True, methods=['post'], url_path='messages/(?P<msg_id>[^/.]+)/star')
    def star_message(self, request, pk=None, msg_id=None):
        """Toggle star/bookmark on a message."""
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        message = get_object_or_404(DirectMessage, pk=msg_id, conversation=conversation)
        from connect.models import MessageStar
        star, created = MessageStar.objects.get_or_create(message=message, user_id=request.user.id)
        if not created:
            star.delete()
            return Response({'starred': False})
        return Response({'starred': True}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='starred')
    def starred_messages(self, request):
        """List all messages starred by the current user."""
        from connect.models import MessageStar
        visible_conv_ids = visible_conversation_ids_for_user(request.user.id, include_archived=True)
        star_msg_ids = MessageStar.objects.filter(user_id=request.user.id).values_list('message_id', flat=True)
        messages = DirectMessage.objects.filter(
            id__in=star_msg_ids,
            conversation_id__in=visible_conv_ids,
            is_deleted=False,
        ).select_related('reply_to').order_by('-created_at')[:100]
        serializer = DirectMessageSerializer(messages, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='changes')
    def changes(self, request):
        """CON-10 (P2): conversations changed since the client's global event_id
        cursor. On reconnect a client asks "which of my conversations have new
        activity since cursor X", then backfills each via
        messages?after_cursor=<seqNum>. WebSocket fanout is only a hint; this is
        the authoritative recovery path (HLD CON-4 §5/§7).
        """
        after_cursor = request.query_params.get('after_cursor')
        try:
            after_event = int(after_cursor) if after_cursor is not None else 0
        except (TypeError, ValueError):
            return Response(
                {'error': 'after_cursor must be an integer event_id'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Conversations the caller is an active participant of (excluding ones
        # they archived, matching the base list — is_archived is per-participant —
        # unless include_archived is passed). CON-10 (P2).
        part_qs = ConversationParticipant.objects.filter(
            user_id=request.user.id, has_left=False,
        )
        if not request.query_params.get('include_archived'):
            part_qs = part_qs.filter(is_archived=False)
        conv_ids = visible_conversation_ids_for_user(
            request.user.id,
            include_archived=bool(request.query_params.get('include_archived')),
        )
        # ...that have a new message past the cursor...
        msg_changed = set(
            DirectMessage.objects
            .filter(conversation_id__in=conv_ids, event_id__gt=after_event)
            .values_list('conversation_id', flat=True)
            .distinct()
        )
        # CON-10 (P2): ...OR whose ack state advanced past the cursor on another
        # device. Acks produce no DirectMessage rows, so a client that is
        # message-current but behind on read state (e.g. a second device) would
        # otherwise miss the change. last_read_event_id is on the same event_id
        # axis, so this is comparable to the message cursor.
        ack_changed = set(
            part_qs.filter(last_read_event_id__gt=after_event)
            .values_list('conversation_id', flat=True)
        )
        changed_ids = msg_changed | ack_changed
        # CON-10 (P2): bound + page the result. A user active in many conversations
        # (a bot, a busy channel member) could otherwise get hundreds/thousands of
        # serialized conversations. Exclude archived (matching the base list) unless
        # include_archived, and page on a STABLE composite (last_message_at, id)
        # cursor so a timestamp tie at the page boundary can't skip a row.
        conv_qs = (
            Conversation.objects
            .filter(id__in=changed_ids)
            .prefetch_related('participants')
            .order_by(F('last_message_at').desc(nulls_last=True), '-id')
        )
        conv_before = request.query_params.get('conv_before')
        if conv_before:
            ts_part, _, id_part = conv_before.partition('|')
            parsed_before = parse_datetime(ts_part)
            if parsed_before is None or not id_part:
                return Response(
                    {'error': 'conv_before must be "<iso last_message_at>|<id>"'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            conv_qs = conv_qs.filter(
                Q(last_message_at__lt=parsed_before)
                | Q(last_message_at=parsed_before, id__lt=id_part)
            )
        conversations = list(conv_qs[:MAX_CHANGES_LIMIT])
        next_cursor = None
        if len(conversations) == MAX_CHANGES_LIMIT and conversations[-1].last_message_at:
            last = conversations[-1]
            next_cursor = f"{last.last_message_at.isoformat()}|{last.id}"
        serializer = ConversationSerializer(conversations, many=True, context={'request': request})
        return Response({'conversations': serializer.data, 'next_cursor': next_cursor})

    @action(detail=True, methods=['post'], url_path='messages/(?P<msg_id>[0-9a-f-]+)/ack')
    def ack_message(self, request, pk=None, msg_id=None):
        """CON-10 (P2): record a delivered/read ack as a per-client side effect,
        NEVER a new message row (HLD CON-4 §3/§6).
          type=delivered -> MessageDelivery (unique per message+user; idempotent).
          type=read      -> advance this participant's last_read_event_id to the
                            message's event_id (monotonic — never regresses) and
                            recompute unread_count.
        """
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        ack_type = (request.data.get('type') or 'delivered').strip().lower()
        if ack_type not in ('delivered', 'read'):
            return Response({'error': "type must be 'delivered' or 'read'"},
                            status=status.HTTP_400_BAD_REQUEST)
        message = get_object_or_404(DirectMessage, id=msg_id, conversation=conversation)
        participant = ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, has_left=False,
        ).first()
        if not participant:
            return Response({'error': 'Not a participant'}, status=status.HTTP_403_FORBIDDEN)

        if ack_type == 'delivered':
            # CON-10 (P2): never record a delivery receipt for the sender's own
            # message — delivered_to is the recipient set (the WS path excludes the
            # author too). No-op idempotently for own messages.
            if message.sender_id != int(request.user.id):
                # Idempotent: the unique (message, user_id) constraint dedupes concurrent acks.
                MessageDelivery.objects.get_or_create(message=message, user_id=request.user.id)
            return Response({'status': 'ok', 'type': 'delivered'})

        # CON-10 (P2): a read ack on the caller's OWN message is a no-op — it must
        # not create a self-read receipt or advance the cursor past others' unread.
        if message.sender_id == int(request.user.id):
            return Response({'status': 'ok', 'type': 'read', 'self_message': True})

        # read: advance the read cursor under a row lock so concurrent acks can't race.
        with transaction.atomic():
            p = ConversationParticipant.objects.select_for_update().get(pk=participant.pk)
            # CON-10 (P2): re-check has_left INSIDE the lock. The auth fetch above
            # runs outside the transaction, so a participant who leaves in that
            # window must not have their read cursor advanced as an ex-participant.
            if p.has_left:
                return Response({'error': 'Not a participant'}, status=status.HTTP_403_FORBIDDEN)
            # CON-10 (P2): persist read receipts for every non-sender message up to
            # and including the acked one (mirrors the WS bulk read path), so
            # get_read_by / message_info reflect everything the client has now read,
            # not just the single acked message. ignore_conflicts dedupes re-acks.
            read_ids = list(
                DirectMessage.objects
                .filter(conversation=conversation, created_at__lte=message.created_at)
                .exclude(sender_id=request.user.id)
                .values_list('id', flat=True)
            )
            if read_ids:
                MessageRead.objects.bulk_create(
                    [MessageRead(message_id=mid, user_id=request.user.id) for mid in read_ids],
                    ignore_conflicts=True,
                )
            if message.event_id is not None and (
                p.last_read_event_id is None or message.event_id > p.last_read_event_id
            ):
                p.last_read_event_id = message.event_id
                p.last_read_at = timezone.now()
                # Unread = messages past the new cursor not sent by this user.
                p.unread_count = (
                    DirectMessage.objects
                    .filter(conversation=conversation, event_id__gt=p.last_read_event_id)
                    .exclude(sender_id=request.user.id)
                    .count()
                )
                # NB: ConversationParticipant has no updated_at column.
                p.save(update_fields=['last_read_event_id', 'last_read_at', 'unread_count'])
            return Response({
                'status': 'ok', 'type': 'read',
                'last_read_event_id': p.last_read_event_id,
                'unread_count': p.unread_count,
            })

    @action(detail=True, methods=['post'], url_path='mark-read')
    def mark_read_bulk(self, request, pk=None):
        """CON-425 S1: bulk mark-as-read for one conversation.

        Body (all optional):
          older_than — ISO-8601 timestamp; only messages created at or before
                       it are marked read. Omit to mark everything.

        Mirrors ack_message type=read semantics without the per-message ack
        round-trips: advances the participant's last_read_event_id monotonically
        (never regresses), persists MessageRead receipts for the covered
        non-self messages, and recomputes unread_count under a row lock.
        """
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        participant = ConversationParticipant.objects.filter(
            conversation=conversation, user_id=request.user.id, has_left=False,
        ).first()
        if not participant:
            return Response({'error': 'Not a participant'}, status=status.HTTP_403_FORBIDDEN)

        older_than_raw = request.data.get('older_than')
        cutoff = None
        if older_than_raw:
            try:
                cutoff = parse_datetime(str(older_than_raw))
            except (TypeError, ValueError):
                cutoff = None
            if cutoff is None:
                return Response({'error': 'older_than must be an ISO-8601 timestamp'},
                                status=status.HTTP_400_BAD_REQUEST)

        covered = DirectMessage.objects.filter(
            conversation=conversation, created_at__lte=cutoff or timezone.now(),
            event_id__isnull=False,
        ).exclude(sender_id=request.user.id)

        with transaction.atomic():
            p = ConversationParticipant.objects.select_for_update().get(pk=participant.pk)
            if p.has_left:
                return Response({'error': 'Not a participant'}, status=status.HTTP_403_FORBIDDEN)
            target = covered.order_by('-event_id').values_list('event_id', flat=True).first()
            marked = 0
            if target is not None and (p.last_read_event_id is None or target > p.last_read_event_id):
                marked = covered.filter(event_id__gt=p.last_read_event_id or 0).count()
                read_ids = list(covered.filter(event_id__gt=p.last_read_event_id or 0)
                                .values_list('id', flat=True))
                if read_ids:
                    MessageRead.objects.bulk_create(
                        [MessageRead(message_id=mid, user_id=request.user.id) for mid in read_ids],
                        ignore_conflicts=True,
                    )
                p.last_read_event_id = target
                p.last_read_at = timezone.now()
                p.unread_count = (
                    DirectMessage.objects
                    .filter(conversation=conversation, event_id__gt=p.last_read_event_id)
                    .exclude(sender_id=request.user.id)
                    .count()
                )
                # NB: ConversationParticipant has no updated_at column.
                p.save(update_fields=['last_read_event_id', 'last_read_at', 'unread_count'])
            return Response({
                'status': 'ok', 'type': 'read', 'bulk': True,
                'last_read_event_id': p.last_read_event_id,
                'unread_count': p.unread_count,
                'marked_read': marked,
            })

    @action(detail=True, methods=['post'], url_path='messages/(?P<msg_id>[0-9a-f-]+)/delete')
    def delete_message(self, request, pk=None, msg_id=None):
        """Delete message (for everyone or for me)."""
        conversation = self.get_object()
        if not can_access_conversation(conversation, request.user.id):
            return Response({'error': 'Conversation blocked by privacy policy'}, status=status.HTTP_403_FORBIDDEN)
        message = get_object_or_404(
            DirectMessage, id=msg_id, conversation=conversation
        )
        delete_type = request.data.get('type', 'for_me')

        if delete_type == 'for_everyone':
            if message.sender_id != int(request.user.id):
                return Response(
                    {'error': 'Can only delete your own messages for everyone'},
                    status=status.HTTP_403_FORBIDDEN
                )
            # Enforce time window (like WhatsApp's ~60 hour limit)
            if timezone.now() - message.created_at > DELETE_FOR_EVERYONE_WINDOW:
                return Response(
                    {'error': 'Cannot delete for everyone after 60 hours'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            message.is_deleted = True
            message.deleted_at = timezone.now()
            message.save(update_fields=['is_deleted', 'deleted_at', 'updated_at'])

            # If this was the chat's latest message, update preview to the
            # WhatsApp-style tombstone so the chat list doesn't keep the
            # deleted text visible.
            latest = DirectMessage.objects.filter(
                conversation=conversation
            ).order_by('-created_at').first()
            if latest and latest.id == message.id:
                conversation.last_message_preview = 'This message was deleted'
                conversation.save(update_fields=['last_message_preview'])

            # Broadcast deletion via WebSocket
            _safe_broadcast(
                f'chat_{conversation.id}',
                {
                    'type': 'message_deleted',
                    'message_id': str(message.id)
                }
            )
        else:
            # Delete for me only
            MessageDeletion.objects.get_or_create(
                message=message, user_id=request.user.id
            )

        return Response({'status': 'deleted'})


class MediaUploadView(APIView):
    """Upload media files for chat messages."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]
    throttle_classes = [MediaUploadThrottle]

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response(
                {'error': 'No file provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate file size
        if file.size > MAX_UPLOAD_SIZE:
            return Response(
                {'error': f'File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate file type
        ext = os.path.splitext(file.name)[1].lower()
        if ext not in ALLOWED_MEDIA_TYPES:
            return Response(
                {'error': f'File type {ext} not allowed'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Generate unique filename
        filename = f"chat/{uuid.uuid4().hex}{ext}"
        upload_dir = os.path.join(settings.MEDIA_ROOT, 'chat')
        os.makedirs(upload_dir, exist_ok=True)

        filepath = os.path.join(settings.MEDIA_ROOT, filename)
        with open(filepath, 'wb+') as dest:
            for chunk in file.chunks():
                dest.write(chunk)

        url = request.build_absolute_uri(f'/{settings.MEDIA_URL}{filename}')
        return Response({
            'url': url,
            'filename': file.name,
            'size': file.size
        }, status=status.HTTP_201_CREATED)


class VoiceTurnView(APIView):
    """Persist a Grok Voice speech-to-speech turn without waking SCLI.

    The caller is the human on the Live call. The other participant must be
    an agent. Both sides are written; the agent's Connect socket is not
    notified, and a MessageDelivery receipt is pre-marked so reconnect
    replay cannot turn the transcript into a second LLM reply.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from connect.services.privacy import can_access_conversation

        conversation_id = str(request.data.get('conversation_id') or '').strip()
        user_text = str(request.data.get('user_text') or '').strip()[:4000]
        assistant_text = str(request.data.get('assistant_text') or '').strip()[:4000]
        if not conversation_id:
            return Response({'error': 'conversation_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not user_text and not assistant_text:
            return Response({'error': 'user_text or assistant_text is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            caller_id = int(getattr(request.user, 'id', 0) or 0)
        except (TypeError, ValueError):
            caller_id = 0
        if not caller_id:
            return Response({'error': 'Caller has no numeric user id'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_conversation(conversation_id, caller_id):
            return Response({'error': 'conversation_not_found_or_not_participant'}, status=status.HTTP_404_NOT_FOUND)

        conversation = Conversation.objects.filter(id=conversation_id).first()
        if conversation is None:
            return Response({'error': 'conversation_not_found_or_not_participant'}, status=status.HTTP_404_NOT_FOUND)

        peer = ConversationParticipant.objects.filter(
            conversation=conversation, has_left=False,
        ).exclude(user_id=caller_id).first()
        if peer is None:
            return Response({'error': 'peer_not_found'}, status=status.HTTP_400_BAD_REQUEST)
        peer_is_agent = (
            peer.participant_type == 'agent'
            or bool(peer.agent_id)
            or str(peer.user_email or '').endswith('@shizuha.com')
        )
        if not peer_is_agent:
            return Response({'error': 'peer_is_not_an_agent'}, status=status.HTTP_403_FORBIDDEN)

        caller_email = getattr(request.user, 'email', '') or ''
        caller_name = (
            f"{getattr(request.user, 'first_name', '')} {getattr(request.user, 'last_name', '')}".strip()
            or getattr(request.user, 'username', '')
        )
        created = []
        if user_text and _recent_voice_dup(conversation, caller_id, user_text):
            user_text = ''
        if assistant_text and _recent_voice_dup(conversation, peer.user_id, assistant_text):
            assistant_text = ''
        if not user_text and not assistant_text:
            return Response({'ok': True, 'message_ids': [], 'deduped': True}, status=status.HTTP_200_OK)
        if user_text:
            created.append(DirectMessage.objects.create(
                conversation=conversation,
                sender_id=caller_id,
                sender_email=caller_email,
                sender_name=caller_name,
                sender_username=getattr(request.user, 'username', '') or '',
                content=user_text,
                sender_is_agent=False,
            ))
        if assistant_text:
            created.append(DirectMessage.objects.create(
                conversation=conversation,
                sender_id=peer.user_id,
                sender_email=peer.user_email or '',
                sender_name=peer.user_name or peer.nickname or 'Agent',
                sender_username='',
                content=assistant_text,
                sender_is_agent=True,
                agent_id=peer.agent_id,
            ))

        last = created[-1]
        conversation.last_message_at = last.created_at
        conversation.last_message_preview = (assistant_text or user_text)[:100]
        conversation.save(update_fields=['last_message_at', 'last_message_preview'])

        for msg in created:
            MessageDelivery.objects.get_or_create(message=msg, user_id=peer.user_id)
            _safe_broadcast(
                f'chat_{conversation.id}',
                {
                    'type': 'new_message',
                    'message_data': DirectMessageSerializer(msg).data,
                    'skip_agent_notify': True,
                    'origin_channel': VOICE_TURN_ORIGIN_CHANNEL,
                },
            )

        return Response({
            'ok': True,
            'message_ids': [str(msg.id) for msg in created],
        }, status=status.HTTP_201_CREATED)


class MessageUserView(APIView):
    """
    Send a direct message from the authenticated caller (human or agent) to
    another platform user, identified by email. Finds-or-creates a Direct
    conversation between the two parties and posts the message there.

    This is the "agents can DM anyone" primitive: an agent does not need to be
    handling an inbound Connect message in order to push one out. Useful for
    proactive notifications, escalations, cross-chain delegation replies, and
    status reports that need to reach a specific user regardless of what
    triggered the agent's turn.

    Request body:
        {
            "recipient_email": "user@example.com",   # required
            "content": "Verification failed on AT-62…",  # required
            "client_message_id": "a1b2c3…",              # optional UUID for idempotency
            "reply_to_message_id": "…"                   # optional
        }

    Auth: standard JWT. Caller is the `request.user` (human or agent federated
    user). Agent-specific metadata (`sender_is_agent`, `agent_id`) is extracted
    from the JWT claims.

    Returns 201 on message created, 200 on idempotent replay.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        recipient_email = (request.data.get('recipient_email') or '').strip()
        # CON-480: `ichi@agents.shizuha.io` is an agent principal display form;
        # collapse it to the bare username or every by-username lookup 404s.
        recipient_username = normalize_username(
            request.data.get('recipient_username')
        )
        content = (request.data.get('content') or '').strip()
        client_message_id_raw = request.data.get('client_message_id')
        reply_to_message_id = request.data.get('reply_to_message_id')

        if not recipient_email and not recipient_username:
            return Response(
                {'error': 'recipient_username or recipient_email is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not content:
            return Response(
                {'error': 'content is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if is_ack_only_message(content):
            return Response(
                {
                    'error': (
                        'content must be the words the recipient should see. '
                        'Do not send "Replied." — put the actual reply in content.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate client_message_id as UUID upfront so we return 400 (not
        # a 500 from the model layer's UUIDField validation).
        client_message_id = None
        if client_message_id_raw:
            try:
                client_message_id = uuid.UUID(str(client_message_id_raw))
            except (ValueError, TypeError):
                return Response(
                    {'error': 'client_message_id must be a valid UUID'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Caller identity.  We need the durable sender id before mutable
        # recipient/block checks so an already-committed idempotent retry can
        # replay even if recipient state changed after the original send.
        try:
            caller_id = int(getattr(request.user, 'id', 0) or 0)
        except (TypeError, ValueError):
            caller_id = 0
        if not caller_id:
            return Response(
                {'error': 'Caller has no numeric user id'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if client_message_id:
            with transaction.atomic():
                existing_idempotency = (
                    DirectMessageIdempotency.objects
                    .select_for_update()
                    .filter(sender_id=caller_id, client_message_id=client_message_id)
                    .first()
                )
                if existing_idempotency and existing_idempotency.message_id:
                    # CON-129: replays are gated too — a retry must not
                    # resurface content across a tenancy boundary (covers
                    # messages committed before the gate shipped). CON-247:
                    # a replay of a collector reply into the sender's own
                    # collector conversation (recipient = System) is org-safe
                    # by construction and is NOT a cross-org resurface.
                    _replay_is_collector = False
                    try:
                        from connect.views.internal_api import _get_system_user_id
                        if int(existing_idempotency.recipient_id) == _get_system_user_id():
                            _replay_is_collector = (
                                _find_collector_conversation(
                                    caller_id, existing_idempotency.recipient_id,
                                ) is not None
                            )
                    except Exception:  # noqa: BLE001 — resolver failure stays fail-closed
                        _replay_is_collector = False
                    org_blocked = org_gate_response(
                        check_messaging_allowed(
                            caller_id,
                            existing_idempotency.recipient_id,
                            sender_token=getattr(request, 'auth', None),
                        ),
                        sender_id=caller_id,
                        recipient_id=existing_idempotency.recipient_id,
                    )
                    if org_blocked is not None and not _replay_is_collector:
                        return org_blocked
                    replay_recipient = _dm_recipient_payload_from_conversation(
                        existing_idempotency.conversation,
                        caller_id,
                        existing_idempotency.recipient_id,
                    )
                    replay_hash = _dm_request_hash(
                        sender_id=caller_id,
                        recipient_id=existing_idempotency.recipient_id,
                        content=content,
                        reply_to_message_id=reply_to_message_id,
                    )
                    if recipient_email and replay_recipient.get('email', '').lower() != recipient_email.lower():
                        return _dm_idempotency_response(
                            existing_idempotency,
                            request_hash='recipient-mismatch',
                            recipient_id=existing_idempotency.recipient_id,
                        )
                    # Username-based recipient check is skipped here: the payload
                    # returned by _dm_recipient_payload_from_conversation() has no
                    # 'username' key (only user_id/email/name). Checking
                    # replay_recipient.get('username') would always be '' and would
                    # fire a false 409 on every username-based retry.  Recipient
                    # identity is already validated by the request_hash (which
                    # encodes recipient_id) below.
                    return _dm_idempotency_response(
                        existing_idempotency,
                        request_hash=replay_hash,
                        recipient_id=existing_idempotency.recipient_id,
                        recipient_payload=replay_recipient,
                    )

        # Self-send guard by email — caller cannot DM themselves.
        caller_email = (getattr(request.user, 'email', '') or '').lower()
        caller_username = (getattr(request.user, 'username', '') or '').lower()
        if caller_email and caller_email == recipient_email.lower():
            return Response(
                {'error': 'Cannot send a direct message to yourself'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if caller_username and caller_username == recipient_username.lower():
            return Response(
                {'error': 'Cannot send a direct message to yourself'},
                status=status.HTTP_400_BAD_REQUEST,
            )


        pre_identity_hive_agent = None
        if recipient_username:
            lookup_lower = recipient_username.lower()
            if (
                lookup_lower in {'hermes', 'claw', 'openclaw'}
                or lookup_lower.startswith('hive:')
                or lookup_lower.startswith('hive-')
            ):
                pre_identity_hive_agent = find_hive_agent_by_lookup(request, recipient_username)
        if pre_identity_hive_agent:
            caller_name = (
                getattr(request.user, 'get_full_name', lambda: '')()
                or getattr(request.user, 'username', '')
                or caller_email
            )
            token = getattr(request, 'auth', None)
            sender_is_agent = False
            if token is not None:
                try:
                    sender_is_agent = bool(token.get('is_agent', False))
                except Exception:
                    pass
            return _send_dm_to_hive_agent(
                request,
                pre_identity_hive_agent,
                content=content,
                client_message_id=client_message_id,
                reply_to_message_id=reply_to_message_id,
                caller_id=caller_id,
                caller_email=caller_email,
                caller_name=caller_name,
                sender_is_agent=sender_is_agent,
            )

        # ── Resolve recipient via shizuha-id internal API ──
        # Supports two lookup forms: by email (legacy) and by username (the
        # ergonomic default — agents only see inbound message senders as
        # `[username]` prefixes, so usernames are what they have).
        shizuha_id_url = getattr(
            settings,
            'SHIZUHA_ID_API_URL',
            'http://shizuha-id:8001/api',
        ).rstrip('/')
        lookup_params: Dict[str, str] = {}
        lookup_url = f'{shizuha_id_url}/internal/users/by-email/'
        lookup_key = recipient_email
        if recipient_username:
            # Try username lookup first; fall back to email if we have both.
            lookup_url = f'{shizuha_id_url}/internal/users/by-username/'
            lookup_params = {'username': recipient_username}
            lookup_key = recipient_username
        else:
            lookup_params = {'email': recipient_email}

        try:
            import requests as _requests  # local import so Celery workers don't pay for it
            resp = _requests.get(lookup_url, params=lookup_params, timeout=5)
        except Exception as e:
            logger.exception("shizuha-id lookup failed for %s", lookup_key)
            return Response(
                {'error': f'Identity lookup failed: {e}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if resp.status_code == 404 and recipient_username and recipient_email:
            # Talk-seat auto-reply sends inbound display name as username
            # (e.g. jdoe) plus SHIZUHA_CONNECT_REPLY_EMAIL. Username
            # lookup 404 must fall through to email or the model turn is
            # silently dropped (CEO Office dead turns, 2026-08-15).
            try:
                resp = _requests.get(
                    f'{shizuha_id_url}/internal/users/by-email/',
                    params={'email': recipient_email},
                    timeout=5,
                )
                lookup_key = recipient_email
                recipient_username = ''
            except Exception as e:
                logger.exception("shizuha-id email fallback failed for %s", recipient_email)
                return Response(
                    {'error': f'Identity lookup failed: {e}'},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        if resp.status_code == 404:
            hive_agent = find_hive_agent_by_lookup(request, recipient_username or recipient_email)
            if hive_agent:
                caller_name = (
                    getattr(request.user, 'get_full_name', lambda: '')()
                    or getattr(request.user, 'username', '')
                    or caller_email
                )
                token = getattr(request, 'auth', None)
                sender_is_agent = False
                if token is not None:
                    try:
                        sender_is_agent = bool(token.get('is_agent', False))
                    except Exception:
                        pass
                return _send_dm_to_hive_agent(
                    request,
                    hive_agent,
                    content=content,
                    client_message_id=client_message_id,
                    reply_to_message_id=reply_to_message_id,
                    caller_id=caller_id,
                    caller_email=caller_email,
                    caller_name=caller_name,
                    sender_is_agent=sender_is_agent,
                )
            return Response(
                {'error': f'No user found with {"username" if recipient_username else "email"} {lookup_key}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not resp.ok:
            return Response(
                {'error': f'Identity service returned {resp.status_code}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            recipient = resp.json()
        except ValueError:
            return Response(
                {'error': 'Identity service returned non-JSON response'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            recipient_id = int(recipient.get('id') or recipient.get('user_id'))
        except (TypeError, ValueError):
            return Response(
                {'error': 'Identity service response missing user id'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        recipient_name_parts = [
            (recipient.get('first_name') or '').strip(),
            (recipient.get('last_name') or '').strip(),
        ]
        recipient_name = (
            ' '.join(p for p in recipient_name_parts if p)
            or recipient.get('username')
            or recipient_email
        )
        recipient_email_canonical = recipient.get('email') or recipient_email
        recipient_profile = recipient.get('profile') if isinstance(recipient.get('profile'), dict) else {}
        recipient_is_agent = (
            str(recipient.get('account_type') or '').lower() == 'agent'
            or str(recipient_profile.get('account_type') or '').lower() == 'agent'
        )
        recipient_payload = {
            'user_id': recipient_id,
            'email': recipient_email_canonical,
            'name': recipient_name,
            'organization_id': recipient.get('organization_id') or recipient.get('org_id'),
            'organization_ids': recipient.get('organization_ids') or recipient.get('org_ids'),
            'organizations': recipient.get('organizations'),
            'organization_memberships': recipient.get('organization_memberships'),
        }

        if caller_id == recipient_id:
            return Response(
                {'error': 'Cannot send a direct message to yourself'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from connect.services.qa_probe import is_qa_fixture_identity
        if recipient_is_agent and not is_qa_fixture_identity(
            email=recipient_email_canonical or '',
            username=(recipient.get('username') or recipient_username or ''),
        ) and is_qa_fixture_identity(email=caller_email, username=caller_username):
            return Response(
                {
                    'error': (
                        'QA fixtures and test accounts cannot DM production '
                        'fleet agents'
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        caller_name = (
            getattr(request.user, 'get_full_name', lambda: '')()
            or getattr(request.user, 'username', '')
            or caller_email
        )

        # ── Agent metadata from JWT claims ──
        # Note: we no longer read an `agent_id` claim. sender_id +
        # sender_is_agent uniquely identify the caller. The legacy
        # DirectMessage.agent_id column is retained for old data but
        # new rows get None.
        token = getattr(request, 'auth', None)
        sender_is_agent = False
        if token is not None:
            try:
                sender_is_agent = bool(token.get('is_agent', False))
            except Exception:
                pass

        # ── CON-247: org-safe collector reply routing ──
        # A reply targeting the System collector (e.g. a retro prompt's reply
        # target) is routed into the sender's OWN collector conversation — the
        # configured in-org fallback. The sender is a participant by
        # construction, so this is NOT a cross-org send; the CON-129 guard is
        # preserved for every other edge. If no collector conversation exists,
        # fall through to the normal gate (fail-closed: 403/503, no delivery).
        collector_redirect = False
        if _is_system_collector(recipient_id):
            if _find_collector_conversation(caller_id, recipient_id) is not None:
                collector_redirect = True

        if not collector_redirect:
            org_blocked = org_gate_response(
                check_messaging_allowed(caller_id, recipient_id, sender_token=token),
                sender_id=caller_id, recipient_id=recipient_id,
            )
            if org_blocked is not None:
                return org_blocked

        # ── Block enforcement ──
        from connect.models import Block
        if Block.objects.filter(
            Q(blocker_id=caller_id, blocked_id=recipient_id)
            | Q(blocker_id=recipient_id, blocked_id=caller_id)
        ).exists():
            return Response(
                {'error': 'Cannot send a direct message to this user'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ── Find-or-create direct conversation ──
        # Uses direct_key + get_or_create to make the TOCTOU race impossible
        # at the DB level (the partial unique constraint on
        # conversation_type='direct' × direct_key guarantees 1:1 uniqueness).
        direct_key = Conversation.build_direct_key(caller_id, recipient_id)
        request_hash = _dm_request_hash(
            sender_id=caller_id,
            recipient_id=recipient_id,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
        created_new_conversation = False
        idempotency_record = None
        try:
            with transaction.atomic():
                # Check durable idempotency before creating the direct
                # conversation.  Do not select_related('message') here:
                # message is nullable, and SELECT ... FOR UPDATE through that
                # outer join is unsafe on PostgreSQL.
                if client_message_id:
                    existing_idempotency = (
                        DirectMessageIdempotency.objects
                        .select_for_update()
                        .filter(sender_id=caller_id, client_message_id=client_message_id)
                        .first()
                    )
                    if existing_idempotency:
                        return _dm_idempotency_response(
                            existing_idempotency,
                            request_hash=request_hash,
                            recipient_id=recipient_id,
                            recipient_payload=recipient_payload,
                        )

                try:
                    with transaction.atomic():
                        conversation, created_new_conversation = Conversation.objects.get_or_create(
                            conversation_type='direct',
                            direct_key=direct_key,
                            defaults={'created_by_id': caller_id},
                        )
                        if created_new_conversation:
                            ConversationParticipant.objects.create(
                                conversation=conversation,
                                user_id=caller_id,
                                user_email=caller_email,
                                user_name=caller_name,
                                is_admin=True,
                                participant_type='agent' if sender_is_agent else 'human',
                            )
                            ConversationParticipant.objects.create(
                                conversation=conversation,
                                user_id=recipient_id,
                                user_email=recipient_email_canonical,
                                user_name=recipient_name,
                                participant_type='agent' if recipient_is_agent else 'human',
                            )

                        # Identity is authoritative for standard fleet agents.
                        # Existing conversations may predate account_type=agent;
                        # repair them on send so Celery's event-driven Connect
                        # wake path does not silently classify an agent as human.
                        if recipient_is_agent:
                            ConversationParticipant.objects.filter(
                                conversation=conversation,
                                user_id=recipient_id,
                                has_left=False,
                            ).exclude(participant_type='agent').update(participant_type='agent')

                        # ── Durable idempotency check (pre-write) ──
                        # get_or_create handles the concurrent first-insert
                        # race for the unique(sender, client_message_id) key.
                        # If a conflicting/replay row appears between the
                        # pre-check above and this point, raise a carrier
                        # exception so the just-created conversation savepoint
                        # rolls back instead of committing a phantom DM.
                        if client_message_id:
                            idempotency_record, created_idempotency = (
                                DirectMessageIdempotency.objects.get_or_create(
                                    sender_id=caller_id,
                                    client_message_id=client_message_id,
                                    defaults={
                                        'recipient_id': recipient_id,
                                        'conversation': conversation,
                                        'request_hash': request_hash,
                                    },
                                )
                            )
                            if not created_idempotency:
                                raise _DMIdempotencyResponse(
                                    _dm_idempotency_response(
                                        idempotency_record,
                                        request_hash=request_hash,
                                        recipient_id=recipient_id,
                                        recipient_payload=recipient_payload,
                                    )
                                )
                except _DMIdempotencyResponse:
                    raise

                # ── CON-104: content-storm dedup (server-side storm guard) ──
                # A looping sender (e.g. a wedged agent replaying its last DM every
                # heartbeat — CON-104: zen flooded hana 50+ times with one message)
                # re-sends identical content with a fresh/absent client_message_id each
                # turn, so the opt-in UUID idempotency above never catches it. If an
                # identical (sender, conversation, content) DM was created within the
                # dedup window, SUPPRESS this send: return the existing message (200,
                # idempotent_replay) with NO duplicate row and NO broadcast. New
                # conversations can't have a prior dup, so they skip the query.
                # Disable with CONNECT_DM_DEDUP_WINDOW_SECONDS=0.
                _dedup_window = int(getattr(settings, 'CONNECT_DM_DEDUP_WINDOW_SECONDS', 15) or 0)
                if _dedup_window > 0 and not created_new_conversation:
                    _recent_dup = (
                        DirectMessage.objects
                        .filter(
                            conversation=conversation,
                            sender_id=caller_id,
                            content=content,
                            created_at__gte=timezone.now() - timedelta(seconds=_dedup_window),
                        )
                        .order_by('-created_at')
                        .first()
                    )
                    if _recent_dup is not None:
                        logger.warning(
                            "CON-104 DM storm dedup: suppressed duplicate DM from "
                            "sender_id=%s to recipient_id=%s within %ss window (content len=%d)",
                            caller_id, recipient_id, _dedup_window, len(content),
                        )
                        return Response(
                            _dm_response_payload(
                                conversation, _recent_dup, recipient_payload,
                                created_new_conversation=False,
                                idempotent_replay=True,
                                sender_is_agent=sender_is_agent,
                            ),
                            status=status.HTTP_200_OK,
                        )

                # ── Resolve reply_to if provided ──
                reply_to = None
                if reply_to_message_id:
                    reply_to = DirectMessage.objects.filter(
                        pk=reply_to_message_id,
                        conversation=conversation,
                    ).first()

                # ── Create the message ──
                try:
                    message = DirectMessage.objects.create(
                        conversation=conversation,
                        sender_id=caller_id,
                        sender_email=caller_email,
                        sender_name=caller_name,
                        sender_username=getattr(request.user, 'username', '') or '',
                        message_type='text',
                        content=content,
                        sender_is_agent=sender_is_agent,
                        client_message_id=client_message_id or None,
                        reply_to=reply_to,
                    )
                    if idempotency_record:
                        idempotency_record.message = message
                        idempotency_record.save(update_fields=['message', 'updated_at'])
                    from connect.metrics import record_inbound_message
                    record_inbound_message('api', 'direct', 'persisted')
                except IntegrityError as exc:
                    from connect.metrics import record_message_persistence_failure
                    record_message_persistence_failure('api', 'direct', exc)
                    # Legacy/direct-message unique constraint race: another request
                    # wrote the same client_message_id before the idempotency row
                    # was available. Preserve replay/conflict semantics.
                    if client_message_id:
                        existing_record = DirectMessageIdempotency.objects.select_for_update().filter(
                            sender_id=caller_id,
                            client_message_id=client_message_id,
                        ).first()
                        if existing_record and existing_record.message_id and existing_record.request_hash == request_hash:
                            existing = existing_record.message
                            return Response(
                                _dm_response_payload(
                                    conversation,
                                    existing,
                                    recipient_payload,
                                    created_new_conversation=False,
                                    idempotent_replay=True,
                                    sender_is_agent=existing.sender_is_agent,
                                    body_hash=existing_record.request_hash,
                                ),
                                status=status.HTTP_200_OK,
                            )
                    raise

                # Bump recipient's unread count (post_save signal handles
                # message_count/last_message_at on the conversation itself)
                ConversationParticipant.objects.filter(
                    conversation=conversation,
                    user_id=recipient_id,
                    has_left=False,
                ).update(unread_count=F('unread_count') + 1)

                conversation.refresh_from_db()

        except _DMIdempotencyResponse as exc:
            return exc.response
        try:
            from connect.services.security_monitor import emit_direct_message_anomalies
            emit_direct_message_anomalies(
                request=request,
                message=message,
                conversation=conversation,
                recipient_payload=recipient_payload,
                sender_is_agent=sender_is_agent,
                created_new_conversation=created_new_conversation,
            )
        except Exception:
            logger.exception("PLAT-1237 security anomaly detector failed")

        # ── Broadcast conversation_created if we just created it ──
        # Both participants need to know so their user_inbox WS consumers
        # auto-join the new chat_<conv_id> group immediately. Agents and
        # humans share the same user_inbox_<id> group — no branching needed.
        if created_new_conversation:
            try:
                conv_payload = ConversationSerializer(
                    conversation, context={'request': request}
                ).data
                channel_layer = get_channel_layer()
                for pid in {caller_id, recipient_id}:
                    async_to_sync(channel_layer.group_send)(
                        f'user_inbox_{pid}',
                        {
                            'type': 'conversation_created',
                            'conversation_id': str(conversation.id),
                            'conversation': conv_payload,
                        },
                    )
            except Exception:
                logger.exception("Failed to broadcast conversation_created")

        # ── Broadcast new_message to the chat group ──
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'new_message', 'message_data': message_data},
        )

        # ── Push notification to offline recipients ──
        try:
            from connect.tasks import send_dm_notification
            send_dm_notification.delay(str(message.id))
        except Exception:
            logger.exception("Failed to enqueue push notification")

        logger.info(
            "DM from user_id=%s to user_id=%s in conversation %s (new=%s, agent=%s)",
            caller_id,
            recipient_id,
            conversation.id,
            created_new_conversation,
            sender_is_agent,
        )

        return Response(
            _dm_response_payload(
                conversation,
                message,
                recipient_payload,
                created_new_conversation=created_new_conversation,
                idempotent_replay=False,
                sender_is_agent=sender_is_agent,
                body_hash=request_hash if client_message_id else None,
                delivered_via_fallback=collector_redirect,
            ),
            status=status.HTTP_201_CREATED,
        )


class SentMessagesView(APIView):
    """
    CON-77: the caller's own outbox — recent direct messages they have SENT,
    newest-first. Lets an agent detect whether it already sent an equivalent
    message (e.g. replying to the same standup ping multiple times across
    heartbeats / session restarts) without tracking message IDs externally.

    Returns ONLY the caller's own sent messages (`sender_id == request.user.id`),
    so it exposes no other user's private content; block rules already gate
    send/receive upstream.

    Query params:
        limit:              max messages (default 20, capped at 100)
        since:              ISO-8601 timestamp; only messages sent at/after it
        recipient_username: only messages sent to this username
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        qs = DirectMessage.objects.filter(sender_id=user_id, is_deleted=False)

        since = request.query_params.get('since')
        if since:
            dt = parse_datetime(since)
            if dt is not None:
                qs = qs.filter(created_at__gte=dt)

        # CON-480: accept the @agents.shizuha.io principal display form here too.
        recipient_username = normalize_username(
            request.query_params.get('recipient_username')
        )
        if recipient_username:
            rid = _resolve_user_id_by_username(recipient_username)
            if rid is None:
                return Response(
                    {'error': f'No user found with username {recipient_username}'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            conv_ids = list(
                ConversationParticipant.objects
                .filter(user_id=rid, has_left=False)
                .values_list('conversation_id', flat=True)
            )
            qs = qs.filter(conversation_id__in=conv_ids)

        try:
            limit = int(request.query_params.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))

        messages = list(qs.order_by('-created_at')[:limit])

        # Resolve each conversation's recipient = the other (non-left) participant.
        conv_ids = {m.conversation_id for m in messages}
        recip_by_conv = {}
        for p in (
            ConversationParticipant.objects
            .filter(conversation_id__in=conv_ids, has_left=False)
            .exclude(user_id=user_id)
            .order_by('id')
        ):
            recip_by_conv.setdefault(p.conversation_id, p)

        username_by_id = _resolve_usernames_by_ids(
            {p.user_id for p in recip_by_conv.values()}
        )

        results = []
        for m in messages:
            p = recip_by_conv.get(m.conversation_id)
            results.append({
                'message_id': str(m.id),
                'conversation_id': str(m.conversation_id),
                'recipient_user_id': p.user_id if p else None,
                'recipient_username': (username_by_id.get(p.user_id) or None) if p else None,
                'recipient_name': (p.user_name or p.user_email or None) if p else None,
                'content_preview': (m.content or '')[:140],
                'sent_at': m.created_at.isoformat(),
            })

        return Response({'count': len(results), 'messages': results})

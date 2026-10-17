"""
Internal API endpoints for service-to-service communication.

These endpoints are secured by X-Internal-Service header validation
and Docker network isolation (nginx blocks external access).

Endpoints:
  POST /connect/api/internal/conversations/{id}/agent-message/
      Create a DirectMessage from an AI agent and broadcast via WS.

  POST /connect/api/internal/conversations/{id}/agent-typing/
      Send typing indicator for an AI agent via channel layer.
"""

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import timedelta

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage, PresenceSession,
    VerifiedAlertDelivery, VALID_NOTIFICATION_CLASSES,
)
from connect.serializers.messaging import DirectMessageSerializer
from connect.services.org_tenancy import get_user_org_ids
from connect.views.messaging import is_ack_only_message

logger = logging.getLogger(__name__)

ALLOWED_INTERNAL_SERVICES = {
    'shizuha-agent',
    'shizuha-connect',
    'shizuha-pulse',
    # Pulse system messages use this sender_service in the signed body; keep it
    # allowlisted separately from the X-Internal-Service header value.
    'pulse',
    # GitHub @mention -> System DM bridge (github-push-sync-webhook, origin ns):
    # PR/issue comments mentioning an agent's GitHub login become signed system
    # DMs so agents notice review pings like any alert (operator 2026-07-11).
    # Messages are HMAC-signed with the shared origin secret; the sender must
    # ALSO be in CONNECT_SYSTEM_MESSAGE_ALLOWLIST for the origin-token gate.
    'github-bridge',
    # Origin/Forgejo PR comment and review @mention bridge (PLAT-4684).
    'forgejo-bridge',
    # Fleet control plane: read-only unread-recipients poll for the
    # unread-wake sweep (HIVE-628). Not in the system-message allowlist.
    'shizuha-hive',
}

BOUNDARY_EVENT_SERVICE_CONTRACT = {
    'shizuha-wiki': {
        'service': 'wiki',
        'operation': 'wiki.explicit_org_read',
        'reason': 'cross_tenant_org_scope',
    },
    'shizuha-admin': {
        'service': 'admin',
        'operation': 'admin.su_dashboard',
        'reason': 'superuser_endpoint',
    },
    'shizuha-id': {
        'service': 'id',
        'operation': 'id.admin_list_services',
        'reason': 'staff_endpoint',
    },
}

_BOUNDARY_EVENT_TOP_LEVEL_FIELDS = {
    'specversion', 'id', 'source', 'type', 'time', 'data',
}
_BOUNDARY_EVENT_DATA_FIELDS = {
    'service', 'operation', 'reason', 'actor_user_id', 'actor_username',
    'source_org_ids', 'target_org_id', 'method', 'route', 'correlation_id',
}
_BOUNDED_EVENT_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


AGENT_MESSAGE_IDEMPOTENCY_NAMESPACE = uuid.UUID('f3711e6c-b0cf-4dd1-b245-ecf9f603f47b')


def _validate_internal_service(request):
    """Validate the X-Internal-Service header."""
    service = request.META.get('HTTP_X_INTERNAL_SERVICE', '')
    return service in ALLOWED_INTERNAL_SERVICES


def _agent_message_client_uuid(raw_client_message_id, *, conversation_id, agent_user_id):
    """Normalize agent-message idempotency keys to DirectMessage.client_message_id.

    The public DirectMessage field is a UUID, but service bridges may carry
    stable namespaced strings (for example ``connect-runtime:<sha256>``).
    UUID values are preserved for clients that already generate them; non-UUID
    keys are deterministically mapped to UUIDv5 within a Connect-owned
    namespace so the existing unique(conversation, client_message_id)
    constraint can provide durable crash/retry idempotency.
    """
    if raw_client_message_id in (None, ''):
        return None
    raw = str(raw_client_message_id).strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        return uuid.uuid5(
            AGENT_MESSAGE_IDEMPOTENCY_NAMESPACE,
            f"{conversation_id}:{agent_user_id}:{raw}",
        )


def _same_agent_message(existing, *, agent_user_id, content, reply_to):
    try:
        sender_id = int(agent_user_id)
    except (TypeError, ValueError):
        sender_id = agent_user_id
    reply_to_id = getattr(reply_to, 'id', None)
    return (
        existing.sender_id == sender_id
        and existing.content == content
        and existing.reply_to_id == reply_to_id
        and existing.sender_is_agent
    )


def _system_message_secret() -> str:
    configured = getattr(settings, 'CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET', '') or ''
    if configured:
        return configured
    if settings.DEBUG:
        return settings.SECRET_KEY
    return ''


def _validate_system_origin_token(request, *, sender_service: str, recipient_user_id: int, content: str) -> bool:
    """Validate short-lived HMAC origin token for system-message bypasses.

    Token format: ``<unix_ts>:<hex_hmac>`` over
    ``sender_service|recipient_user_id|sha256(content)|ts``. The service name
    must also be in the server-owned allowlist. This gives automated system
    messages an explicit, enumerable bypass without letting arbitrary internal
    callers inject user-visible content into blocked/privacy-protected paths.
    """
    allowlist = set(getattr(settings, 'CONNECT_SYSTEM_MESSAGE_ALLOWLIST', ALLOWED_INTERNAL_SERVICES))
    if sender_service not in allowlist:
        return False
    secret = _system_message_secret()
    if not secret:
        return False
    token = request.META.get('HTTP_X_SYSTEM_MESSAGE_TOKEN', '')
    try:
        raw_ts, supplied = token.split(':', 1)
        ts = int(raw_ts)
    except (ValueError, AttributeError):
        return False
    max_age = int(getattr(settings, 'CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS', 300))
    if abs(int(time.time()) - ts) > max_age:
        return False
    body_hash = hashlib.sha256((content or '').encode('utf-8')).hexdigest()
    payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
    expected = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _security_event_secret() -> str:
    """A dedicated producer secret; never reuse the system-message bypass."""
    return getattr(settings, 'CONNECT_SECURITY_EVENT_ORIGIN_SECRET', '') or ''


def _canonical_security_event(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')


def _validate_security_event_origin(request, *, sender_service: str, payload: dict) -> bool:
    allowlist = set(getattr(
        settings,
        'CONNECT_SECURITY_EVENT_ALLOWLIST',
        BOUNDARY_EVENT_SERVICE_CONTRACT,
    ))
    if sender_service not in allowlist or sender_service not in BOUNDARY_EVENT_SERVICE_CONTRACT:
        return False
    secret = _security_event_secret()
    if not secret:
        return False
    try:
        timestamp = int(request.META.get('HTTP_X_SECURITY_EVENT_TIMESTAMP', ''))
    except (TypeError, ValueError):
        return False
    max_age = int(getattr(settings, 'CONNECT_SECURITY_EVENT_TOKEN_MAX_AGE_SECONDS', 300))
    if abs(int(time.time()) - timestamp) > max_age:
        return False
    supplied = request.META.get('HTTP_X_SECURITY_EVENT_SIGNATURE', '')
    body_hash = hashlib.sha256(_canonical_security_event(payload)).hexdigest()
    signed = f'{sender_service}|{timestamp}|{body_hash}'
    expected = hmac.new(
        secret.encode('utf-8'),
        signed.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def _bounded_text(value, *, max_len: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError('expected text')
    value = value.strip()
    if (required and not value) or len(value) > max_len:
        raise ValueError('invalid text length')
    return value


def _sanitize_boundary_event(payload: dict, *, sender_service: str) -> dict:
    """Validate the fixed, secret-free boundary-denial event contract.

    This intentionally rejects arbitrary producer fields. In particular, request
    bodies, query text, authorization values, response data, and tokens can never
    enter the alert queue because they are not members of this allowlist.
    """
    if not isinstance(payload, dict) or set(payload) != _BOUNDARY_EVENT_TOP_LEVEL_FIELDS:
        raise ValueError('invalid event envelope')
    if payload.get('specversion') != '1.0' or payload.get('type') != 'platform.boundary_denial':
        raise ValueError('invalid event type')

    event_id = _bounded_text(payload.get('id'), max_len=128)
    if not _BOUNDED_EVENT_ID_RE.fullmatch(event_id):
        raise ValueError('invalid event id')
    event_time = _bounded_text(payload.get('time'), max_len=40)
    contract = BOUNDARY_EVENT_SERVICE_CONTRACT[sender_service]
    if payload.get('source') != f"shizuha://{contract['service']}":
        raise ValueError('invalid event source')

    data = payload.get('data')
    if not isinstance(data, dict) or set(data) != _BOUNDARY_EVENT_DATA_FIELDS:
        raise ValueError('invalid event data')
    for key in ('service', 'operation', 'reason'):
        if data.get(key) != contract[key]:
            raise ValueError(f'invalid {key}')

    actor_user_id = int(data.get('actor_user_id'))
    if actor_user_id <= 0:
        raise ValueError('invalid actor')
    actor_username = _bounded_text(data.get('actor_username'), max_len=150)
    correlation_id = _bounded_text(data.get('correlation_id'), max_len=128)
    if not _BOUNDED_EVENT_ID_RE.fullmatch(correlation_id):
        raise ValueError('invalid correlation id')
    method = _bounded_text(data.get('method'), max_len=12)
    if method not in {'GET', 'HEAD', 'POST', 'PATCH', 'PUT', 'DELETE'}:
        raise ValueError('invalid method')
    route = _bounded_text(data.get('route'), max_len=160)

    raw_org_ids = data.get('source_org_ids')
    if not isinstance(raw_org_ids, list) or len(raw_org_ids) > 32:
        raise ValueError('invalid source org ids')
    source_org_ids = sorted({int(value) for value in raw_org_ids})
    if any(value <= 0 for value in source_org_ids):
        raise ValueError('invalid source org id')
    target_org_id = data.get('target_org_id')
    if target_org_id is not None:
        target_org_id = int(target_org_id)
        if target_org_id <= 0:
            raise ValueError('invalid target org')

    return {
        'event_type': 'platform.boundary_denial',
        'event_id': event_id,
        'event_source': payload['source'],
        'reasons': [contract['reason']],
        'reason': contract['reason'],
        'service': contract['service'],
        'operation': contract['operation'],
        'actor_user_id': actor_user_id,
        'actor_username': actor_username,
        'source_org_ids': source_org_ids,
        'target_org_id': target_org_id,
        'request_method': method,
        'route': route,
        'correlation_id': correlation_id,
        'detected_at': event_time,
        'received_at': timezone.now().isoformat(),
    }


class InternalBoundarySecurityEventView(APIView):
    """Authenticated ingest for sanitized Wiki/Admin/ID boundary denials."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        sender_service = request.META.get('HTTP_X_INTERNAL_SERVICE', '')
        payload = request.data
        if not _validate_security_event_origin(
            request,
            sender_service=sender_service,
            payload=payload,
        ):
            try:
                from connect.metrics import record_connect_security_alert_failure
                record_connect_security_alert_failure('ingest_auth_rejected')
            except Exception:
                pass
            return Response({'error': 'Forbidden'}, status=403)

        try:
            event = _sanitize_boundary_event(payload, sender_service=sender_service)
        except (TypeError, ValueError, OverflowError):
            try:
                from connect.metrics import record_connect_security_alert_failure
                record_connect_security_alert_failure('ingest_invalid_event')
            except Exception:
                pass
            return Response({'error': 'Invalid security event'}, status=400)

        from connect.services.security_outbox import accept_security_event
        try:
            row, duplicate, conflict = accept_security_event(
                producer_service=sender_service,
                event=event,
            )
        except DatabaseError:
            logger.exception('CON-217 failed to durably accept security event')
            return Response(
                {'error': 'Security event acceptance unavailable'},
                status=503,
            )
        if conflict:
            return Response(
                {'error': 'Security event identity conflicts with accepted payload'},
                status=409,
            )
        return Response({
            'status': 'accepted',
            'duplicate': duplicate,
            'acceptance_id': str(row.pk),
        }, status=202)



_ALLOWED_AUTH_REJECT_CODES = {4401, 4403, 1008}


def _active_agent_presence_sessions(*, user_id: int | None = None, username: str = ''):
    """Return scoped live agent WS sessions eligible for an auth-reject test.

    This is intentionally narrow: one selected agent user, active non-offline
    presence rows, agent sessions only, and rows that recorded a concrete
    Channels channel_name from UserChatConsumer.
    """
    qs = (
        PresenceSession.objects
        .filter(is_agent=True, channel_name__gt='')
        .exclude(status='offline')
        .order_by('-last_heartbeat_at')
    )
    if user_id is not None:
        qs = qs.filter(user_id=int(user_id))
    if username:
        qs = qs.filter(metadata__username=username)
    return qs

def _safe_broadcast(group, event):
    """Broadcast via channel layer, swallowing failures."""
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(group, event)
    except Exception:
        logger.exception("Failed to broadcast %s to %s", event.get('type', '?'), group)


def _alert_request_hash(*, organization_id, requester_id, recipient_id, content):
    canonical = json.dumps({
        'organization_id': int(organization_id),
        'requester_id': int(requester_id),
        'recipient_id': int(recipient_id),
        'content': content,
    }, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


class InternalVerifiedAlertDeliveryView(APIView):
    """Closed PLAT-4571 destination: atomically persist one receipt + DM."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request) or request.META.get('HTTP_X_INTERNAL_SERVICE') != 'shizuha-pulse':
            return Response({'error': 'Forbidden'}, status=403)
        try:
            organization_id = int(request.data['organization_id'])
            requester_id = int(request.data['requester_id'])
            recipient_id = int(request.data['recipient_id'])
            client_message_id = uuid.UUID(str(request.data['client_message_id']))
        except (KeyError, TypeError, ValueError, AttributeError):
            return Response({'error': 'invalid_verified_alert_delivery'}, status=400)
        content = str(request.data.get('content', ''))
        if not content or len(content) > 20000:
            return Response({'error': 'invalid_verified_alert_delivery'}, status=400)

        requester_orgs = get_user_org_ids(requester_id)
        recipient_orgs = get_user_org_ids(recipient_id)
        if requester_orgs is None or recipient_orgs is None:
            return Response({'error': 'org_lookup_unavailable'}, status=503)
        if organization_id not in requester_orgs or organization_id not in recipient_orgs:
            return Response({'error': 'exact_org_authority_required'}, status=403)

        request_hash = _alert_request_hash(
            organization_id=organization_id, requester_id=requester_id,
            recipient_id=recipient_id, content=content,
        )
        try:
            with transaction.atomic():
                existing = VerifiedAlertDelivery.objects.select_for_update().filter(
                    requester_id=requester_id, client_message_id=client_message_id,
                ).first()
                if existing:
                    if existing.request_hash != request_hash:
                        return Response({'error': 'client_message_id_conflict'}, status=409)
                    return Response({'receipt_id': str(existing.id)}, status=200)

                direct_key = Conversation.build_direct_key(requester_id, recipient_id)
                conversation, _ = Conversation.objects.get_or_create(
                    conversation_type='direct', direct_key=direct_key,
                    defaults={'created_by_id': requester_id},
                )
                ConversationParticipant.objects.get_or_create(
                    conversation=conversation, user_id=requester_id,
                    defaults={'participant_type': 'system', 'is_admin': True},
                )
                ConversationParticipant.objects.get_or_create(
                    conversation=conversation, user_id=recipient_id,
                    defaults={'participant_type': 'human'},
                )
                message = DirectMessage.objects.create(
                    conversation=conversation, sender_id=requester_id,
                    sender_name='Shizuha Alerts', sender_username='system',
                    message_type='system', content=content,
                    client_message_id=client_message_id,
                )
                delivery = VerifiedAlertDelivery.objects.create(
                    organization_id=organization_id, requester_id=requester_id,
                    recipient_id=recipient_id, client_message_id=client_message_id,
                    request_hash=request_hash, message=message,
                )
                message_data = DirectMessageSerializer(message).data
                transaction.on_commit(lambda: _safe_broadcast(
                    f'chat_{conversation.id}',
                    {'type': 'new_message', 'message_data': message_data},
                ))
        except IntegrityError:
            existing = VerifiedAlertDelivery.objects.filter(
                requester_id=requester_id, client_message_id=client_message_id,
            ).first()
            if existing and existing.request_hash == request_hash:
                return Response({'receipt_id': str(existing.id)}, status=200)
            return Response({'error': 'client_message_id_conflict'}, status=409)
        return Response({'receipt_id': str(delivery.id)}, status=201)

    def get(self, request, receipt_id):
        if not _validate_internal_service(request) or request.META.get('HTTP_X_INTERNAL_SERVICE') != 'shizuha-pulse':
            return Response({'error': 'Forbidden'}, status=403)
        try:
            requester_id = int(request.query_params['requester_id'])
            organization_id = int(request.query_params['organization_id'])
        except (KeyError, TypeError, ValueError):
            return Response({'error': 'invalid_receipt_lookup'}, status=400)
        requester_orgs = get_user_org_ids(requester_id)
        if requester_orgs is None:
            return Response({'error': 'org_lookup_unavailable'}, status=503)
        if organization_id not in requester_orgs:
            return Response({'error': 'receipt_not_found'}, status=404)
        try:
            delivery = VerifiedAlertDelivery.objects.get(
                id=receipt_id, organization_id=organization_id,
                requester_id=requester_id,
            )
        except VerifiedAlertDelivery.DoesNotExist:
            return Response({'error': 'receipt_not_found'}, status=404)
        recipient_orgs = get_user_org_ids(delivery.recipient_id)
        if recipient_orgs is None:
            return Response({'error': 'org_lookup_unavailable'}, status=503)
        if organization_id not in recipient_orgs:
            return Response({'error': 'receipt_not_found'}, status=404)
        return Response({'receipt_id': str(delivery.id), 'state': 'accepted'}, status=200)


class InternalAgentMessageView(APIView):
    """
    Create a DirectMessage from an AI agent and broadcast to all participants.

    Called by shizuha-agent after generating a response to a group @mention.

    Request body:
        {
            "agent_id": "uuid",
            "agent_user_id": int,       # Agent's shizuha_id_user_id
            "agent_name": "Kai",
            "agent_email": "kai@shizuha.com",
            "content": "Here's the fix...",
            "reply_to_id": "uuid" (optional),
            "client_message_id": "uuid or stable idempotency key" (optional)
        }
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, conversation_id):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        try:
            conversation = Conversation.objects.get(pk=conversation_id)
        except Conversation.DoesNotExist:
            return Response({'error': 'Conversation not found'}, status=404)

        agent_id = request.data.get('agent_id')
        agent_user_id = request.data.get('agent_user_id')
        agent_name = request.data.get('agent_name', 'AI Agent')
        agent_email = request.data.get('agent_email', '')
        content = request.data.get('content', '')
        reply_to_id = request.data.get('reply_to_id')
        client_message_id = _agent_message_client_uuid(
            request.data.get('client_message_id'),
            conversation_id=conversation.id,
            agent_user_id=agent_user_id,
        )

        if not content:
            return Response({'error': 'content is required'}, status=400)
        if not agent_user_id:
            return Response({'error': 'agent_user_id is required'}, status=400)

        # SCLI-756: an agent "replying" to a forged always-on inbox example
        # (e.g. `[hritik] Hi <agent>, please reply with the word: pong`) emits
        # a one-word pong DM — a role-leak hallucination (the agent answered a
        # user turn it fabricated itself). The user-facing HTTP DM path
        # already rejects ack-only content; the agent path must too, or the
        # forged pongs land on the operator exactly through this view. The
        # exact-match regex is fail-open by construction: any real sentence
        # delivers, and sanctioned one-line reply protocols (e.g.
        # "CON-21 pong <seat>-<date>") carry payloads and pass.
        if is_ack_only_message(content):
            return Response(
                {
                    'error': (
                        'forged-inbox pong suspected: trivial ack with no '
                        'conversational context. Fabricated user turns are a '
                        'role-leak hallucination — do not reply to them; '
                        'emit no assistant text instead.'
                    )
                },
                status=400,
            )

        # Verify agent is a participant
        participant = ConversationParticipant.objects.filter(
            conversation=conversation,
            participant_type='agent',
            has_left=False,
            agent_id=agent_id,
        ).first()

        if not participant:
            return Response({'error': 'Agent is not a participant in this conversation'}, status=403)

        # Create the message
        reply_to = None
        if reply_to_id:
            reply_to = DirectMessage.objects.filter(
                pk=reply_to_id, conversation=conversation
            ).first()

        if client_message_id:
            existing = DirectMessage.objects.filter(
                conversation=conversation,
                client_message_id=client_message_id,
            ).first()
            if existing:
                if not _same_agent_message(
                    existing,
                    agent_user_id=agent_user_id,
                    content=content,
                    reply_to=reply_to,
                ):
                    return Response(
                        {'error': 'client_message_id was already used with a different agent message'},
                        status=409,
                    )
                return Response(DirectMessageSerializer(existing).data, status=200)

        try:
            message = DirectMessage.objects.create(
                conversation=conversation,
                sender_id=agent_user_id,
                sender_email=agent_email,
                sender_name=agent_name,
                message_type='text',
                content=content,
                sender_is_agent=True,
                agent_id=agent_id,
                reply_to=reply_to,
                client_message_id=client_message_id,
            )
        except IntegrityError:
            if not client_message_id:
                raise
            existing = DirectMessage.objects.filter(
                conversation=conversation,
                client_message_id=client_message_id,
            ).first()
            if existing and _same_agent_message(
                existing,
                agent_user_id=agent_user_id,
                content=content,
                reply_to=reply_to,
            ):
                return Response(DirectMessageSerializer(existing).data, status=200)
            return Response(
                {'error': 'client_message_id was already used with a different agent message'},
                status=409,
            )

        # Update conversation stats (post_save signal handles message_count etc.)
        conversation.refresh_from_db()

        # Override preview with agent prefix for groups
        first_name = agent_name.split()[0] if agent_name else 'AI'
        preview = f"{first_name}: {content}"[:100]
        Conversation.objects.filter(pk=conversation.pk).update(
            last_message_preview=preview
        )

        # Update unread counts for all human participants
        ConversationParticipant.objects.filter(
            conversation=conversation,
            has_left=False,
            participant_type='human',
        ).update(
            unread_count=F('unread_count') + 1
        )

        # Broadcast via WebSocket
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'new_message', 'message_data': message_data}
        )

        # Stop typing indicator
        _safe_broadcast(
            f'chat_{conversation.id}',
            {
                'type': 'typing_indicator',
                'user_id': agent_user_id,
                'user_name': agent_name,
                'is_typing': False,
            }
        )

        # Trigger push notification for offline recipients
        from connect.tasks import send_dm_notification
        send_dm_notification.delay(str(message.id))

        logger.info(
            f"Agent {agent_name} posted message in conversation {conversation_id}"
        )

        return Response(message_data, status=201)


class InternalAgentTypingView(APIView):
    """
    Send typing indicator for an AI agent via channel layer.

    Called by shizuha-agent while processing a mention.

    Request body:
        {
            "agent_id": "uuid",
            "agent_user_id": int,
            "agent_name": "Kai",
            "is_typing": true
        }
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, conversation_id):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        agent_user_id = request.data.get('agent_user_id', 0)
        agent_name = request.data.get('agent_name', 'AI Agent')
        is_typing = request.data.get('is_typing', True)

        _safe_broadcast(
            f'chat_{conversation_id}',
            {
                'type': 'typing_indicator',
                'user_id': agent_user_id,
                'user_name': agent_name,
                'is_typing': is_typing,
            }
        )

        return Response({'status': 'ok'})


#: The real "System" user in shizuha-id. Created once via
#: `User.objects.create(username='system', email='system@shizuha.com')`.
#: Every service-originated message (Pulse reminders, HR notifications,
#: scheduler fires, etc.) flows FROM this user to the recipient — same
#: plumbing as any other human-to-human DM, no sentinel-ID hacks, no
#: special participant type. The username is cached at first use and
#: the caller never has to think about provisioning.
SYSTEM_USERNAME = 'system'
_system_user_id_cache: int | None = None


def _get_system_user_id() -> int:
    """
    Resolve the shizuha-id user_id of the System user.

    Cached for the lifetime of the process. If the user doesn't exist
    (fresh install), we raise — operators should provision it once:
        User.objects.create(username='system', email='system@shizuha.com')
    """
    global _system_user_id_cache
    if _system_user_id_cache is not None:
        return _system_user_id_cache

    # Look up via shizuha-id's internal API. Connect doesn't have direct
    # DB access to identity, so this is a one-shot HTTP call cached for
    # the process lifetime. Note: SHIZUHA_ID_API_URL already includes
    # the /api suffix — match the pattern used by `send_notification`.
    import requests
    from django.conf import settings
    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    try:
        resp = requests.get(
            f"{id_base}/internal/users/by-username/",
            params={'username': SYSTEM_USERNAME},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=5,
        )
        if resp.status_code == 200:
            _system_user_id_cache = int(resp.json()['id'])
            logger.info("Resolved System user_id=%s", _system_user_id_cache)
            return _system_user_id_cache
        logger.error("shizuha-id returned %s for system user lookup: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Failed to resolve System user via shizuha-id: %s", e)

    raise RuntimeError(
        "System user not provisioned. Create it in shizuha-id: "
        "User.objects.create(username='system', email='system@shizuha.com')"
    )


def _get_or_create_system_dm(recipient_user_id: int, request=None):
    """
    Find or create the dedicated System→recipient DM.

    A system DM is just a normal direct conversation with two real
    participants: the recipient and the System user (user_id =
    `_get_system_user_id()`). No participant-type sentinels, no magic
    user_id=0. This means every downstream path — WS group membership,
    conversation_created broadcasts, serializer output — works exactly
    like a regular DM.

    When the conversation is created (first-time case), we broadcast
    `conversation_created` to the recipient's `user_inbox_{id}` group
    so their already-connected WS consumer auto-subscribes to the new
    `chat_{conv_id}` group BEFORE we publish the message. Without this,
    the recipient silently misses the first system message (their WS
    was established before the conversation existed).

    Returns: (conversation, created: bool)
    """
    system_user_id = _get_system_user_id()

    # Use direct_key + get_or_create so two concurrent sends to the same
    # recipient can't both create new conversations (previously visible as
    # "2 chats with System" in the Kotlin app). The partial unique constraint
    # on (conversation_type='direct', direct_key) makes the race impossible
    # at the DB level — the second caller gets the existing row instead.
    direct_key = Conversation.build_direct_key(system_user_id, recipient_user_id)

    with transaction.atomic():
        conversation, created = Conversation.objects.get_or_create(
            conversation_type='direct',
            direct_key=direct_key,
            defaults={},
        )
        if not created:
            return conversation, False

        # First-time case — add both participants.
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=recipient_user_id,
            user_name=f'user_{recipient_user_id}',
            participant_type='human',
        )
        ConversationParticipant.objects.create(
            conversation=conversation,
            user_id=system_user_id,
            user_name='System',
            user_email='system@shizuha.com',
            participant_type='human',
        )

    # Broadcast conversation_created to the RECIPIENT's user_inbox group
    # so their WS consumer auto-subscribes to the new chat_<conv_id>
    # group before we publish new_message below. Without this, the very
    # first system message to a user silently disappears — the WS was
    # established before this conversation existed, so group_send to
    # chat_<conv_id> has zero subscribers.
    #
    # We do NOT broadcast to the System user's inbox — nobody is
    # connected as System (it's a bot identity, not a logged-in session).
    try:
        from connect.serializers.messaging import ConversationSerializer
        # Build a minimal serialization — can't pass `request` here since
        # we're called from an internal API without DRF request context
        # (for ConversationSerializer's participant-filtering).
        conv_payload = {
            'id': str(conversation.id),
            'conversation_type': 'direct',
            'created_at': conversation.created_at.isoformat() if conversation.created_at else None,
        }
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'user_inbox_{recipient_user_id}',
            {
                'type': 'conversation_created',
                'conversation_id': str(conversation.id),
                'conversation': conv_payload,
            },
        )
    except Exception:
        logger.exception(
            "Failed to broadcast conversation_created for new system DM "
            "(recipient=%s, conv=%s) — first message may be dropped",
            recipient_user_id, conversation.id,
        )

    return conversation, True


class InternalUnreadRecipientsView(APIView):
    """Fleet unread and last-inbound-DM state for Hive lifecycle decisions.

    Hive's on-demand reconciler polls this to wake STOPPED agents that have
    unread Connect messages — the reconciler backstop behind the event-driven
    connect-wake (HIVE-602), so a DM sent while hive had an agent shut down
    for compute savings is never stranded. PLAT-4490 also uses ``last_dm_at``
    to avoid stopping an agent immediately after it reads a DM and starts
    processing it. Read-only aggregate (no message content); gated by the
    internal-service header allowlist.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)
        from django.db.models import Count, F, Max, Sum

        from connect.models import (
            ConversationParticipant,
            DirectMessage,
            MessageDeletion,
            MessageDelivery,
        )

        # Perf (operator 2026-07-14): the previous shape computed last_dm_at via
        # a CORRELATED Subquery — one ordered DirectMessage scan PER recipient
        # row — measured 7.0s at 156k messages, brushing the reconciler's 10s
        # budget (a timeout fail-closes ALL hibernation stops). Two set queries
        # merged in Python return the identical payload in 0.13s (54x).
        rows = list(
            ConversationParticipant.objects
            .filter(
                conversation__conversation_type='direct',
                has_left=False,
            )
            .values('user_id', 'user_email')
            .annotate(
                unread_total=Sum('unread_count'),
                conversations=Count('id'),
                last_activity=Max('conversation__last_message_at'),
            )
        )
        # Latest INBOUND (not self-sent) direct DM per participant, as ONE
        # grouped aggregate over the messages↔participants join — identical
        # semantics to the old per-row subquery (direct conversations,
        # has_left=False, sender != the participant).
        latest_by_user = dict(
            DirectMessage.objects
            .filter(
                conversation__conversation_type='direct',
                conversation__participants__has_left=False,
            )
            .exclude(sender_id=F('conversation__participants__user_id'))
            .values('conversation__participants__user_id')
            .annotate(last_dm_at=Max('created_at'))
            .values_list('conversation__participants__user_id', 'last_dm_at')
        )

        # Lifecycle must follow delivery state, not the UI badge.  Participant
        # unread_count is intentionally user-managed and can remain non-zero
        # after an agent bridge has already processed a message.  Using that
        # badge as a keep-warm lock leaked idle pods indefinitely.  Compute the
        # set of messages an agent connection can actually replay: inbound,
        # undelivered, not deleted, newer than both the configured agent replay
        # floor and the participant's own read/clear cursor.
        try:
            replay_window_seconds = max(0, float(getattr(
                settings,
                'CONNECT_AGENT_DM_REPLAY_WINDOW_SECONDS',
                15 * 60,
            )))
        except (TypeError, ValueError):
            replay_window_seconds = 15 * 60
        replay_floor = timezone.now() - timedelta(seconds=replay_window_seconds)
        participant_states = list(
            ConversationParticipant.objects.filter(
                conversation__conversation_type='direct',
                has_left=False,
            ).values(
                'conversation_id', 'user_id', 'user_email',
                'last_read_at', 'cleared_at',
            )
        )
        conversation_ids = {row['conversation_id'] for row in participant_states}
        replay_messages = list(
            DirectMessage.objects.filter(
                conversation_id__in=conversation_ids,
                created_at__gt=replay_floor,
                is_deleted=False,
            ).values('id', 'conversation_id', 'sender_id', 'created_at')
        )
        messages_by_conversation = defaultdict(list)
        for message in replay_messages:
            messages_by_conversation[message['conversation_id']].append(message)
        message_ids = [message['id'] for message in replay_messages]
        delivered = set(MessageDelivery.objects.filter(
            message_id__in=message_ids,
        ).values_list('message_id', 'user_id'))
        deleted = set(MessageDeletion.objects.filter(
            message_id__in=message_ids,
        ).values_list('message_id', 'user_id'))
        pending_by_identity = defaultdict(int)
        for participant in participant_states:
            cutoff = max(
                value for value in (
                    replay_floor,
                    participant['last_read_at'],
                    participant['cleared_at'],
                ) if value is not None
            )
            user_id = participant['user_id']
            for message in messages_by_conversation[participant['conversation_id']]:
                delivery_key = (message['id'], user_id)
                if (
                    message['sender_id'] != user_id
                    and message['created_at'] > cutoff
                    and delivery_key not in delivered
                    and delivery_key not in deleted
                ):
                    pending_by_identity[(user_id, participant['user_email'])] += 1
        for row in rows:
            row['last_dm_at'] = latest_by_user.get(row['user_id'])
            row['pending_delivery_total'] = pending_by_identity.get(
                (row['user_id'], row['user_email']),
                0,
            )
        rows.sort(key=lambda r: (
            -(r['unread_total'] or 0),
            -(r['last_dm_at'].timestamp() if r['last_dm_at'] else float('-inf')),
            r['user_id'],
        ))
        # This response is the authoritative stop-safety snapshot. Truncating it
        # silently turns omitted recipients into unread=0/no-recent-DM in Hive,
        # which can stop an agent that is actively processing a message. Return
        # the complete aggregate and attest completeness explicitly; Hive rejects
        # missing/false completeness and suppresses every stop.
        return Response({
            'recipients': rows,
            'complete': True,
            'lifecycle_delivery_complete': True,
        })


class InternalSystemMessageView(APIView):
    """
    Send a system message to a user via Connect.

    Used by other services (Pulse, HR, scheduler, etc.) to inject task
    reminders, notifications, and triggers into a user's or agent's inbox.

    Each user has a DEDICATED system DM (conversation containing exactly
    the recipient + the System pseudo-participant with user_id=0). System
    messages are ALWAYS posted into that system DM — never into a regular
    human-to-human or human-to-agent conversation. This guarantees that
    automated notifications cannot leak into other users' chat threads
    even if the recipient happens to share many DMs with other people.

    Request body:
        {
            "recipient_user_id": int,       # Target user's shizuha-id user_id
            "content": "Your task SM-1 is due...",
            "sender_name": "Pulse Scheduler",
            "sender_service": "pulse",      # Source service for attribution
            "client_message_id": "stable delivery key"  # optional idempotency
        }
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        recipient_user_id = request.data.get('recipient_user_id')
        content = request.data.get('content', '')
        sender_name = request.data.get('sender_name', 'System')
        sender_service = request.data.get('sender_service', 'system')
        raw_client_message_id = request.data.get('client_message_id')
        # CON-429 S2 (design §2.1): the notification class travels on the
        # ingress payload and is validated against the server-side allowlist.
        # Unknown values are REJECTED (400), never coerced — a producer bug
        # cannot mint an arbitrary class string. Absent class stamps the
        # design default `informational` for system-channel posts (§2).
        raw_notification_class = request.data.get('notification_class')
        notification_class = raw_notification_class or 'informational'
        if notification_class not in VALID_NOTIFICATION_CLASSES:
            return Response(
                {
                    'error': 'invalid notification_class',
                    'detail': (
                        f'{raw_notification_class!r} is not in the allowlist '
                        f'(allowed: {sorted(VALID_NOTIFICATION_CLASSES)})'
                    ),
                },
                status=400,
            )

        if not recipient_user_id or not content:
            return Response(
                {'error': 'recipient_user_id and content are required'},
                status=400,
            )

        try:
            recipient_user_id = int(recipient_user_id)
        except (TypeError, ValueError):
            return Response(
                {'error': 'recipient_user_id must be an integer'},
                status=400,
            )

        if not _validate_system_origin_token(
            request,
            sender_service=sender_service,
            recipient_user_id=recipient_user_id,
            content=content,
        ):
            return Response({'error': 'Invalid system message origin token'}, status=403)

        system_user_id = _get_system_user_id()
        if recipient_user_id == system_user_id:
            return Response(
                {'error': 'Cannot send a system message to the System user'},
                status=400,
            )

        conversation, created = _get_or_create_system_dm(recipient_user_id)
        if created:
            logger.info(
                "Created dedicated system DM for user %s (conversation %s)",
                recipient_user_id, conversation.id,
            )

        client_message_id = _agent_message_client_uuid(
            raw_client_message_id,
            conversation_id=conversation.id,
            agent_user_id=system_user_id,
        )
        if client_message_id:
            existing = DirectMessage.objects.filter(
                conversation=conversation,
                client_message_id=client_message_id,
            ).first()
            if existing:
                if (
                    existing.sender_id != system_user_id
                    or existing.content != content
                    or existing.sender_name != sender_name
                    or (existing.media_metadata or {}).get('sender_service') != sender_service
                ):
                    return Response(
                        {'error': 'client_message_id was already used with a different system message'},
                        status=409,
                    )
                return Response(
                    {
                        'status': 'sent',
                        'conversation_id': str(conversation.id),
                        'message_id': str(existing.id),
                        'system_dm_created': False,
                        'idempotent_replay': True,
                    },
                    status=200,
                )

        # Create the system message — sender is the real System user,
        # sender_name/service carry the originating service tag so the
        # UI can still show "Pulse Scheduler" / "HR Bot" etc.
        try:
            with transaction.atomic():
                message = DirectMessage.objects.create(
                    conversation=conversation,
                    sender_id=system_user_id,
                    sender_email='system@shizuha.com',
                    sender_name=sender_name,
                    sender_username=SYSTEM_USERNAME,
                    message_type='text',
                    content=content,
                    client_message_id=client_message_id,
                    media_metadata={'sender_service': sender_service},
                    notification_class=notification_class,
                )
        except IntegrityError:
            # A concurrent retry may win the unique(conversation,
            # client_message_id) race. Re-read and apply the same replay versus
            # conflict contract instead of emitting a duplicate notification.
            existing = DirectMessage.objects.filter(
                conversation=conversation,
                client_message_id=client_message_id,
            ).first()
            if existing and (
                existing.sender_id == system_user_id
                and existing.content == content
                and existing.sender_name == sender_name
                and (existing.media_metadata or {}).get('sender_service') == sender_service
            ):
                return Response(
                    {
                        'status': 'sent',
                        'conversation_id': str(conversation.id),
                        'message_id': str(existing.id),
                        'system_dm_created': False,
                        'idempotent_replay': True,
                    },
                    status=200,
                )
            return Response(
                {'error': 'client_message_id was already used with a different system message'},
                status=409,
            )

        conversation.refresh_from_db()

        # Broadcast new_message to the chat group. By this point, if this
        # is a brand-new DM, _get_or_create_system_dm has already fired
        # conversation_created to the recipient's user_inbox — their WS
        # consumer will have auto-joined chat_<conv_id>, so this broadcast
        # reaches them.
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'new_message', 'message_data': message_data},
        )

        # PLAT-6502: system task-update DMs are automated notifications, not
        # human messages requiring attention — they must NOT inflate the
        # recipient's unread badge (observed: 2586 unread system DMs burying
        # real conversations). The System conversation stays reachable but its
        # unread_count is deliberately NOT bumped on delivery. Real human/agent
        # DMs still surface as unread (messaging.py delivery paths unchanged).

        logger.info(
            "System message sent to user %s via %s (conv=%s): %s",
            recipient_user_id, sender_service, conversation.id, content[:50],
        )

        return Response(
            {
                'status': 'sent',
                'conversation_id': str(conversation.id),
                'message_id': str(message.id),
                'system_dm_created': created,
            },
            status=201,
        )


class InternalGroupMessageView(APIView):
    """Post a service/automation message into an EXISTING group conversation.

    Unlike ``InternalSystemMessageView`` (which targets a per-user system DM by
    ``recipient_user_id``), this posts directly into a named group conversation
    by id — for team rituals like the weekly retro (PLAT-948) and the design-review
    thread (PLAT-967) that broadcast to a standing group rather than a single user.

    The message is attributed to the real System user (``user_id`` from
    ``_get_system_user_id()``); ``sender_name`` / ``sender_service`` carry the
    originating service tag for display ("Shizuha", "shizuha-pulse"). No origin
    token is required (there is no ``recipient_user_id`` to sign over) — access is
    gated by the ``X-Internal-Service`` allowlist + Docker network isolation, the
    same as the agent-message endpoint.

    Request body:
        {
            "conversation_id": "uuid",      # target GROUP conversation
            "content": "🧭 Design Review …",
            "sender_name": "Shizuha",       # display name in the group
            "sender_service": "shizuha-pulse"
        }
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        conversation_id = request.data.get('conversation_id')
        content = request.data.get('content', '')
        sender_name = request.data.get('sender_name', 'System')
        sender_service = request.data.get('sender_service', 'system')

        if not conversation_id or not content:
            return Response(
                {'error': 'conversation_id and content are required'},
                status=400,
            )

        try:
            conversation = Conversation.objects.get(pk=conversation_id)
        except (Conversation.DoesNotExist, ValueError, ValidationError):
            return Response({'error': 'Conversation not found'}, status=404)

        system_user_id = _get_system_user_id()

        message = DirectMessage.objects.create(
            conversation=conversation,
            sender_id=system_user_id,
            sender_email='system@shizuha.com',
            sender_name=sender_name,
            sender_username=SYSTEM_USERNAME,
            message_type='text',
            content=content,
        )

        conversation.refresh_from_db()

        # Last-message preview, attributed to the sender display name.
        first_name = sender_name.split()[0] if sender_name else 'System'
        Conversation.objects.filter(pk=conversation.pk).update(
            last_message_preview=f"{first_name}: {content}"[:100]
        )

        # Bump unread for every participant except the System sender.
        ConversationParticipant.objects.filter(
            conversation=conversation,
            has_left=False,
        ).exclude(user_id=system_user_id).update(
            unread_count=F('unread_count') + 1
        )

        # Broadcast to the live chat group so connected members see it instantly.
        message_data = DirectMessageSerializer(message).data
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'new_message', 'message_data': message_data},
        )

        # Push notification for offline participants (same path as agent-message).
        try:
            from connect.tasks import send_dm_notification
            send_dm_notification.delay(str(message.id))
        except Exception as exc:  # never fail the post on a push hiccup
            logger.warning("group-message push notify enqueue failed: %s", exc)

        logger.info(
            "Group message posted to conversation %s via %s: %s",
            conversation.id, sender_service, content[:50],
        )

        return Response(
            {
                'status': 'sent',
                'conversation_id': str(conversation.id),
                'message_id': str(message.id),
            },
            status=201,
        )


class AgentTelemetryView(APIView):
    """Internal: latest runtime telemetry per agent — model, context% used,
    tokens/sec, turns, activity state, and health (error counts + recent errors),
    reported by agent bridges over the Connect WS (``agent.telemetry``). Read-only;
    Hive reads this to render live fleet metrics and surface issues proactively.
    Network-internal (AllowAny), like the other internal endpoints here."""

    permission_classes = [AllowAny]

    def get(self, request):
        # Lazy import: consumers.py pulls in channels machinery; importing it at
        # module load would risk a circular import through the app's url/view graph.
        from connect.consumers import _read_all_agent_telemetry
        username = request.query_params.get('username')
        data = _read_all_agent_telemetry()
        if username:
            snap = data.get(username)
            return Response({'agent': snap} if snap else {'agent': None}, status=200)
        return Response({'agents': data, 'count': len(data)})


class InternalAgentConfigView(APIView):
    """Push a live config update to a connected agent bridge over its WS.

    HIVE-215 fix: broadcasts `agent_config_update` to `user_inbox_<id>` so
    `UserChatConsumer` (which agents connect to via /ws/connect/agent/) forwards
    the payload over the WS to the bridge process.

    Request body:
        {
            "agent_user_id": int,        # Agent's shizuha-id numeric user_id
            "config": {"contextPrompt": "...", ...}
        }

    Returns:
        {"ok": true, "agent_user_id": N, "keys": [...]}
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        agent_user_id = request.data.get('agent_user_id')
        config = request.data.get('config') or {}

        if not agent_user_id:
            return Response({'error': 'agent_user_id is required'}, status=400)
        try:
            agent_user_id = int(agent_user_id)
        except (TypeError, ValueError):
            return Response({'error': 'agent_user_id must be an integer'}, status=400)

        if not isinstance(config, dict):
            return Response({'error': 'config must be an object'}, status=400)

        keys = list(config.keys())
        _safe_broadcast(
            f'user_inbox_{agent_user_id}',
            {
                'type': 'agent_config_update',
                'config': config,
            },
        )
        logger.info(
            'agent config push: agent_user_id=%s keys=%s',
            agent_user_id, keys,
        )
        return Response({'ok': True, 'agent_user_id': agent_user_id, 'keys': keys})


class InternalAgentAuthRejectTriggerView(APIView):
    """Scoped live trigger for exercising agent WS auth-reject recovery.

    Internal-only endpoint used by PLAT-3988/PLAT-3146 verification. It closes
    exactly the selected agent user's currently-connected UserChatConsumer
    sockets with an auth-class WebSocket close code so the runtime can prove:
    close -> forceTokenRefresh -> broker /token -> reconnect. This does not
    revoke credentials, rotate tokens, or touch other fleet agents.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)

        user_id = request.data.get('user_id')
        username = str(request.data.get('username') or '').strip()
        if not user_id and not username:
            return Response({'error': 'user_id or username is required'}, status=400)
        from connect.services.qa_probe import is_qa_fixture_identity
        if not is_qa_fixture_identity(username=username):
            return Response(
                {
                    'error': (
                        'auth-reject tests cannot target production fleet agents; '
                        'use a *-qa / qafixture seat'
                    )
                },
                status=403,
            )

        try:
            user_id_int = int(user_id) if user_id not in (None, '') else None
        except (TypeError, ValueError):
            return Response({'error': 'user_id must be an integer'}, status=400)

        try:
            code = int(request.data.get('code') or 4401)
        except (TypeError, ValueError):
            return Response({'error': 'code must be an integer'}, status=400)
        if code not in _ALLOWED_AUTH_REJECT_CODES:
            return Response({'error': 'code must be one of 4401, 4403, 1008'}, status=400)

        reason = str(request.data.get('reason') or '').strip()
        if len(reason) < 8:
            return Response({'error': 'reason must describe the verification purpose'}, status=400)

        sessions = list(_active_agent_presence_sessions(
            user_id=user_id_int,
            username='' if user_id_int is not None else username,
        )[:5])
        if not sessions:
            return Response({'error': 'no active agent websocket sessions found'}, status=404)

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return Response({'error': 'channel layer unavailable'}, status=503)

        delivered = []
        for session in sessions:
            event = {
                'type': 'force_auth_reject',
                'code': code,
                'reason': reason[:200],
                'trigger': 'internal-agent-auth-reject-test',
                'target_user_id': session.user_id,
                'target_session_id': str(session.id),
            }
            try:
                async_to_sync(channel_layer.send)(session.channel_name, event)
                delivered.append({
                    'session_id': str(session.id),
                    'channel_name': session.channel_name,
                    'user_id': session.user_id,
                    'status': session.status,
                })
            except Exception:
                logger.exception(
                    'Failed to deliver auth-reject test event to channel %s for user %s',
                    session.channel_name, session.user_id,
                )

        if not delivered:
            return Response({'error': 'failed to deliver trigger to any session'}, status=503)

        logger.warning(
            'Internal agent auth-reject test triggered: user_id=%s username=%s code=%s sessions=%s reason=%s',
            user_id_int, username, code, [d['session_id'] for d in delivered], reason,
        )
        return Response({'triggered': len(delivered), 'code': code, 'sessions': delivered})


class InternalAgentConnectionView(APIView):
    """POST /api/internal/agent-connection/ — HIVE-620/624.

    Idempotently ensure an ACCEPTED Connection + a direct DM conversation between
    an owner and their PERSONAL agent, so the agent shows in the owner's sidebar
    by default (no connect request needed) and is DM-wakeable while offline.
    Internal-service authenticated; identities supplied by the trusted caller.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _validate_internal_service(request):
            return Response({'error': 'Forbidden'}, status=403)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            owner_id = int(data.get('owner_user_id'))
            agent_id = int(data.get('agent_user_id'))
        except (TypeError, ValueError):
            return Response({'error': 'owner_user_id and agent_user_id (int) are required'}, status=400)
        if owner_id == agent_id:
            return Response({'error': 'owner and agent must differ'}, status=400)
        agent_name = (str(data.get('agent_name') or 'Shizuha').strip() or 'Shizuha')
        agent_email = str(data.get('agent_email') or '').strip()
        owner_email = str(data.get('owner_email') or '').strip()
        owner_name = str(data.get('owner_name') or '').strip()

        from django.db import transaction
        from connect.models import Connection, Conversation, ConversationParticipant

        with transaction.atomic():
            low, high = sorted([owner_id, agent_id])
            conn, _ = Connection.objects.get_or_create(
                user_a_id=low, user_b_id=high,
                defaults={'status': 'accepted', 'requester_id': owner_id},
            )
            if conn.status != 'accepted':
                conn.status = 'accepted'
                conn.save(update_fields=['status'])

            direct_key = Conversation.build_direct_key(owner_id, agent_id)
            conv, created = Conversation.objects.get_or_create(
                conversation_type='direct', direct_key=direct_key,
                defaults={'created_by_id': owner_id},
            )
            if created:
                ConversationParticipant.objects.create(
                    conversation=conv, user_id=owner_id, user_email=owner_email,
                    user_name=owner_name or owner_email or f'user-{owner_id}',
                    is_admin=True, participant_type='human',
                )
                ConversationParticipant.objects.create(
                    conversation=conv, user_id=agent_id, user_email=agent_email,
                    user_name=agent_name, participant_type='agent',
                    agent_role='Personal assistant',
                )
            else:
                # Keep the agent participant's display name fresh (e.g. "Shizuha").
                ConversationParticipant.objects.filter(
                    conversation=conv, user_id=agent_id).update(user_name=agent_name)

        return Response({
            'status': 'ok', 'connection': conn.status,
            'conversation_id': str(conv.id), 'conversation_created': created,
        }, status=200)

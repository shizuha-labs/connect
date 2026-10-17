"""External channel bridge envelope, signature, and idempotency helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction
from django.utils import timezone

from connect.models import (
    BridgeChannelPolicy,
    BridgeIdentityMapping,
    BridgeInboundEvent,
    BridgeOutboundEvent,
    BridgeWebhookSecret,
    Conversation,
    ConversationParticipant,
    DirectMessage,
)
from connect.serializers.messaging import DirectMessageSerializer
from connect.services.privacy import (
    actor_can_reach_recipient,
    conversation_has_any_block,
)
from connect.views.messaging import _safe_broadcast
from connect.metrics import (
    record_inbound_message,
    record_message_persistence_failure,
    record_provider_event,
)
from connect.services.backpressure import (
    build_retry_metadata,
    outbound_queue_state,
    ProviderRateLimited,
)
from connect.services.replay import outbound_duplicate_storm_decision

logger = logging.getLogger(__name__)


class BridgeError(Exception):
    status_code = 400


class MissingWebhookSecret(BridgeError):
    status_code = 404


class InvalidSignature(BridgeError):
    status_code = 400


class BridgeConflict(BridgeError):
    status_code = 409


@dataclass(frozen=True)
class BridgeProcessResult:
    event: BridgeInboundEvent
    created: bool = False
    idempotent_replay: bool = False
    conflict: bool = False


@dataclass(frozen=True)
class BridgeOutboundEnvelope:
    provider: str
    provider_account_id: str
    channel_key: str
    task_key: str
    agent_id: int
    recipient_external_id: str
    body: str
    idempotency_key: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class BridgeOutboundResult:
    event: BridgeOutboundEvent
    created: bool = False
    idempotent_replay: bool = False
    conflict: bool = False
    sent_to_provider: bool = False


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def body_hash(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def outbound_request_hash(envelope: BridgeOutboundEnvelope) -> str:
    return payload_hash({
        "provider": envelope.provider,
        "provider_account_id": envelope.provider_account_id,
        "channel_key": envelope.channel_key,
        "task_key": envelope.task_key,
        "agent_id": envelope.agent_id,
        "recipient_external_id": envelope.recipient_external_id,
        "body_hash": body_hash(envelope.body),
        "conversation_id": envelope.conversation_id or "",
    })


def _fernet() -> Fernet:
    configured = getattr(settings, "CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY", "") or ""
    if configured:
        key = configured.encode("utf-8")
    else:
        if not settings.DEBUG:
            raise ImproperlyConfigured(
                "CONNECT_BRIDGE_SECRET_ENCRYPTION_KEY must be set in production. "
                "Falling back to SECRET_KEY ties webhook secret rotation to Django's "
                "session/CSRF key and increases blast radius on SECRET_KEY exposure."
            )
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_provider_secret(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret must be a non-empty string")
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_provider_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise MissingWebhookSecret("webhook secret cannot be decrypted") from exc


def upsert_webhook_secret(provider: str, provider_account_id: str, secret: str, *, key_id: str = "default") -> BridgeWebhookSecret:
    encrypted = encrypt_provider_secret(secret)
    obj, _ = BridgeWebhookSecret.objects.update_or_create(
        provider=provider,
        provider_account_id=provider_account_id,
        defaults={"secret_ciphertext": encrypted, "key_id": key_id, "is_active": True},
    )
    return obj


def _signature_header(headers: Mapping[str, str]) -> str:
    return (
        headers.get("X-Hub-Signature-256")
        or headers.get("X-WhatsApp-Signature-256")
        or headers.get("HTTP_X_HUB_SIGNATURE_256")
        or headers.get("HTTP_X_WHATSAPP_SIGNATURE_256")
        or ""
    )


def verify_signature(raw_body: bytes, provided_signature: str, secret: str) -> bool:
    if not provided_signature:
        return False
    if provided_signature.startswith("sha256="):
        provided_signature = provided_signature.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_signature, expected)


def _load_secret(provider: str, provider_account_id: str) -> str:
    secret = BridgeWebhookSecret.objects.filter(
        provider=provider,
        provider_account_id=provider_account_id,
        is_active=True,
    ).first()
    if not secret:
        raise MissingWebhookSecret("no active webhook secret for provider account")
    return decrypt_provider_secret(secret.secret_ciphertext)


def verify_request_or_raise(provider: str, provider_account_id: str, raw_body: bytes, headers: Mapping[str, str]) -> None:
    secret = _load_secret(provider, provider_account_id)
    if not verify_signature(raw_body, _signature_header(headers), secret):
        # Deliberately do not log the signature, secret, or payload body.
        logger.warning("Rejected %s webhook for provider_account_id=%s: invalid signature", provider, provider_account_id)
        raise InvalidSignature("invalid webhook signature")


def _first_message(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        return payload["entry"][0]["changes"][0]["value"].get("messages", [{}])[0]
    except (KeyError, IndexError, TypeError, AttributeError):
        messages = payload.get("messages") if isinstance(payload, Mapping) else None
        if isinstance(messages, list) and messages:
            return messages[0] or {}
        return payload


def _extract_whatsapp_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    msg = _first_message(payload)
    provider_event_id = str(
        msg.get("id")
        or payload.get("provider_event_id")
        or payload.get("message_id")
        or payload.get("id")
        or ""
    ).strip()
    external_sender_id = str(
        msg.get("from")
        or payload.get("external_sender_id")
        or payload.get("from")
        or ""
    ).strip()
    text = ""
    if isinstance(msg.get("text"), Mapping):
        text = str(msg["text"].get("body") or "")
    elif msg.get("body"):
        text = str(msg.get("body"))
    elif payload.get("text"):
        text = str(payload.get("text"))
    return {
        "provider_event_id": provider_event_id,
        "external_sender_id": external_sender_id,
        "content_text": text,
        "timestamp": str(msg.get("timestamp") or payload.get("timestamp") or ""),
    }


def _is_admin_command(content: str) -> bool:
    stripped = (content or "").strip().lower()
    return stripped.startswith("/admin") or stripped.startswith("!admin") or stripped.startswith("admin:")


def _identity_is_fresh(mapping: BridgeIdentityMapping) -> bool:
    hours = int(getattr(settings, "CONNECT_BRIDGE_ADMIN_IDENTITY_FRESHNESS_HOURS", 24))
    return bool(mapping.verified_at and mapping.verified_at >= timezone.now() - timedelta(hours=hours))


def _route_message(event: BridgeInboundEvent, mapping: BridgeIdentityMapping) -> DirectMessage:
    direct_key = Conversation.build_direct_key(mapping.mapped_user_id, 0)
    with transaction.atomic():
        conversation, created = Conversation.objects.get_or_create(
            conversation_type="direct",
            direct_key=direct_key,
            defaults={"created_by_id": mapping.mapped_user_id},
        )
        if created:
            ConversationParticipant.objects.create(
                conversation=conversation,
                user_id=mapping.mapped_user_id,
                user_email=mapping.mapped_user_email,
                user_name=mapping.mapped_user_name,
                participant_type="human",
                is_admin=True,
            )
            ConversationParticipant.objects.create(
                conversation=conversation,
                user_id=0,
                user_email="bridge@shizuha.system",
                user_name="External Bridge",
                participant_type="system",
            )

        try:
            message = DirectMessage.objects.create(
                conversation=conversation,
                sender_id=mapping.mapped_user_id,
                sender_email=mapping.mapped_user_email,
                sender_name=mapping.mapped_user_name,
                sender_username="",
                message_type="text",
                content=event.content_text,
                media_metadata={
                    "bridge_event_id": str(event.id),
                    "provider": event.provider,
                    "provider_account_id": event.provider_account_id,
                    "provider_event_id": event.provider_event_id,
                },
            )
        except Exception:
            raise
        ConversationParticipant.objects.filter(
            conversation=conversation,
            user_id=0,
            has_left=False,
        ).update(unread_count=models.F("unread_count") + 1)

    event.message = message
    event.processing_status = BridgeInboundEvent.Status.ROUTED
    event.save(update_fields=["message", "processing_status", "updated_at"])

    try:
        _safe_broadcast(
            f"chat_{conversation.id}",
            {"type": "new_message", "message_data": DirectMessageSerializer(message).data},
        )
    except Exception:
        logger.exception("Failed to broadcast bridged message %s", message.id)
    return message


def _outbound_policy_for(envelope: BridgeOutboundEnvelope) -> BridgeChannelPolicy | None:
    policy = BridgeChannelPolicy.objects.filter(
        provider=envelope.provider,
        provider_account_id=envelope.provider_account_id,
        channel_key=envelope.channel_key,
        task_key=envelope.task_key,
        agent_id=envelope.agent_id,
        is_active=True,
    ).first()
    if not policy or not policy.admin_approved_at:
        return None
    allowed = policy.allowed_recipients or []
    if allowed and envelope.recipient_external_id not in {str(item) for item in allowed}:
        return None
    return policy



def _mapped_user_id_for_external(provider: str, provider_account_id: str, external_sender_id: str) -> int | None:
    mapping = BridgeIdentityMapping.objects.filter(
        provider=provider,
        provider_account_id=provider_account_id,
        external_sender_id=external_sender_id,
        is_active=True,
    ).first()
    return mapping.mapped_user_id if mapping else None


def _external_ids_for_same_canonical(envelope: BridgeOutboundEnvelope) -> list[str]:
    mapped_user_id = _mapped_user_id_for_external(
        envelope.provider,
        envelope.provider_account_id,
        envelope.recipient_external_id,
    )
    if mapped_user_id is None:
        return [envelope.recipient_external_id]
    ids = list(BridgeIdentityMapping.objects.filter(
        provider=envelope.provider,
        provider_account_id=envelope.provider_account_id,
        mapped_user_id=mapped_user_id,
        is_active=True,
    ).values_list("external_sender_id", flat=True))
    return ids or [envelope.recipient_external_id]


def _bridge_privacy_error(envelope: BridgeOutboundEnvelope) -> str:
    if not envelope.conversation_id:
        return ""
    mapped_user_id = _mapped_user_id_for_external(
        envelope.provider,
        envelope.provider_account_id,
        envelope.recipient_external_id,
    )
    if mapped_user_id is None:
        # CON-16 C2: if a bridge identity is unresolved, default-deny when the
        # referenced conversation contains any block relationship.
        return "unresolved_recipient_in_blocked_conversation" if conversation_has_any_block(envelope.conversation_id) else ""
    participant_ids = list(ConversationParticipant.objects.filter(
        conversation_id=envelope.conversation_id,
        has_left=False,
    ).values_list("user_id", flat=True))
    if any(not actor_can_reach_recipient(mapped_user_id, uid) for uid in participant_ids if uid != mapped_user_id):
        return "blocked_bridge_recipient"
    return ""

def _outbound_rate_limits() -> dict[str, int]:
    minimums = {
        "agent": 30,
        "recipient": 10,
        "provider_account": 100,
        "window_seconds": 60,
    }
    configured = getattr(settings, "CONNECT_BRIDGE_OUTBOUND_RATE_LIMITS", {}) or {}
    return {
        key: max(int(configured.get(key, value)), value)
        for key, value in minimums.items()
    }


def _rate_limit_status(envelope: BridgeOutboundEnvelope) -> str:
    limits = _outbound_rate_limits()
    since = timezone.now() - timedelta(seconds=limits["window_seconds"])
    counted_statuses = [
        BridgeOutboundEvent.Status.PENDING,
        BridgeOutboundEvent.Status.ACCEPTED,
        BridgeOutboundEvent.Status.DELIVERED,
    ]
    base = BridgeOutboundEvent.objects.filter(created_at__gte=since, status__in=counted_statuses)
    if base.filter(agent_id=envelope.agent_id).count() >= limits["agent"]:
        return "agent"
    if base.filter(recipient_external_id__in=_external_ids_for_same_canonical(envelope)).count() >= limits["recipient"]:
        return "recipient"
    if base.filter(
        provider=envelope.provider,
        provider_account_id=envelope.provider_account_id,
    ).count() >= limits["provider_account"]:
        return "provider_account"
    return ""


def _create_outbound_audit_event(
    envelope: BridgeOutboundEnvelope,
    *,
    request_digest: str,
    status_value: str,
    error_code: str = "",
    error_message: str = "",
) -> BridgeOutboundEvent:
    return BridgeOutboundEvent.objects.create(
        provider=envelope.provider,
        provider_account_id=envelope.provider_account_id,
        channel_key=envelope.channel_key,
        task_key=envelope.task_key,
        agent_id=envelope.agent_id,
        recipient_external_id=envelope.recipient_external_id,
        conversation_id=envelope.conversation_id or None,
        idempotency_key=envelope.idempotency_key,
        request_hash=request_digest,
        body_hash=body_hash(envelope.body),
        status=status_value,
        error_code=error_code,
        error_message=error_message,
        audit_metadata={
            "body_redacted": True,
            "provider_delivery_id_recorded": False,
        },
    )


def _default_provider_send(event: BridgeOutboundEvent, envelope: BridgeOutboundEnvelope) -> Mapping[str, str]:
    """Deterministic provider stub; real adapters pass an explicit sender."""
    delivery_seed = f"{event.provider}:{event.provider_account_id}:{event.idempotency_key}"
    return {"provider_delivery_id": hashlib.sha256(delivery_seed.encode("utf-8")).hexdigest()[:32]}


def send_outbound_bridge_message(
    envelope: BridgeOutboundEnvelope,
    *,
    provider_send=None,
) -> BridgeOutboundResult:
    """Authorize, rate-limit, audit, and idempotently send an outbound bridge envelope."""
    if envelope.provider != BridgeOutboundEvent.Provider.WHATSAPP:
        raise BridgeError("unsupported bridge provider")
    if not envelope.idempotency_key or not envelope.task_key or not envelope.channel_key:
        raise BridgeError("idempotency_key, task_key, and channel_key are required")

    request_digest = outbound_request_hash(envelope)
    with transaction.atomic():
        existing = BridgeOutboundEvent.objects.select_for_update().filter(
            provider=envelope.provider,
            provider_account_id=envelope.provider_account_id,
            idempotency_key=envelope.idempotency_key,
        ).first()
        if existing:
            if existing.request_hash != request_digest:
                existing.status = BridgeOutboundEvent.Status.CONFLICT
                existing.error_code = "idempotency_conflict"
                existing.error_message = "idempotency key reused with different outbound envelope"
                existing.save(update_fields=["status", "error_code", "error_message", "updated_at"])
                record_provider_event(envelope.provider, envelope.channel_key, 'idempotency_conflict', 'failure')
                return BridgeOutboundResult(existing, conflict=True)
            record_provider_event(envelope.provider, envelope.channel_key, 'idempotent_replay', 'success')
            return BridgeOutboundResult(existing, idempotent_replay=True)

        if not _outbound_policy_for(envelope):
            event = _create_outbound_audit_event(
                envelope,
                request_digest=request_digest,
                status_value=BridgeOutboundEvent.Status.FORBIDDEN,
                error_code="policy_required",
                error_message="explicit task/channel policy with admin approval is required",
            )
            record_provider_event(envelope.provider, envelope.channel_key, 'policy_denied', 'failure')
            return BridgeOutboundResult(event, created=True)

        privacy_error = _bridge_privacy_error(envelope)
        if privacy_error:
            event = _create_outbound_audit_event(
                envelope,
                request_digest=request_digest,
                status_value=BridgeOutboundEvent.Status.FORBIDDEN,
                error_code=privacy_error,
                error_message="bridge recipient blocked by conversation privacy policy",
            )
            record_provider_event(envelope.provider, envelope.channel_key, 'privacy_denied', 'failure')
            return BridgeOutboundResult(event, created=True)

        duplicate_decision = outbound_duplicate_storm_decision(envelope, body_hash(envelope.body))
        if duplicate_decision.suppressed:
            event = _create_outbound_audit_event(
                envelope,
                request_digest=request_digest,
                status_value=BridgeOutboundEvent.Status.RATE_LIMITED,
                error_code='duplicate_storm_suppressed',
                error_message='duplicate outbound payload storm suppressed',
            )
            event.audit_metadata = {
                **(event.audit_metadata or {}),
                **duplicate_decision.evidence,
                'delivery_state': 'suppressed_duplicate_storm',
            }
            event.save(update_fields=['audit_metadata', 'updated_at'])
            record_provider_event(envelope.provider, envelope.channel_key, 'duplicate_storm', 'suppressed')
            return BridgeOutboundResult(event, created=True)

        limited_dimension = _rate_limit_status(envelope)
        if limited_dimension:
            event = _create_outbound_audit_event(
                envelope,
                request_digest=request_digest,
                status_value=BridgeOutboundEvent.Status.RATE_LIMITED,
                error_code=f"rate_limit_{limited_dimension}",
                error_message=f"outbound bridge {limited_dimension} rate limit exceeded",
            )
            record_provider_event(envelope.provider, envelope.channel_key, 'rate_limited', 'failure')
            return BridgeOutboundResult(event, created=True)

        queue_state = outbound_queue_state(envelope.provider, envelope.channel_key)
        if queue_state.overloaded:
            event = _create_outbound_audit_event(
                envelope,
                request_digest=request_digest,
                status_value=BridgeOutboundEvent.Status.RETRYING,
                error_code=f"outbound_queue_{queue_state.reason}",
                error_message="outbound bridge queue backpressure active; retry later",
            )
            status_value, audit_metadata = build_retry_metadata(
                event,
                ProviderRateLimited("outbound queue backpressure"),
                queue_state=queue_state,
            )
            event.status = status_value
            event.audit_metadata = audit_metadata
            event.save(update_fields=["status", "audit_metadata", "updated_at"])
            record_provider_event(envelope.provider, envelope.channel_key, 'queue_backpressure', 'retrying')
            return BridgeOutboundResult(event, created=True)

        event = _create_outbound_audit_event(
            envelope,
            request_digest=request_digest,
            status_value=BridgeOutboundEvent.Status.PENDING,
        )

    sender = provider_send or _default_provider_send
    try:
        provider_result = sender(event, envelope) or {}
        provider_delivery_id = str(provider_result.get("provider_delivery_id") or "")
        event.provider_delivery_id = provider_delivery_id
        event.status = BridgeOutboundEvent.Status.ACCEPTED
        event.audit_metadata = {
            **(event.audit_metadata or {}),
            "provider_delivery_id_recorded": bool(provider_delivery_id),
        }
        event.save(update_fields=["provider_delivery_id", "status", "audit_metadata", "updated_at"])
        record_provider_event(envelope.provider, envelope.channel_key, 'provider_send', 'accepted')
        return BridgeOutboundResult(event, created=True, sent_to_provider=True)
    except Exception as exc:
        logger.exception("Outbound bridge provider send failed for event %s", event.id)
        status_value, audit_metadata = build_retry_metadata(event, exc)
        event.status = status_value
        event.error_code = "provider_rate_limited" if getattr(exc, 'status_code', None) == 429 else "provider_send_retrying"
        if status_value == BridgeOutboundEvent.Status.DEAD_LETTERED:
            event.error_code = "provider_send_dead_lettered"
        event.error_message = str(exc)[:240]
        event.audit_metadata = audit_metadata
        event.save(update_fields=["status", "error_code", "error_message", "audit_metadata", "updated_at"])
        record_provider_event(envelope.provider, envelope.channel_key, 'provider_send', 'failure')
        return BridgeOutboundResult(event, created=True)


def record_outbound_delivery_status(
    *,
    provider: str,
    provider_account_id: str,
    provider_delivery_id: str,
    status_value: str,
    payload: Mapping[str, Any] | None = None,
) -> BridgeOutboundEvent:
    event = BridgeOutboundEvent.objects.get(
        provider=provider,
        provider_account_id=provider_account_id,
        provider_delivery_id=provider_delivery_id,
    )
    event.status = status_value
    event.audit_metadata = {
        **(event.audit_metadata or {}),
        "callback_payload_hash": payload_hash(payload or {}),
        "callback_body_redacted": True,
    }
    event.save(update_fields=["status", "audit_metadata", "updated_at"])
    record_provider_event(provider, event.channel_key, 'delivery_status', status_value)
    return event


def process_inbound_webhook(provider: str, provider_account_id: str, raw_body: bytes, headers: Mapping[str, str]) -> BridgeProcessResult:
    # CON-5 invariant: verify signature before store, idempotency, or routing.
    channel = provider
    try:
        verify_request_or_raise(provider, provider_account_id, raw_body, headers)
    except InvalidSignature:
        record_inbound_message(provider, channel, 'auth_failure')
        record_provider_event(provider, channel, 'auth_failure', 'failure')
        raise
    except MissingWebhookSecret:
        record_inbound_message(provider, channel, 'auth_missing_secret')
        record_provider_event(provider, channel, 'auth_failure', 'missing_secret')
        raise
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_inbound_message(provider, channel, 'invalid_json')
        raise BridgeError("invalid JSON payload") from exc
    if not isinstance(payload, Mapping):
        record_inbound_message(provider, channel, 'invalid_payload')
        raise BridgeError("webhook payload must be an object")

    if provider != BridgeInboundEvent.Provider.WHATSAPP:
        record_inbound_message(provider, channel, 'unsupported_provider')
        raise BridgeError("unsupported bridge provider")
    extracted = _extract_whatsapp_event(payload)
    if not extracted["provider_event_id"] or not extracted["external_sender_id"]:
        record_inbound_message(provider, channel, 'invalid_payload')
        raise BridgeError("missing stable provider event id or sender id")

    digest = payload_hash(payload)
    with transaction.atomic():
        existing = BridgeInboundEvent.objects.select_for_update().filter(
            provider=provider,
            provider_account_id=provider_account_id,
            provider_event_id=extracted["provider_event_id"],
        ).first()
        if existing:
            if existing.payload_hash != digest:
                existing.processing_status = BridgeInboundEvent.Status.CONFLICT
                existing.signature_status = BridgeInboundEvent.SignatureStatus.CONFLICT
                existing.save(update_fields=["processing_status", "signature_status", "updated_at"])
                logger.warning(
                    "Bridge payload conflict provider=%s account=%s event_id=%s",
                    provider,
                    provider_account_id,
                    extracted["provider_event_id"],
                )
                record_inbound_message(provider, channel, 'conflict')
                record_provider_event(provider, channel, 'payload_conflict', 'failure')
                return BridgeProcessResult(existing, conflict=True)
            existing.processing_status = BridgeInboundEvent.Status.REPLAYED if existing.message_id else existing.processing_status
            existing.save(update_fields=["processing_status", "updated_at"])
            record_inbound_message(provider, channel, 'replayed')
            return BridgeProcessResult(existing, idempotent_replay=True)

        mapping = BridgeIdentityMapping.objects.filter(
            provider=provider,
            provider_account_id=provider_account_id,
            external_sender_id=extracted["external_sender_id"],
            is_active=True,
        ).first()
        event = BridgeInboundEvent.objects.create(
            provider=provider,
            provider_account_id=provider_account_id,
            provider_event_id=extracted["provider_event_id"],
            bridge_event_id=uuid.uuid4(),
            external_sender_id=extracted["external_sender_id"],
            mapped_user_id=mapping.mapped_user_id if mapping else None,
            identity_verified_at=mapping.verified_at if mapping else None,
            conversation_key=f"{provider}:{provider_account_id}:{extracted['external_sender_id']}",
            direction=BridgeInboundEvent.Direction.INBOUND,
            content_text=extracted["content_text"],
            payload=payload,
            payload_hash=digest,
            signature_status=BridgeInboundEvent.SignatureStatus.VALID,
            processing_status=BridgeInboundEvent.Status.RECEIVED,
        )

    if not mapping:
        event.processing_status = BridgeInboundEvent.Status.UNMAPPED
        event.save(update_fields=["processing_status", "updated_at"])
        record_inbound_message(provider, channel, 'unmapped')
        return BridgeProcessResult(event, created=True)
    if _is_admin_command(event.content_text) and (not mapping.is_admin_capable or not _identity_is_fresh(mapping)):
        event.processing_status = BridgeInboundEvent.Status.FORBIDDEN
        event.save(update_fields=["processing_status", "updated_at"])
        record_inbound_message(provider, channel, 'forbidden')
        record_provider_event(provider, channel, 'auth_failure', 'forbidden')
        return BridgeProcessResult(event, created=True)
    try:
        _route_message(event, mapping)
    except Exception as exc:
        record_inbound_message(provider, channel, 'persistence_error')
        record_message_persistence_failure(provider, channel, exc)
        raise
    record_inbound_message(provider, channel, 'routed')
    return BridgeProcessResult(event, created=True)

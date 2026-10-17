"""Postgres transactional outbox for authenticated boundary-security events."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from connect.models import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    SecurityEventOutbox,
    SecurityEventRelayHealth,
    SecurityEventRepeatClaim,
)

logger = logging.getLogger(__name__)

SECURITY_DM_NAMESPACE = uuid.UUID('1c5df799-a096-41d8-a5d7-23f480dc0f31')


def canonical_event_hash(event: dict) -> str:
    # ``received_at`` is assigned by Connect after producer authentication. It
    # deliberately remains in the forensic payload, but it cannot participate
    # in producer-request identity: an otherwise identical producer retry is
    # received at a different instant and must still be an idempotent replay.
    canonical_event = dict(event)
    canonical_event.pop('received_at', None)
    canonical = json.dumps(
        canonical_event,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def semantic_fingerprint(event: dict) -> str:
    raw = '|'.join(str(event.get(key) or '') for key in (
        'event_type', 'service', 'operation', 'reason',
        'actor_user_id', 'target_org_id',
    ))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _safe_publish_after_commit(outbox_id: str) -> None:
    try:
        publish_security_event(outbox_id)
    except Exception:
        # Acceptance already committed. The periodic Postgres relay remains the
        # recovery path even if this best-effort fast wake itself loses DB access.
        logger.exception('CON-217 post-commit fast wake failed id=%s', outbox_id)


def accept_security_event(*, producer_service: str, event: dict):
    """Commit the sanitized event and return ``(row, duplicate, conflict)``."""
    event_id = str(event['event_id'])
    request_hash = canonical_event_hash(event)
    defaults = {
        'schema_version': '1.0',
        'event_type': str(event.get('event_type') or 'unknown')[:128],
        'payload': event,
        'request_hash': request_hash,
        'semantic_fingerprint': semantic_fingerprint(event),
        'next_dispatch_at': timezone.now(),
    }
    with transaction.atomic():
        # Django's get_or_create handles the unique-constraint race in its own
        # savepoint. Keeping that handling inside the ORM avoids querying from
        # a transaction already marked broken by an escaped IntegrityError.
        row, created = SecurityEventOutbox.objects.get_or_create(
            producer_service=producer_service,
            producer_event_id=event_id,
            defaults=defaults,
        )

        if not created and row.request_hash != request_hash:
            SecurityEventOutbox.objects.filter(pk=row.pk).update(
                conflict_count=F('conflict_count') + 1,
                last_error_class='IdentityPayloadConflict',
            )
            return row, False, True

        if created:
            transaction.on_commit(lambda: _safe_publish_after_commit(str(row.pk)))
        return row, not created, False


def _bounded_error(exc: Exception) -> str:
    return exc.__class__.__name__[:64]


def _backoff_seconds(attempt: int) -> int:
    base = max(1, int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_BACKOFF_BASE_SECONDS', 5)))
    cap = max(base, int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_BACKOFF_MAX_SECONDS', 300)))
    return min(cap, base * (2 ** min(max(0, attempt - 1), 8)))


def publish_security_event(outbox_id: str) -> bool:
    """Best-effort publish by UUID; failure leaves durable work due for relay."""
    now = timezone.now()
    with transaction.atomic():
        row = SecurityEventOutbox.objects.select_for_update().filter(pk=outbox_id).first()
        if row is None or row.is_terminal:
            return False
        row.publish_attempts += 1
        attempt = row.publish_attempts
        row.status = SecurityEventOutbox.Status.RETRYING
        # Move the row out of the relay's due set before releasing its lease.
        # The worker can claim immediately (lease cleared), while a concurrent
        # relay cannot republish during a slow broker call.
        worker_window = max(
            15,
            int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_WORKER_LEASE_SECONDS', 60)),
        )
        row.next_dispatch_at = now + timedelta(seconds=worker_window)
        row.lease_owner = ''
        row.lease_expires_at = None
        row.save(update_fields=[
            'publish_attempts', 'status', 'next_dispatch_at', 'lease_owner',
            'lease_expires_at', 'updated_at',
        ])

    try:
        from connect.tasks import process_security_event_outbox
        # The outbox owns retry/backoff. Disable Celery's synchronous publish
        # retry so a broker outage cannot pin a web/relay process while kombu
        # exhausts its own retry loop.
        process_security_event_outbox.apply_async(args=[str(outbox_id)], retry=False)
    except Exception as exc:
        SecurityEventOutbox.objects.filter(pk=outbox_id, terminal_at__isnull=True).update(
            status=SecurityEventOutbox.Status.RETRYING,
            next_dispatch_at=now + timedelta(seconds=_backoff_seconds(attempt)),
            lease_owner='',
            lease_expires_at=None,
            last_error_class=_bounded_error(exc),
        )
        logger.exception('CON-217 failed to publish security outbox id=%s', outbox_id)
        return False

    updates = {
        'last_published_at': now,
        'lease_owner': '',
        'lease_expires_at': None,
        'last_error_class': '',
    }
    row = SecurityEventOutbox.objects.filter(pk=outbox_id, terminal_at__isnull=True).first()
    if row and row.first_published_at is None:
        updates['first_published_at'] = now
    SecurityEventOutbox.objects.filter(pk=outbox_id, terminal_at__isnull=True).update(**updates)
    return True


def relay_due_security_events(*, batch_size: int | None = None) -> dict:
    """Claim a bounded due batch with SKIP LOCKED, then publish outside locks."""
    batch_size = max(1, min(
        int(batch_size or getattr(settings, 'CONNECT_SECURITY_OUTBOX_RELAY_BATCH_SIZE', 50)),
        200,
    ))
    now = timezone.now()
    lease_seconds = max(15, int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_RELAY_LEASE_SECONDS', 45)))
    owner = f'relay:{uuid.uuid4()}'
    claimed: list[str] = []
    try:
        with transaction.atomic():
            due = (
                SecurityEventOutbox.objects.select_for_update(skip_locked=True)
                .filter(terminal_at__isnull=True, next_dispatch_at__lte=now)
                .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
                .order_by('next_dispatch_at')[:batch_size]
            )
            for row in due:
                row.lease_owner = owner
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.save(update_fields=['lease_owner', 'lease_expires_at', 'updated_at'])
                claimed.append(str(row.pk))

        published = sum(1 for outbox_id in claimed if publish_security_event(outbox_id))
        SecurityEventRelayHealth.objects.update_or_create(
            channel='security_outbox',
            defaults={
                'last_run_at': now,
                'last_success_at': timezone.now(),
                'last_error_class': '',
            },
        )
        return {'claimed': len(claimed), 'published': published}
    except Exception as exc:
        health, _ = SecurityEventRelayHealth.objects.get_or_create(channel='security_outbox')
        SecurityEventRelayHealth.objects.filter(pk=health.pk).update(
            last_run_at=now,
            failure_count=F('failure_count') + 1,
            last_error_class=_bounded_error(exc),
        )
        raise


def claim_for_delivery(outbox_id: str, *, worker_id: str):
    """Acquire an expiring worker lease and return a payload snapshot."""
    now = timezone.now()
    lease_seconds = max(15, int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_WORKER_LEASE_SECONDS', 60)))
    with transaction.atomic():
        row = SecurityEventOutbox.objects.select_for_update().filter(pk=outbox_id).first()
        if row is None:
            return None, 'missing'
        if row.is_terminal:
            return None, row.status
        if row.lease_expires_at and row.lease_expires_at > now and row.lease_owner not in ('', worker_id):
            return None, 'leased'
        row.status = SecurityEventOutbox.Status.PROCESSING
        row.delivery_attempts += 1
        row.lease_owner = worker_id[:64]
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.next_dispatch_at = row.lease_expires_at
        row.last_error_class = ''
        row.save(update_fields=[
            'status', 'delivery_attempts', 'lease_owner', 'lease_expires_at',
            'next_dispatch_at', 'last_error_class', 'updated_at',
        ])
        return dict(row.payload), 'claimed'


def mark_terminal(outbox_id: str, status: str, *, reason: str = '', message=None):
    if status not in SecurityEventOutbox.TERMINAL_STATUSES:
        raise ValueError(f'non-terminal status: {status}')
    with transaction.atomic():
        row = SecurityEventOutbox.objects.select_for_update().get(pk=outbox_id)
        if row.is_terminal:
            return row
        row.status = status
        row.terminal_at = timezone.now()
        row.terminal_reason = str(reason or status)[:128]
        row.terminal_message = message
        row.lease_owner = ''
        row.lease_expires_at = None
        row.last_error_class = ''
        row.save(update_fields=[
            'status', 'terminal_at', 'terminal_reason', 'terminal_message',
            'lease_owner', 'lease_expires_at', 'last_error_class', 'updated_at',
        ])
        return row


def claim_repeat_window(outbox_id: str, event: dict) -> bool:
    """Return true when another event owns the active semantic repeat window."""
    window = int(getattr(settings, 'CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS', 300))
    if window <= 0 or event.get('event_type') != 'platform.boundary_denial':
        return False
    fingerprint = semantic_fingerprint(event)
    now = timezone.now()
    with transaction.atomic():
        claim = SecurityEventRepeatClaim.objects.select_for_update().filter(
            fingerprint=fingerprint,
        ).first()
        if claim and claim.owner_outbox_id == uuid.UUID(str(outbox_id)):
            return False
        if claim and claim.expires_at > now:
            return True
        if claim:
            claim.owner_outbox_id = outbox_id
            claim.expires_at = now + timedelta(seconds=window)
            claim.save(update_fields=['owner_outbox', 'expires_at', 'updated_at'])
        else:
            SecurityEventRepeatClaim.objects.create(
                fingerprint=fingerprint,
                owner_outbox_id=outbox_id,
                expires_at=now + timedelta(seconds=window),
            )
        return False


def schedule_delivery_retry(outbox_id: str, exc: Exception):
    """Leave a failed delivery reclaimable or make it explicitly terminal."""
    now = timezone.now()
    with transaction.atomic():
        row = SecurityEventOutbox.objects.select_for_update().get(pk=outbox_id)
        if row.is_terminal:
            return row.status
        max_attempts = max(1, int(getattr(settings, 'CONNECT_SECURITY_OUTBOX_MAX_DELIVERY_ATTEMPTS', 12)))
        if row.delivery_attempts >= max_attempts:
            row.status = SecurityEventOutbox.Status.DEAD_LETTERED
            row.terminal_at = now
            row.terminal_reason = _bounded_error(exc)
        else:
            row.status = SecurityEventOutbox.Status.RETRYING
            row.next_dispatch_at = now + timedelta(seconds=_backoff_seconds(row.delivery_attempts))
        row.last_error_class = _bounded_error(exc)
        row.lease_owner = ''
        row.lease_expires_at = None
        row.save(update_fields=[
            'status', 'terminal_at', 'terminal_reason', 'next_dispatch_at',
            'last_error_class', 'lease_owner', 'lease_expires_at', 'updated_at',
        ])
        return row.status


def deliver_security_dm(outbox_id: str, *, recipient_user_id: int, content: str):
    """Create one deterministic DM and terminalize the outbox atomically."""
    from connect.serializers.messaging import DirectMessageSerializer
    from connect.views.internal_api import SYSTEM_USERNAME, _get_system_user_id, _safe_broadcast

    system_user_id = _get_system_user_id()
    client_message_id = uuid.uuid5(SECURITY_DM_NAMESPACE, str(outbox_id))
    callback = None
    with transaction.atomic():
        row = SecurityEventOutbox.objects.select_for_update().get(pk=outbox_id)
        if row.is_terminal:
            return row.terminal_message, False

        direct_key = Conversation.build_direct_key(system_user_id, recipient_user_id)
        conversation, conversation_created = Conversation.objects.get_or_create(
            conversation_type='direct',
            direct_key=direct_key,
        )
        if conversation_created:
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

        message, created = DirectMessage.objects.get_or_create(
            conversation=conversation,
            client_message_id=client_message_id,
            defaults={
                'sender_id': system_user_id,
                'sender_email': 'system@shizuha.com',
                'sender_name': 'Connect Security Monitor',
                'sender_username': SYSTEM_USERNAME,
                'message_type': 'text',
                'content': content,
            },
        )
        if created:
            conversation.message_count = F('message_count') + 1
            conversation.last_message_at = message.created_at
            conversation.last_message_preview = content[:100]
            conversation.save(update_fields=[
                'message_count', 'last_message_at', 'last_message_preview', 'updated_at',
            ])
            ConversationParticipant.objects.filter(
                conversation=conversation,
                has_left=False,
            ).exclude(user_id=system_user_id).update(unread_count=F('unread_count') + 1)

        row.status = SecurityEventOutbox.Status.SENT
        row.terminal_at = timezone.now()
        row.terminal_reason = 'sent'
        row.terminal_message = message
        row.lease_owner = ''
        row.lease_expires_at = None
        row.last_error_class = ''
        row.save(update_fields=[
            'status', 'terminal_at', 'terminal_reason', 'terminal_message',
            'lease_owner', 'lease_expires_at', 'last_error_class', 'updated_at',
        ])

        if created:
            message_data = DirectMessageSerializer(message).data
            conversation_id = str(conversation.id)
            created_at = conversation.created_at.isoformat() if conversation.created_at else None

            def callback():
                if conversation_created:
                    _safe_broadcast(f'user_inbox_{recipient_user_id}', {
                        'type': 'conversation_created',
                        'conversation_id': conversation_id,
                        'conversation': {
                            'id': conversation_id,
                            'conversation_type': 'direct',
                            'created_at': created_at,
                        },
                    })
                _safe_broadcast(
                    f'chat_{conversation_id}',
                    {'type': 'new_message', 'message_data': message_data},
                )

            transaction.on_commit(callback)
        return message, created

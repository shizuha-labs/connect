"""CON-165 replay, idempotency, and duplicate-storm controls.

This module intentionally stores replay audit evidence on the existing
``BridgeOutboundEvent`` row instead of creating a second delivery envelope: the
row's provider/account/idempotency key remains the idempotency boundary, so a
replay can never duplicate a delivered message or create a second outbound event.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from connect.metrics import record_bridge_replay, record_duplicate_storm
from connect.models import BridgeOutboundEvent
from connect.services.backpressure import build_retry_metadata


REPLAYABLE_OUTBOUND_STATUSES = {
    BridgeOutboundEvent.Status.PENDING,
    BridgeOutboundEvent.Status.RETRYING,
    BridgeOutboundEvent.Status.DEAD_LETTERED,
}
def _int_setting(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(getattr(settings, name, os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def duplicate_storm_window_seconds() -> int:
    return _int_setting('CONNECT_BRIDGE_DUPLICATE_STORM_WINDOW_SECONDS', 60, 0)


def duplicate_storm_threshold() -> int:
    return _int_setting('CONNECT_BRIDGE_DUPLICATE_STORM_THRESHOLD', 5, 0)


def replay_batch_limit() -> int:
    return _int_setting('CONNECT_BRIDGE_REPLAY_MAX_BATCH_SIZE', 100, 1)


@dataclass(frozen=True)
class DuplicateStormDecision:
    suppressed: bool
    count: int = 0
    threshold: int = 0
    window_seconds: int = 0
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayScope:
    provider: str | None = None
    channel_key: str | None = None
    conversation_id: str | int | None = None
    since: datetime | None = None
    until: datetime | None = None
    statuses: Iterable[str] = field(default_factory=lambda: tuple(REPLAYABLE_OUTBOUND_STATUSES))


@dataclass(frozen=True)
class ReplayItemResult:
    event_id: str
    outbound_event_id: str
    status_before: str
    status_after: str
    action: str
    provider_delivery_id: str = ''
    error_code: str = ''


@dataclass(frozen=True)
class ReplayResult:
    dry_run: bool
    requested_batch_size: int
    bounded_batch_size: int
    candidates: int
    replayed: int
    skipped: int
    failed: int
    items: tuple[ReplayItemResult, ...]


def outbound_duplicate_storm_decision(envelope, request_body_hash: str, *, now=None) -> DuplicateStormDecision:
    """Return a suppression decision for repeated identical outbound bridge sends.

    The storm key is intentionally privacy-safe and bounded: provider/account,
    bounded channel, conversation, recipient, and SHA-256 body hash. A zero
    threshold/window disables the guard for emergency override.
    """
    threshold = duplicate_storm_threshold()
    window_seconds = duplicate_storm_window_seconds()
    if not threshold or not window_seconds:
        return DuplicateStormDecision(False, threshold=threshold, window_seconds=window_seconds)

    now = now or timezone.now()
    since = now - timezone.timedelta(seconds=window_seconds)
    qs = BridgeOutboundEvent.objects.filter(
        provider=envelope.provider,
        provider_account_id=envelope.provider_account_id,
        channel_key=envelope.channel_key,
        recipient_external_id=envelope.recipient_external_id,
        body_hash=request_body_hash,
        created_at__gte=since,
    ).exclude(status__in=[
        BridgeOutboundEvent.Status.CONFLICT,
        BridgeOutboundEvent.Status.FORBIDDEN,
    ])
    if envelope.conversation_id:
        qs = qs.filter(conversation_id=envelope.conversation_id)
    else:
        qs = qs.filter(conversation__isnull=True)

    count = qs.count()
    suppressed = count >= threshold
    evidence = {
        'duplicate_storm_key': {
            'provider': envelope.provider,
            'provider_account_id': envelope.provider_account_id,
            'channel_key': envelope.channel_key,
            'conversation_id': str(envelope.conversation_id or ''),
            'recipient_external_id': envelope.recipient_external_id,
            'body_hash': request_body_hash,
        },
        'duplicate_storm_count': count,
        'duplicate_storm_threshold': threshold,
        'duplicate_storm_window_seconds': window_seconds,
    }
    if suppressed:
        record_duplicate_storm(envelope.provider, envelope.channel_key, 'suppressed')
    return DuplicateStormDecision(suppressed, count=count, threshold=threshold, window_seconds=window_seconds, evidence=evidence)


def append_replay_audit(
    event: BridgeOutboundEvent,
    *,
    actor: str,
    task_key: str,
    reason: str,
    action: str,
    dry_run: bool,
    status_before: str | None = None,
) -> dict:
    metadata = dict(event.audit_metadata or {})
    history = list(metadata.get('replay_history') or [])
    history.append({
        'at': timezone.now().isoformat(),
        'actor': str(actor or 'unknown')[:120],
        'task_key': str(task_key or '')[:80],
        'reason': str(reason or '')[:240],
        'action': action,
        'dry_run': bool(dry_run),
        'status_before': status_before or event.status,
    })
    metadata['replay_history'] = history[-20:]
    metadata['last_replay_actor'] = str(actor or 'unknown')[:120]
    metadata['last_replay_task_key'] = str(task_key or '')[:80]
    metadata['last_replay_reason'] = str(reason or '')[:240]
    metadata['last_replay_action'] = action
    metadata['last_replay_at'] = timezone.now().isoformat()
    return metadata


def replay_queryset(scope: ReplayScope):
    qs = BridgeOutboundEvent.objects.filter(status__in=list(scope.statuses or REPLAYABLE_OUTBOUND_STATUSES))
    if scope.provider:
        qs = qs.filter(provider=scope.provider)
    if scope.channel_key:
        qs = qs.filter(channel_key=scope.channel_key)
    if scope.conversation_id:
        qs = qs.filter(conversation_id=scope.conversation_id)
    if scope.since:
        qs = qs.filter(created_at__gte=scope.since)
    if scope.until:
        qs = qs.filter(created_at__lte=scope.until)
    return qs.order_by('created_at', 'id')


def _redacted_envelope_for_event(event: BridgeOutboundEvent):
    return SimpleNamespace(
        provider=event.provider,
        provider_account_id=event.provider_account_id,
        channel_key=event.channel_key,
        task_key=event.task_key,
        agent_id=event.agent_id,
        recipient_external_id=event.recipient_external_id,
        # Body is deliberately not persisted on BridgeOutboundEvent. Provider
        # adapters used for replay must use the existing event/audit payload they
        # own; the default deterministic sender only needs the event identity.
        body='',
        idempotency_key=event.idempotency_key,
        conversation_id=str(event.conversation_id) if event.conversation_id else None,
    )


def _default_replay_provider_send(event: BridgeOutboundEvent, _envelope) -> dict[str, str]:
    """Deterministic provider stub for tests/dry local replay plumbing."""
    delivery_seed = f"{event.provider}:{event.provider_account_id}:{event.idempotency_key}:replay"
    return {"provider_delivery_id": hashlib.sha256(delivery_seed.encode("utf-8")).hexdigest()[:32]}


def replay_outbound_bridge_events(
    *,
    scope: ReplayScope | None = None,
    actor: str,
    task_key: str,
    reason: str,
    batch_size: int = 100,
    dry_run: bool = True,
    provider_send: Callable | None = None,
) -> ReplayResult:
    """Replay bounded queued/dead-lettered outbound bridge events idempotently.

    Delivered/accepted/conflict/forbidden rows are never replay candidates. Each
    execution reuses and updates the existing row; no new BridgeOutboundEvent row
    is created, preserving provider/account/idempotency uniqueness.
    """
    scope = scope or ReplayScope()
    bounded_batch_size = min(max(1, int(batch_size or 1)), replay_batch_limit())
    candidates = list(replay_queryset(scope)[:bounded_batch_size])
    items: list[ReplayItemResult] = []
    replayed = skipped = failed = 0
    sender = provider_send or _default_replay_provider_send

    for candidate in candidates:
        status_before = candidate.status
        if status_before not in REPLAYABLE_OUTBOUND_STATUSES:
            skipped += 1
            record_bridge_replay(candidate.provider, candidate.channel_key, 'skipped_terminal')
            items.append(ReplayItemResult(
                event_id=str(candidate.id),
                outbound_event_id=str(candidate.outbound_event_id),
                status_before=status_before,
                status_after=status_before,
                action='skipped_terminal',
                error_code=candidate.error_code,
            ))
            continue

        if dry_run:
            skipped += 1
            record_bridge_replay(candidate.provider, candidate.channel_key, 'dry_run')
            items.append(ReplayItemResult(
                event_id=str(candidate.id),
                outbound_event_id=str(candidate.outbound_event_id),
                status_before=status_before,
                status_after=status_before,
                action='dry_run',
                provider_delivery_id=candidate.provider_delivery_id,
                error_code=candidate.error_code,
            ))
            continue

        with transaction.atomic():
            event = BridgeOutboundEvent.objects.select_for_update().get(pk=candidate.pk)
            status_before = event.status
            if status_before not in REPLAYABLE_OUTBOUND_STATUSES:
                skipped += 1
                record_bridge_replay(event.provider, event.channel_key, 'skipped_terminal')
                items.append(ReplayItemResult(
                    event_id=str(event.id),
                    outbound_event_id=str(event.outbound_event_id),
                    status_before=status_before,
                    status_after=status_before,
                    action='skipped_terminal',
                    provider_delivery_id=event.provider_delivery_id,
                    error_code=event.error_code,
                ))
                continue
            event.audit_metadata = append_replay_audit(
                event,
                actor=actor,
                task_key=task_key,
                reason=reason,
                action='attempted',
                dry_run=False,
                status_before=status_before,
            )
            event.save(update_fields=['audit_metadata', 'updated_at'])

        try:
            provider_result = sender(event, _redacted_envelope_for_event(event)) or {}
            provider_delivery_id = str(provider_result.get('provider_delivery_id') or event.provider_delivery_id or '')
            event.provider_delivery_id = provider_delivery_id
            event.status = BridgeOutboundEvent.Status.ACCEPTED
            event.error_code = ''
            event.error_message = ''
            event.audit_metadata = {
                **append_replay_audit(
                    event,
                    actor=actor,
                    task_key=task_key,
                    reason=reason,
                    action='accepted',
                    dry_run=False,
                    status_before=status_before,
                ),
                'provider_delivery_id_recorded': bool(provider_delivery_id),
            }
            event.save(update_fields=['provider_delivery_id', 'status', 'error_code', 'error_message', 'audit_metadata', 'updated_at'])
            replayed += 1
            record_bridge_replay(event.provider, event.channel_key, 'accepted')
            items.append(ReplayItemResult(
                event_id=str(event.id),
                outbound_event_id=str(event.outbound_event_id),
                status_before=status_before,
                status_after=event.status,
                action='accepted',
                provider_delivery_id=provider_delivery_id,
            ))
        except Exception as exc:
            status_value, audit_metadata = build_retry_metadata(event, exc)
            event.status = status_value
            event.error_code = 'provider_send_dead_lettered' if status_value == BridgeOutboundEvent.Status.DEAD_LETTERED else 'provider_send_retrying'
            event.error_message = str(exc)[:240]
            event.audit_metadata = audit_metadata
            event.audit_metadata = append_replay_audit(
                event,
                actor=actor,
                task_key=task_key,
                reason=reason,
                action='failed',
                dry_run=False,
                status_before=status_before,
            )
            event.save(update_fields=['status', 'error_code', 'error_message', 'audit_metadata', 'updated_at'])
            failed += 1
            record_bridge_replay(event.provider, event.channel_key, 'failed')
            items.append(ReplayItemResult(
                event_id=str(event.id),
                outbound_event_id=str(event.outbound_event_id),
                status_before=status_before,
                status_after=event.status,
                action='failed',
                error_code=event.error_code,
            ))

    return ReplayResult(
        dry_run=dry_run,
        requested_batch_size=int(batch_size or 0),
        bounded_batch_size=bounded_batch_size,
        candidates=len(candidates),
        replayed=replayed,
        skipped=skipped,
        failed=failed,
        items=tuple(items),
    )

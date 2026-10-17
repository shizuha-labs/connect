"""CON-164 bounded backpressure policy for Connect delivery paths.

Semantics (kept close to code so reviewers/ops can audit behavior):
- provider outage/rate-limit: never drop silently; record `retrying` with a
  bounded Retry-After/jitter hint, or `dead_lettered` after the max-attempt budget.
- local queue overload: fail closed before provider send and surface retrying with
  queue depth/oldest age in audit metadata.
- websocket slow consumer: bound each send with a timeout; on timeout mark the
  connection overloaded and close it so reconnect replay is the recovery path.
- DB persistence failure: callers must not receive success unless the
  DirectMessage/BridgeOutboundEvent row committed; exceptions propagate or return
  explicit send_error frames.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


OUTBOUND_MAX_PENDING = _int_env('CONNECT_OUTBOUND_MAX_PENDING', 1000, 1)
OUTBOUND_MAX_OLDEST_SECONDS = _int_env('CONNECT_OUTBOUND_MAX_OLDEST_SECONDS', 1800, 1)
OUTBOUND_RETRY_BASE_SECONDS = _int_env('CONNECT_OUTBOUND_RETRY_BASE_SECONDS', 30, 1)
OUTBOUND_RETRY_MAX_SECONDS = _int_env('CONNECT_OUTBOUND_RETRY_MAX_SECONDS', 300, 1)
OUTBOUND_MAX_ATTEMPTS = _int_env('CONNECT_OUTBOUND_MAX_ATTEMPTS', 3, 1)
WS_SEND_TIMEOUT_SECONDS = _int_env('CONNECT_WS_SEND_TIMEOUT_SECONDS', 5, 1)
WS_CLOSE_CODE_OVERLOAD = 1013


@dataclass(frozen=True)
class QueueState:
    depth: int
    oldest_age_seconds: int
    overloaded: bool
    reason: str = ''


class ProviderRateLimited(Exception):
    """Adapter-facing exception carrying a Retry-After hint."""

    status_code = 429

    def __init__(self, message='provider rate limited', retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_for_attempt(attempt: int, seed: str = '') -> int:
    """Bounded exponential backoff with deterministic jitter for idempotent retries."""
    attempt = max(1, int(attempt or 1))
    base = min(OUTBOUND_RETRY_MAX_SECONDS, OUTBOUND_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    digest = hashlib.sha256(str(seed or attempt).encode('utf-8')).hexdigest()
    jitter = int(digest[:2], 16) % max(1, min(30, base))
    return min(OUTBOUND_RETRY_MAX_SECONDS, base + jitter)


def provider_retry_after(exc, attempt: int, seed: str = '') -> int:
    explicit = getattr(exc, 'retry_after', None)
    if explicit is not None:
        try:
            return max(1, min(OUTBOUND_RETRY_MAX_SECONDS, int(explicit)))
        except (TypeError, ValueError):
            pass
    return retry_after_for_attempt(attempt, seed=seed)


def is_provider_rate_limited(exc) -> bool:
    return int(getattr(exc, 'status_code', 0) or 0) == 429 or 'rate limit' in str(exc).lower()


def outbound_queue_state(provider: str, channel_key: str, *, now=None) -> QueueState:
    from connect.models import BridgeOutboundEvent

    now = now or timezone.now()
    pending_statuses = [
        BridgeOutboundEvent.Status.PENDING,
        BridgeOutboundEvent.Status.RETRYING,
    ]
    qs = BridgeOutboundEvent.objects.filter(
        provider=provider,
        channel_key=channel_key,
        status__in=pending_statuses,
    )
    depth = qs.count()
    oldest_age = 0
    oldest = qs.order_by('created_at').values_list('created_at', flat=True).first()
    if oldest:
        oldest_age = int((now - oldest).total_seconds())
    reason = ''
    if depth >= OUTBOUND_MAX_PENDING:
        reason = 'queue_depth'
    elif oldest_age >= OUTBOUND_MAX_OLDEST_SECONDS:
        reason = 'oldest_age'
    return QueueState(depth=depth, oldest_age_seconds=oldest_age, overloaded=bool(reason), reason=reason)


def build_retry_metadata(event, exc, *, queue_state: QueueState | None = None) -> tuple[str, dict]:
    """Return `(status, metadata)` for a provider/backpressure retry decision."""
    current = dict(event.audit_metadata or {})
    attempts = int(current.get('attempts') or 0) + 1
    retry_after = provider_retry_after(exc, attempts, seed=str(event.id))
    terminal = attempts >= OUTBOUND_MAX_ATTEMPTS
    current.update({
        'delivery_state': 'dead_lettered' if terminal else 'retrying',
        'attempts': attempts,
        'retry_after_seconds': retry_after,
        'last_error_class': exc.__class__.__name__,
        'last_error_message': str(exc)[:160],
    })
    if queue_state is not None:
        current.update({
            'queue_depth': queue_state.depth,
            'queue_oldest_age_seconds': queue_state.oldest_age_seconds,
            'queue_backpressure_reason': queue_state.reason,
        })
    from connect.models import BridgeOutboundEvent

    return (BridgeOutboundEvent.Status.DEAD_LETTERED if terminal else BridgeOutboundEvent.Status.RETRYING), current

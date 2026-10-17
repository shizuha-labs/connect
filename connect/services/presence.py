"""Presence state service for Connect.

This module is the only place that mutates presence rows. Consumers, bridge
webhooks, and cleanup workers call it with heartbeats/disconnects; the service
then recomputes the user-level aggregate deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import redis

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from connect.models import PresenceSession, PresenceStatus

logger = logging.getLogger(__name__)


STALE_AFTER = timedelta(seconds=90)
OFFLINE_AFTER = timedelta(minutes=5)
MAX_SESSION_KEY_LEN = 200
MAX_CLIENT_ID_LEN = 200
MAX_CLIENT_LABEL_LEN = 200
MAX_CHANNEL_NAME_LEN = 255


@dataclass(frozen=True)
class PresenceSnapshot:
    user_id: int
    status: str
    active_session_count: int
    degraded_session_count: int
    last_seen_at: object
    sessions: list[dict]
    # Tenant derived from the user's live sessions, so context-free callers (the
    # stale-cleanup worker) can publish to the correct org bridge channel.
    organization_id: object = None

    def as_event(self) -> dict:
        return {
            'user_id': self.user_id,
            'status': self.status,
            'active_session_count': self.active_session_count,
            'degraded_session_count': self.degraded_session_count,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
            'sessions': self.sessions,
        }


def _clamp_text(value: str, max_len: int) -> str:
    return str(value or '')[:max_len]


def _status_defaults(status: str = 'offline') -> dict:
    return {
        'status': status,
        'active_session_count': 0,
        'degraded_session_count': 0,
        'details': {'sessions': []},
    }


def _locked_status_row(user_id: int) -> PresenceStatus:
    try:
        row, _ = PresenceStatus.objects.select_for_update().get_or_create(
            user_id=user_id,
            defaults=_status_defaults(),
        )
        return row
    except IntegrityError:
        # Concurrent first writer won the unique(user_id) race. Fetch and lock
        # the canonical row instead of surfacing a 500 to GET /presence/.
        return PresenceStatus.objects.select_for_update().get(user_id=user_id)


def _session_payload(session: PresenceSession) -> dict:
    return {
        'session_id': str(session.id),
        'session_key': session.session_key,
        'client_id': session.client_id,
        'client_type': session.client_type,
        'client_label': session.client_label,
        'is_agent': session.is_agent,
        'status': session.status,
        'last_heartbeat_at': session.last_heartbeat_at.isoformat() if session.last_heartbeat_at else None,
        'stale_at': session.stale_at.isoformat() if session.stale_at else None,
        'offline_at': session.offline_at.isoformat() if session.offline_at else None,
    }


def _aggregate_status(sessions: list[PresenceSession]) -> tuple[str, int, int]:
    """Return user-level status, active count, degraded/stale count.

    Deterministic priority:
    - online if any eligible session is online
    - away if no online sessions and at least one session is away
    - degraded if only degraded/stale sessions remain
    - offline when every session is offline or absent
    """
    active = [s for s in sessions if s.status != 'offline']
    degraded_count = sum(1 for s in active if s.status in {'degraded', 'stale'})
    statuses = {s.status for s in active}
    if 'online' in statuses:
        return 'online', len(active), degraded_count
    if 'away' in statuses:
        return 'away', len(active), degraded_count
    if statuses & {'degraded', 'stale'}:
        return 'degraded', len(active), degraded_count
    return 'offline', 0, 0


def _recompute_user(user_id: int) -> PresenceSnapshot:
    status_row = _locked_status_row(user_id)
    sessions = list(
        PresenceSession.objects.select_for_update()
        .filter(user_id=user_id)
        .exclude(status='offline')
        .order_by('pk')
    )
    status, active_count, degraded_count = _aggregate_status(sessions)
    last_seen_at = PresenceSession.objects.filter(user_id=user_id).aggregate(
        last_seen=Max('last_heartbeat_at')
    )['last_seen']
    payload = [_session_payload(s) for s in sessions]
    # Tenant for context-free publishers (cleanup): prefer the org carried by a
    # live session; fall back to the latest org seen on any session for this
    # user (covers all-offline cleanup snapshots where `sessions` is empty).
    org_id = next((s.organization_id for s in sessions if s.organization_id is not None), None)
    if org_id is None:
        org_id = (
            PresenceSession.objects
            .filter(user_id=user_id, organization_id__isnull=False)
            .order_by('-last_heartbeat_at')
            .values_list('organization_id', flat=True)
            .first()
        )
    status_row.status = status
    status_row.active_session_count = active_count
    status_row.degraded_session_count = degraded_count
    status_row.last_seen_at = last_seen_at
    status_row.details = {'sessions': payload}
    status_row.save(update_fields=[
        'status', 'active_session_count', 'degraded_session_count',
        'last_seen_at', 'details', 'updated_at',
    ])
    return PresenceSnapshot(
        user_id=user_id,
        status=status,
        active_session_count=active_count,
        degraded_session_count=degraded_count,
        last_seen_at=last_seen_at,
        sessions=payload,
        organization_id=org_id,
    )


@transaction.atomic
def touch_presence(
    *,
    user_id: int,
    session_key: str,
    client_id: str = '',
    client_type: str = 'unknown',
    client_label: str = '',
    is_agent: bool = False,
    status: str = 'online',
    channel_name: str = '',
    metadata: dict | None = None,
    organization_id=None,
    at=None,
) -> PresenceSnapshot:
    """Create/update a live presence session and recompute its user aggregate."""
    now = at or timezone.now()
    # Lock the aggregate row first.  Heartbeats for different sessions of the
    # same user then acquire locks in a consistent PresenceStatus -> sessions
    # order, avoiding circular waits between per-session update_or_create() and
    # _recompute_user()'s all-session lock.
    _locked_status_row(user_id)
    if status == 'stale':
        stale_at = now
        offline_at = None
    elif status == 'offline':
        stale_at = now
        offline_at = now
    else:
        stale_at = None
        offline_at = None
    defaults = {
        'user_id': user_id,
        'client_id': _clamp_text(client_id, MAX_CLIENT_ID_LEN),
        'client_type': client_type if client_type in dict(PresenceSession.CLIENT_TYPES) else 'unknown',
        'client_label': _clamp_text(client_label, MAX_CLIENT_LABEL_LEN),
        'is_agent': is_agent,
        'status': status,
        'metadata': metadata or {},
        'last_heartbeat_at': now,
        'stale_at': stale_at,
        'offline_at': offline_at,
        'channel_name': _clamp_text(channel_name, MAX_CHANNEL_NAME_LEN),
    }
    # Only set the org when the caller knows it (live WS/REST paths carry a JWT),
    # so a context-free heartbeat never clobbers a previously-stored org.
    if organization_id is not None:
        defaults['organization_id'] = organization_id
    PresenceSession.objects.update_or_create(
        session_key=_clamp_text(session_key, MAX_SESSION_KEY_LEN),
        defaults=defaults,
    )
    return _recompute_user(user_id)


def set_active_conversation(*, user_id: int, session_key: str, conversation_id, at=None) -> int:
    """CON-15 B.2: record (focus/open) or clear (blur/close) the conversation a
    session is currently foregrounding, for the active-conversation-open
    notification suppressor.

    `conversation_id=None` clears focus. The timestamp is refreshed on focus so the
    suppressor's freshness TTL (notifications.ACTIVE_CONVERSATION_TTL_SECONDS) can
    expire a stale tab that never sent a blur. Returns the number of session rows
    updated (the WS session must already exist via touch_presence)."""
    now = at or timezone.now()
    return PresenceSession.objects.filter(
        user_id=user_id, session_key=_clamp_text(session_key, MAX_SESSION_KEY_LEN),
    ).update(
        active_conversation_id=conversation_id,
        active_conversation_updated_at=(now if conversation_id else None),
    )


@transaction.atomic
def mark_offline(*, user_id: int, session_key: str, at=None) -> PresenceSnapshot:
    now = at or timezone.now()
    _locked_status_row(user_id)
    PresenceSession.objects.filter(user_id=user_id, session_key=session_key).update(
        status='offline',
        stale_at=now,
        offline_at=now,
        last_heartbeat_at=now,
    )
    return _recompute_user(user_id)


def cleanup_stale_presence(*, stale_after: timedelta = STALE_AFTER, offline_after: timedelta = OFFLINE_AFTER, at=None) -> list[PresenceSnapshot]:
    """Move missed-heartbeat sessions online/away/degraded -> stale -> offline."""
    now = at or timezone.now()
    stale_cutoff = now - stale_after
    offline_cutoff = now - offline_after

    candidate_user_ids = set(
        PresenceSession.objects.filter(
            Q(status__in=['online', 'away', 'degraded'], last_heartbeat_at__lt=stale_cutoff)
            | Q(status='stale', last_heartbeat_at__lt=offline_cutoff)
        ).values_list('user_id', flat=True)
    )

    snapshots: list[PresenceSnapshot] = []
    for user_id in sorted(candidate_user_ids):
        with transaction.atomic():
            _locked_status_row(user_id)
            sessions = list(
                PresenceSession.objects.select_for_update()
                .filter(user_id=user_id)
                .filter(
                    Q(status__in=['online', 'away', 'degraded'], last_heartbeat_at__lt=stale_cutoff)
                    | Q(status='stale', last_heartbeat_at__lt=offline_cutoff)
                )
                .order_by('pk')
            )
            for session in sessions:
                if session.status in {'online', 'away', 'degraded'} and session.last_heartbeat_at < stale_cutoff:
                    session.status = 'stale'
                    session.stale_at = now
                    session.save(update_fields=['status', 'stale_at', 'updated_at'])
                elif session.status == 'stale' and session.last_heartbeat_at < offline_cutoff:
                    session.status = 'offline'
                    session.offline_at = now
                    session.save(update_fields=['status', 'offline_at', 'updated_at'])
            snapshots.append(_recompute_user(user_id))

    return snapshots


def refresh_presence(
    *,
    user_id: int,
    session_key: str,
    default_status: str = 'online',
    at=None,
    **kwargs,
) -> PresenceSnapshot:
    """Heartbeat an existing session without overwriting explicit away/degraded."""
    current = (
        PresenceSession.objects
        .filter(user_id=user_id, session_key=_clamp_text(session_key, MAX_SESSION_KEY_LEN))
        .values_list('status', flat=True)
        .first()
    )
    status = current if current in {'away', 'degraded'} else default_status
    return touch_presence(user_id=user_id, session_key=session_key, status=status, at=at, **kwargs)


def publish_presence_bridge(snapshot: PresenceSnapshot, *, org_id=1) -> None:
    """Publish a presence snapshot to the cross-service Redis bridge."""
    try:
        r = redis.from_url(
            settings.CHANNEL_REDIS_URL,
            socket_timeout=settings.CHANNEL_REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=settings.CHANNEL_REDIS_CONNECT_TIMEOUT,
        )
        r.publish(f"shizuha:presence:{org_id or 1}", json.dumps(snapshot.as_event()))
        r.close()
    except Exception:
        logger.debug("Failed to publish presence bridge event for user %s", snapshot.user_id)


def get_presence_for_users(user_ids: list[int]) -> list[PresenceStatus]:
    """Return aggregate rows, creating offline defaults for missing users."""
    existing = {p.user_id: p for p in PresenceStatus.objects.filter(user_id__in=user_ids)}
    missing = [uid for uid in user_ids if uid not in existing]
    for uid in missing:
        with transaction.atomic():
            try:
                existing[uid], _ = PresenceStatus.objects.get_or_create(
                    user_id=uid,
                    defaults=_status_defaults(),
                )
            except IntegrityError:
                existing[uid] = PresenceStatus.objects.get(user_id=uid)
    return [existing[uid] for uid in user_ids]

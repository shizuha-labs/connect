"""Event-driven Connect security anomaly detection.

PLAT-1237: emit SIEM-lite events from the message/conversation paths when a
runtime action crosses org/privilege boundaries or shows abnormal DM fan-out.
The detector is best-effort and must never block delivery.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from typing import Any, Iterable

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _as_int(value: Any):
    try:
        if value in (None, ''):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_org_ids(source: Any) -> set[int]:
    """Extract organization ids from JWT/user/identity payload variants."""
    if not source:
        return set()

    if isinstance(source, (str, int)):
        value = _as_int(source)
        return {value} if value is not None else set()

    if isinstance(source, (list, tuple, set, frozenset)):
        # Org ids captured at request time and stored on the raw security event
        # arrive back here as a JSON list (e.g. sorted([1])). Recurse so a bare
        # list of ids — or of membership dicts — resolves, instead of falling
        # through to the object-attribute probe below (which returns empty and
        # forces a redundant shizuha-id re-fetch during worker evaluation).
        collected: set[int] = set()
        for item in source:
            collected |= normalize_org_ids(item)
        return collected

    ids: set[int] = set()
    if isinstance(source, dict):
        for key in (
            'organization_id',
            'org_id',
            'organization',
            'org',
            'tenant_id',
            'primary_organization_id',
        ):
            value = _as_int(source.get(key))
            if value is not None:
                ids.add(value)

        for key in ('organization_ids', 'org_ids', 'organizations', 'organization_memberships'):
            value = source.get(key)
            if isinstance(value, dict):
                iterable: Iterable[Any] = value.keys()
            elif isinstance(value, (list, tuple, set)):
                iterable = value
            else:
                continue
            for item in iterable:
                if isinstance(item, dict):
                    item_value = (
                        item.get('id')
                        or item.get('organization_id')
                        or item.get('org_id')
                    )
                else:
                    item_value = item
                parsed = _as_int(item_value)
                if parsed is not None:
                    ids.add(parsed)
        return ids

    for attr in ('organization_id', 'org_id', 'tenant_id'):
        value = _as_int(getattr(source, attr, None))
        if value is not None:
            ids.add(value)
    return ids


def org_ids_from_request(request) -> set[int]:
    ids = normalize_org_ids(getattr(request, 'auth', None))
    if ids:
        return ids
    return normalize_org_ids(getattr(request, 'user', None))


def _identity_org_ids(user_ids: Iterable[int]) -> dict[int, set[int]]:
    """Best-effort org lookup via shizuha-id batch identity endpoint."""
    ids = [str(uid) for uid in user_ids if uid]
    if not ids or not bool(getattr(settings, 'CONNECT_SECURITY_ID_ORG_LOOKUP_ENABLED', True)):
        return {}
    try:
        import requests

        id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
        resp = requests.get(
            f"{id_base}/internal/users/",
            params={'ids': ','.join(ids)},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=2,
        )
        if resp.status_code != 200:
            logger.warning("security monitor org lookup returned %s", resp.status_code)
            return {}
        payload = resp.json() or []
        if isinstance(payload, dict):
            payload = payload.get('results') or payload.get('users') or [payload]
        out: dict[int, set[int]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            uid = _as_int(item.get('id') or item.get('user_id'))
            if uid is not None:
                out[uid] = normalize_org_ids(item)
        return out
    except Exception as exc:
        logger.warning("security monitor org lookup failed: %s", exc)
        return {}


def _enabled() -> bool:
    return bool(getattr(settings, 'CONNECT_SECURITY_ANOMALY_ALERTS_ENABLED', True))


@lru_cache(maxsize=4)
def _dedupe_redis_client(url: str):
    import redis
    return redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)


def claim_security_dedupe(key: str, *, ttl_seconds: int) -> bool:
    """Atomically claim a retry/repeat fingerprint across all Connect replicas.

    Production uses the shared Redis broker. An empty URL is an explicit local
    test fallback. Redis failure is fail-open (send the alert) and increments the
    existing fail-loud metric so a detector outage cannot silently suppress data.
    """
    ttl_seconds = max(1, int(ttl_seconds))
    url = getattr(settings, 'CONNECT_SECURITY_DEDUPE_REDIS_URL', '') or ''
    if not url:
        return bool(cache.add(key, 1, timeout=ttl_seconds))
    try:
        return bool(_dedupe_redis_client(url).set(key, '1', nx=True, ex=ttl_seconds))
    except Exception:
        try:
            from connect.metrics import record_connect_security_alert_failure
            record_connect_security_alert_failure('dedupe_exception')
        except Exception:
            pass
        logger.exception('Connect security dedupe Redis claim failed; failing open')
        return True


def _queue_alert(event: dict[str, Any]) -> None:
    if not _enabled():
        return
    try:
        from connect.metrics import record_connect_security_event_queued
        from connect.tasks import send_security_anomaly_alert

        def _enqueue():
            try:
                send_security_anomaly_alert.delay(event)
                record_connect_security_event_queued(event.get('event_type'))
            except Exception:
                from connect.metrics import record_connect_security_alert_failure
                record_connect_security_alert_failure('enqueue_exception')
                logger.exception("failed to enqueue Connect security anomaly alert")

        transaction.on_commit(_enqueue)
    except Exception:
        try:
            from connect.metrics import record_connect_security_alert_failure
            record_connect_security_alert_failure('enqueue_exception')
        except Exception:
            pass
        logger.exception("failed to enqueue Connect security anomaly alert")


def emit_direct_message_anomalies(
    *,
    request,
    message,
    conversation,
    recipient_payload: dict[str, Any] | None,
    sender_is_agent: bool,
    created_new_conversation: bool,
) -> None:
    """Queue a raw DM security event after a successful /api/messaging/dm/ send.

    Keep this hook intentionally tiny: it only copies fields already available in
    the request/identity response and defers identity enrichment, fan-out counts,
    and rule evaluation to the Celery worker. The user-facing DM hot path must
    never wait on shizuha-id or a joined distinct-count query.
    """
    sender_org_ids = org_ids_from_request(request)
    recipient_org_ids = normalize_org_ids(recipient_payload or {})
    _queue_alert({
        'event_type': 'connect.direct_message.event',
        'sender_user_id': int(message.sender_id),
        'sender_email': message.sender_email,
        'sender_username': message.sender_username,
        'sender_is_agent': bool(sender_is_agent),
        'sender_org_ids': sorted(sender_org_ids),
        'recipient_user_id': recipient_payload.get('user_id') if recipient_payload else None,
        'recipient_email': recipient_payload.get('email') if recipient_payload else '',
        'recipient_name': recipient_payload.get('name') if recipient_payload else '',
        'recipient_org_ids': sorted(recipient_org_ids),
        'conversation_id': str(conversation.id),
        'message_id': str(message.id),
        'created_new_conversation': bool(created_new_conversation),
        'detected_at': timezone.now().isoformat(),
    })


def emit_direct_conversation_anomaly(
    *,
    request,
    conversation,
    target_user_id: int,
    target_org_ids: set[int] | None = None,
    created: bool,
) -> None:
    """Emit direct contact-attempt anomalies from conversation creation."""
    if not created:
        return
    token = getattr(request, 'auth', None) or {}
    sender_is_agent = False
    try:
        sender_is_agent = bool(token.get('is_agent', False))
    except Exception:
        sender_is_agent = False
    sender_org_ids = org_ids_from_request(request)
    recipient_org_ids = set(target_org_ids or [])
    shared_org_ids = sorted(sender_org_ids & recipient_org_ids)
    disjoint_known_orgs = bool(sender_org_ids and recipient_org_ids and not shared_org_ids)
    if not (disjoint_known_orgs and sender_is_agent):
        return
    _queue_alert({
        'event_type': 'connect.direct_conversation.anomaly',
        'reasons': ['cross_org_direct_conversation', 'internal_agent_external_user_contact_attempt'],
        'sender_user_id': int(getattr(request.user, 'id', 0) or 0),
        'sender_email': getattr(request.user, 'email', '') or '',
        'sender_username': getattr(request.user, 'username', '') or '',
        'sender_is_agent': True,
        'sender_org_ids': sorted(sender_org_ids),
        'recipient_user_id': int(target_user_id),
        'recipient_org_ids': sorted(recipient_org_ids),
        'shared_org_ids': shared_org_ids,
        'conversation_id': str(conversation.id),
        'created_new_conversation': bool(created),
        'detected_at': timezone.now().isoformat(),
    })

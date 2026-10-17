"""Resolve a human conversation label when stored participant names are blank.

Connect persists ConversationParticipant.user_name at DM-create time. When
shizuha-id is briefly unreachable (or the client sends a placeholder), that
column is stored as ``User 735``. Conversation lists then prefer the stored
value and never re-resolve — even after ID/SocialProfile has a real name.

A name is a placeholder when it is blank or equals ``User <id>`` / ``user_<id>``
for that participant. Real labels come from SocialProfile, then shizuha-id
(first+last, username, email local-part).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r'^(?:user[_\s-])(\d+)$', re.IGNORECASE)


def is_placeholder_name(name, user_id=None) -> bool:
    """True when *name* is empty or the numeric fallback for *user_id*."""
    text = str(name or '').strip()
    if not text:
        return True
    match = _PLACEHOLDER_RE.match(text)
    if not match:
        return False
    if user_id is None:
        return True
    try:
        return int(match.group(1)) == int(user_id)
    except (TypeError, ValueError):
        return True


def label_from_identity(ident: dict | None, user_id=None) -> str:
    """Pick a display label from a shizuha-id / SocialProfile-like dict."""
    ident = ident or {}
    full = f"{ident.get('first_name', '')} {ident.get('last_name', '')}".strip()
    candidates = (
        ident.get('display_name'),
        full,
        ident.get('username'),
        ident.get('user_email') or ident.get('email'),
    )
    for raw in candidates:
        text = str(raw or '').strip()
        if not text:
            continue
        if '@' in text and ' ' not in text:
            text = text.split('@', 1)[0].strip()
        if text and not is_placeholder_name(text, user_id):
            return text
    return ''


def resolve_identity_users(user_ids: Iterable[int]) -> dict[int, dict]:
    """Batch-fetch shizuha-id rows keyed by user id. Best-effort; never raises."""
    ids = []
    seen = set()
    for raw in user_ids:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    if not ids:
        return {}
    from django.conf import settings
    import requests

    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    out: dict[int, dict] = {}
    headers = {'X-Internal-Service': 'shizuha-connect'}
    try:
        resp = requests.get(
            f"{id_base}/internal/users/",
            params={'ids': ','.join(str(i) for i in ids)},
            headers=headers,
            timeout=3,
        )
        payload = resp.json() if resp.status_code == 200 else None
    except Exception as exc:
        logger.warning("display-name identity lookup failed: %s", exc)
        payload = None
        resp = None
    if isinstance(payload, dict):
        rows = payload.get('results') or payload.get('users') or []
        if not rows and payload.get('id') is not None:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = row.get('id') if row.get('id') is not None else row.get('user_id')
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        out[uid] = row
    missing = [uid for uid in ids if uid not in out]
    if missing and (resp is None or resp.status_code in (401, 403, 404) or not rows):
        # ID-46: /internal/users/ list is auth-gated. The per-id route is the
        # in-cluster network-trust equivalent of this batch lookup.
        for uid in missing:
            try:
                one = requests.get(
                    f"{id_base}/internal/users/{uid}/",
                    headers=headers,
                    timeout=3,
                )
            except Exception as exc:
                logger.warning("display-name per-id lookup failed for %s: %s", uid, exc)
                continue
            if one.status_code != 200:
                continue
            row = one.json() if one.content else {}
            if isinstance(row, dict) and row:
                out[uid] = row
    return out


def cached_identity_users(context: dict | None, user_ids: Iterable[int]) -> dict[int, dict]:
    """Request-scoped cache so a conversation list does one ID round-trip."""
    context = context if isinstance(context, dict) else {}
    cache = context.setdefault('_id_user_cache', {})
    missing = [uid for uid in user_ids if uid not in cache]
    if missing:
        cache.update(resolve_identity_users(missing))
        for uid in missing:
            cache.setdefault(uid, {})
    return cache


def preferred_participant_label(participant, *, profiles=None, identities=None) -> str:
    """Best label for one ConversationParticipant, never inventing a new User-N."""
    user_id = getattr(participant, 'user_id', None)
    stored = getattr(participant, 'user_name', '') or ''
    if not is_placeholder_name(stored, user_id):
        return stored.strip()
    profiles = profiles or {}
    identities = identities or {}
    profile = profiles.get(user_id)
    if profile is not None:
        from_profile = label_from_identity(
            {
                'display_name': getattr(profile, 'display_name', ''),
                'username': getattr(profile, 'username', ''),
                'email': getattr(profile, 'user_email', ''),
            },
            user_id,
        )
        if from_profile:
            return from_profile
    from_id = label_from_identity(identities.get(user_id) or {}, user_id)
    if from_id:
        return from_id
    email = getattr(participant, 'user_email', '') or ''
    from_email = label_from_identity({'email': email}, user_id)
    if from_email:
        return from_email
    return stored.strip() or (f'User {user_id}' if user_id is not None else '')

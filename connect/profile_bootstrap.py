"""Shared SocialProfile bootstrap helpers for authenticated user endpoints."""

from django.db import IntegrityError, transaction

from connect.models import SocialProfile


def _display_name(user):
    get_full_name = getattr(user, 'get_full_name', None)
    full_name = get_full_name() if callable(get_full_name) else ''
    return full_name or getattr(user, 'username', '') or getattr(user, 'email', '') or f'user-{getattr(user, "id", "unknown")}'


def _base_username(user):
    raw = (getattr(user, 'username', '') or '').strip() or (getattr(user, 'email', '').split('@')[0] or '')
    raw = ''.join(c.lower() if c.isalnum() or c in {'_', '-'} else '-' for c in raw).strip('-_')
    return (raw or f'user-{getattr(user, "id", "unknown")}')[:40]


def get_or_create_social_profile(user):
    """Return a Connect SocialProfile for a Shizuha ID user without 500s.

    `SocialProfile.user_id` and `username` are both unique. Older code used the
    Shizuha ID username as a default inside `get_or_create(user_id=...)`; if a
    different profile already owned that username, valid signed-in users hit an
    IntegrityError and `/me/`/onboarding returned a generic 500. We keep the
    user_id lookup canonical and generate a deterministic unique username.
    """
    existing = SocialProfile.objects.filter(user_id=user.id).first()
    if existing:
        return existing

    base = _base_username(user)
    candidates = [base, f'{base}-{user.id}'[:50]]
    candidates.extend(f'{base[:44]}-{user.id}-{i}'[:50] for i in range(2, 10))
    defaults = {
        'user_email': getattr(user, 'email', f'user_{user.id}@shizuha.id'),
        'display_name': _display_name(user),
        'onboarding_state': {},
    }
    last_exc = None
    for username in candidates:
        try:
            with transaction.atomic():
                profile, _ = SocialProfile.objects.get_or_create(
                    user_id=user.id,
                    defaults={**defaults, 'username': username},
                )
                return profile
        except IntegrityError as exc:
            last_exc = exc
            # Another profile owns that username; retry with a suffixed candidate.
            continue
    raise last_exc or IntegrityError('Could not allocate a unique Connect username')

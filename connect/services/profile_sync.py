"""
CON-52: ensure every messageable Shizuha identity has a Connect SocialProfile.

Agents are resolved at the shizuha-id / messaging layer by username (so DMs via
``message_user`` work), but they never visit the Connect UI, so they never get a
``SocialProfile``. People-search queries ``SocialProfile.search_vector`` and
conversation participant rendering reads ``SocialProfile.display_name/username``
— so an agent with no profile is invisible to search AND renders blank in
conversation lists. This module idempotently upserts a profile for any user_id,
resolving display fields from shizuha-id when not supplied.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def resolve_identity(user_id):
    """Return {username, display_name, email} for a user from shizuha-id, or {}."""
    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    try:
        resp = requests.get(
            f"{id_base}/internal/users/",
            params={'ids': str(user_id)},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=3,
        )
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict):
                rows = payload.get('results') or payload.get('users') or []
                if not rows and payload.get('id') is not None:
                    rows = [payload]
            else:
                rows = payload or []
            if rows:
                d = rows[0]
                from connect.services.display_names import label_from_identity

                uname = d.get('username') or ''
                display = label_from_identity(d, user_id) or uname
                return {
                    'username': uname,
                    'display_name': display,
                    'email': d.get('email', '') or '',
                }
        # List endpoint is auth-gated (ID-46). Fall back to the per-id
        # network-trust lookup used by other Connect paths.
        if resp.status_code in (401, 403):
            one = requests.get(
                f"{id_base}/internal/users/{user_id}/",
                headers={'X-Internal-Service': 'shizuha-connect'},
                timeout=3,
            )
            if one.status_code == 200:
                d = one.json() or {}
                from connect.services.display_names import label_from_identity
                uname = d.get('username') or ''
                display = label_from_identity(d, user_id) or uname
                return {
                    'username': uname,
                    'display_name': display,
                    'email': d.get('email', '') or '',
                }
    except Exception as e:  # network/shizuha-id hiccup — best-effort
        logger.warning("CON-52 identity resolve failed for user %s: %s", user_id, e)
    return {}


def ensure_social_profile(user_id, *, username='', display_name='', email='', resolve=True):
    """
    Idempotently upsert a SocialProfile for ``user_id``.

    Best-effort: returns the profile or None, and never raises into the caller
    (it runs from a post_save signal / Celery task / management command and must
    not break message delivery).

    - Creates a profile if missing.
    - Heals blank ``display_name``/``user_email`` on an existing row when better
      values become available (e.g. a row seeded with placeholders before
      identity resolution).
    """
    from connect.models import SocialProfile

    try:
        if resolve and (not username or not display_name or not email):
            ident = resolve_identity(user_id)
            username = username or ident.get('username', '')
            display_name = display_name or ident.get('display_name', '')
            email = email or ident.get('email', '')

        # username is unique + NOT NULL on the model and is what makes a profile
        # searchable. Without one we cannot write a valid, useful row — skip.
        if not username:
            return None
        display_name = display_name or username

        profile, created = SocialProfile.objects.get_or_create(
            user_id=user_id,
            defaults={
                'username': username,
                'display_name': display_name,
                'user_email': email,
            },
        )
        if created:
            return profile

        from connect.services.display_names import is_placeholder_name

        dirty_fields = []
        if display_name and (
            not profile.display_name
            or is_placeholder_name(profile.display_name, user_id)
        ):
            profile.display_name = display_name
            dirty_fields.append('display_name')
        if not profile.user_email and email:
            profile.user_email = email
            dirty_fields.append('user_email')
        if dirty_fields:
            dirty_fields.append('updated_at')
            profile.save(update_fields=dirty_fields)
        return profile
    except Exception as e:
        logger.warning("CON-52 ensure_social_profile failed for user %s: %s", user_id, e)
        return None

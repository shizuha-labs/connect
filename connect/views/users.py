"""
User identity lookup (CON-2).

Agents only ever see inbound senders as ``[username]`` prefixes, so a username is
what they have — but ``connect_create_conversation`` needs the numeric Shizuha ID
``user_id`` to populate ``participant_ids`` (ConversationParticipant is keyed by
integer ``user_id``; see messaging.ConversationViewSet.create). Before this
endpoint the only way to resolve a username was an out-of-band ``kubectl exec`` +
Django ORM lookup (as ichi did for PLAT-241), which agents cannot repeat.

This resolves against the Shizuha ID service (not the local SocialProfile table),
so it works for any registered user — human or agent — INCLUDING agents that have
no Connect social profile.

NB: the CON-2 title says "uuid", but the identity service has no per-user UUID —
``/internal/users/by-username/`` returns an integer ``id``, and conversation
participants are keyed by that integer. So the canonical identifier returned here
is ``user_id`` (int). Unblocks PLAT-239 (agent group conversation creation).
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class UserByUsernameView(APIView):
    """GET /api/users/by-username/<username>/uuid — resolve username -> {username, user_id}."""

    permission_classes = [IsAuthenticated]

    def get(self, request, username):
        # CON-480: tolerate the @agents.shizuha.io principal display form.
        from connect.services.agent_identity import normalize_username
        username = normalize_username(username)
        if not username:
            return Response({'error': 'username is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        shizuha_id_url = getattr(
            settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api',
        ).rstrip('/')
        lookup_url = f'{shizuha_id_url}/internal/users/by-username/'

        try:
            import requests  # local import — Celery workers shouldn't pay for it
            resp = requests.get(lookup_url, params={'username': username}, timeout=5)
        except Exception as e:
            logger.exception("CON-2 identity lookup failed for %s", username)
            return Response({'error': f'Identity lookup failed: {e}'},
                            status=status.HTTP_502_BAD_GATEWAY)

        if resp.status_code == 404:
            return Response({'error': f'No user found with username {username}'},
                            status=status.HTTP_404_NOT_FOUND)
        if not resp.ok:
            return Response({'error': f'Identity service returned {resp.status_code}'},
                            status=status.HTTP_502_BAD_GATEWAY)

        try:
            data = resp.json()
        except ValueError:
            return Response({'error': 'Identity service returned non-JSON response'},
                            status=status.HTTP_502_BAD_GATEWAY)

        try:
            user_id = int(data.get('id') or data.get('user_id'))
        except (TypeError, ValueError):
            return Response({'error': 'Identity service response missing user id'},
                            status=status.HTTP_502_BAD_GATEWAY)

        # `id` is included as an alias so callers can read either key, matching the
        # `recipient.get('id') or recipient.get('user_id')` convention used in
        # messaging.py's recipient resolution.
        return Response({
            'username': data.get('username') or username,
            'user_id': user_id,
            'id': user_id,
        }, status=status.HTTP_200_OK)

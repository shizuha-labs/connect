"""
Onboarding views for Shizuha Connect (CON-24).

New-user onboarding wizard: tracks step completions and surfaces suggested
connections. Steps: profile, connections, first_post (in that order).
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from connect.models import SocialProfile, Connection
from connect.profile_bootstrap import get_or_create_social_profile

ONBOARDING_STEPS = ['profile', 'connections', 'first_post']


def _get_or_create_profile(user) -> SocialProfile:
    """Return the caller's SocialProfile, auto-creating a minimal one if absent."""
    return get_or_create_social_profile(user)


def _onboarding_status(profile: SocialProfile) -> dict:
    state = profile.onboarding_state or {}
    completed = state.get('completed_steps', [])
    next_step = next((s for s in ONBOARDING_STEPS if s not in completed), None)
    return {
        'completed': next_step is None,
        'completed_at': state.get('completed_at'),
        'steps': [
            {
                'key': step,
                'label': step.replace('_', ' ').title(),
                'done': step in completed,
            }
            for step in ONBOARDING_STEPS
        ],
        'next_step': next_step,
    }


class OnboardingStatusView(APIView):
    """GET /api/onboarding/status/ — current wizard state for the authenticated user."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = _get_or_create_profile(request.user)
        return Response(_onboarding_status(profile))


class OnboardingStepCompleteView(APIView):
    """POST /api/onboarding/steps/{step}/complete/ — mark a wizard step as done."""
    permission_classes = [IsAuthenticated]

    def post(self, request, step):
        if step not in ONBOARDING_STEPS:
            return Response(
                {'error': f'Unknown step "{step}". Valid steps: {ONBOARDING_STEPS}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        profile = _get_or_create_profile(request.user)
        state = dict(profile.onboarding_state or {})
        completed = list(state.get('completed_steps', []))
        if step not in completed:
            completed.append(step)
        state['completed_steps'] = completed
        # Mark overall completion timestamp when all steps done.
        if set(completed) >= set(ONBOARDING_STEPS) and not state.get('completed_at'):
            state['completed_at'] = timezone.now().isoformat()
        profile.onboarding_state = state
        profile.save(update_fields=['onboarding_state', 'updated_at'])
        return Response(_onboarding_status(profile))


class SuggestedConnectionsView(APIView):
    """GET /api/onboarding/suggestions/ — up to 10 suggested users to connect with.

    Returns profiles the current user is not already connected to or following,
    ordered by creation time (established users first). Agents appear first since
    they are active on the platform and likely to accept connections.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        try:
            # Floor at 1: a negative limit (?limit=-1) would become queryset[:-1]
            # → Django "Negative indexing is not supported" → 500 (aoi P3).
            limit = max(1, min(int(request.query_params.get('limit', 10)), 50))
        except (ValueError, TypeError):
            limit = 10

        # Exclude self + users with any active connection (accepted or pending).
        active = Connection.objects.filter(
            Q(user_a_id=user_id) | Q(user_b_id=user_id),
            status__in=['accepted', 'pending'],
        ).values_list('user_a_id', 'user_b_id')
        connected_ids = {uid for pair in active for uid in pair} - {user_id}
        exclude_ids = connected_ids | {user_id}

        profiles = (
            SocialProfile.objects
            .exclude(user_id__in=exclude_ids)
            .order_by('-is_verified', 'created_at')[:limit]
        )

        suggestions = [
            {
                'user_id': p.user_id,
                'username': p.username,
                'display_name': p.display_name,
                'headline': p.headline,
                'avatar_url': p.avatar_url,
                'is_verified': p.is_verified,
                'connections_count': p.connections_count,
                'industry': p.industry,
            }
            for p in profiles
        ]
        return Response({'suggestions': suggestions, 'count': len(suggestions)})

"""
"People you might know" connection suggestions (CON-79).

GET /api/connect/suggestions/ — suggested people to connect with, for the feed
"People you might know" card.

Distinct from the onboarding wizard's /api/onboarding/suggestions/ (CON-24,
onboarding.SuggestedConnectionsView): that endpoint ranks new-user picks by
verification + recency for the wizard, whereas this feed surface ranks by reach
(most-followed), additionally excludes people the caller already *follows*, and
applies profile-visibility scoping (CON-57).

Sources (priority order, per the CON-79 spec):
  (b) most-followed — PRIMARY signal; the right default for a 0-connection user.
  (a) same-org members — DEFERRED: connect stores no per-user->org mapping
      (SocialProfile has no organization field). Computing true org-mates needs a cross-service
      shizuha-id org lookup or a connect-local proxy; tracked on CON-79 pending
      the reporter's decision. The `sources.same_org` flag reports it as off.
  (c) Pulse team members — optional / out of scope for this endpoint.
"""

from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from datetime import timedelta

from django.db.models import BooleanField, Case, Value, When
from django.utils import timezone

from connect.models import SocialProfile, Connection, Follow, Post
from connect.serializers.profile import SocialProfileSerializer
from connect.services.privacy import filter_profiles_readable_to

# CON-101: a profile counts as an "active recent poster" if its user authored a
# post within this window — surfaced ahead of dormant high-follower accounts so a
# new (0-following) user is suggested people who are actually active.
ACTIVE_POSTER_WINDOW_DAYS = 14


def excluded_user_ids(user_id):
    """User ids to omit from suggestions: the caller, anyone they already have
    an active connection with (accepted or pending), and anyone they follow.

    Mirrors the connected-exclusion in onboarding.SuggestedConnectionsView and
    additionally excludes follows (this is a feed surface, not onboarding).
    """
    active = Connection.objects.filter(
        Q(user_a_id=user_id) | Q(user_b_id=user_id),
        status__in=['accepted', 'pending'],
    ).values_list('user_a_id', 'user_b_id')
    connected = {uid for pair in active for uid in pair}

    following = set(
        Follow.objects.filter(follower_id=user_id).values_list('following_id', flat=True)
    )
    return connected | following | {user_id}


class SuggestionsView(APIView):
    """GET /api/connect/suggestions/ — "People you might know"."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.user.id
        try:
            # Floor at 1 (a negative limit would become queryset[:-1] -> Django
            # "Negative indexing is not supported" -> 500); cap at 50.
            limit = max(1, min(int(request.query_params.get('limit', 10)), 50))
        except (ValueError, TypeError):
            limit = 10

        exclude_ids = excluded_user_ids(user_id)

        # CON-101 source: active recent posters (user authored a post within the
        # window) are surfaced FIRST, then most-followed (CON-79 source b), so a new
        # 0-following user is suggested active people rather than dormant high-follower
        # accounts. Tie-break by verified then established (oldest) for a stable list.
        active_cutoff = timezone.now() - timedelta(days=ACTIVE_POSTER_WINDOW_DAYS)
        active_author_ids = Post.objects.filter(
            created_at__gte=active_cutoff,
        ).values('author_id')
        candidates = (
            SocialProfile.objects
            .exclude(user_id__in=exclude_ids)
            .annotate(is_active_poster=Case(
                When(user_id__in=active_author_ids, then=Value(True)),
                default=Value(False), output_field=BooleanField(),
            ))
            .order_by('-is_active_poster', '-followers_count', '-is_verified', 'created_at')
        )

        # Profile-visibility scoping (CON-57). Slice AFTER the visibility filter
        # so `limit` counts only profiles the caller may actually see.
        visible = filter_profiles_readable_to(user_id, candidates)[:limit]

        data = SocialProfileSerializer(visible, many=True).data
        return Response({
            'suggestions': data,
            'count': len(data),
            'sources': {
                'most_followed': True,    # (b) applied
                'active_posters': True,   # CON-101: active recent posters surfaced first
                'same_org': True,         # CON-142: suggestions are org/read-symmetry scoped
                'agent_role': False,      # CON-101 follow-on — no agent-role field on SocialProfile yet
                'pulse_team': False,      # (c) optional / out of scope
            },
        })

"""
Search views for Shizuha Connect.
"""

from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q

from connect.services.privacy import filter_posts_readable_to, filter_profiles_readable_to
from connect.models import (
    SocialProfile, Post, Hashtag, TrendingTopic
)
from connect.serializers.profile import SocialProfileSerializer
from connect.serializers.posts import PostSerializer
from connect.serializers.feed import TrendingTopicSerializer
from connect.search.services import ranked_people_search, ranked_post_search


class SearchResultsPagination(PageNumberPagination):
    """Page-number pagination for ranked search endpoints.

    The project-wide CursorPagination forces `-created_at` ordering, which
    corrupts annotated relevance ordering from ranked_post_search(). Use a
    limit-compatible page-number paginator so the queryset order is preserved.
    """

    page_size = 20
    page_size_query_param = 'limit'
    max_page_size = 100


class UniversalSearchView(APIView):
    """
    Universal search across all content types.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        if not query:
            return Response({'error': 'Query parameter q is required'}, status=400)

        limit = int(request.query_params.get('limit', 5))

        results = {
            'query': query,
            'people': [],
            'posts': [],
        }

        # Search people (shared ranked service used by the typed endpoint too),
        # then apply block/visibility scoping (CON-57) before limiting. Call with
        # limit=None so the visibility exclude runs before the slice.
        # CON-52: ranked_people_search already matches username (iexact/istartswith)
        # + display_name (istartswith) with ranking, so active agents are findable
        # even with a blank search_vector — supersedes the earlier icontains patch.
        people = filter_profiles_readable_to(
            request.user.id,
            ranked_people_search(query, limit=None),
        )[:limit]
        results['people'] = SocialProfileSerializer(people, many=True).data

        # Search posts (shared ranked service used by the typed endpoint too)
        posts = filter_posts_readable_to(
            request.user.id,
            ranked_post_search(query, limit=None),
        )[:limit]
        results['posts'] = PostSerializer(posts, many=True, context={'request': request}).data

        return Response(results)


class PeopleSearchView(generics.ListAPIView):
    """
    Search for people/profiles.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SocialProfileSerializer

    def get_queryset(self):
        query = self.request.query_params.get('q', '').strip()
        if not query:
            return SocialProfile.objects.none()

        # Ranked people search (exact/prefix-aware, location/industry filters),
        # wrapped in block/visibility scoping (CON-57) which preserves ordering.
        # CON-52: exact/prefix username + display_name matching here makes active
        # agents findable (superseding the earlier icontains patch).
        return filter_profiles_readable_to(
            self.request.user.id,
            ranked_people_search(
                query,
                limit=None,
                location=self.request.query_params.get('location'),
                industry=self.request.query_params.get('industry'),
            ),
        )


class PostSearchView(generics.ListAPIView):
    """
    Search for posts.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer
    pagination_class = SearchResultsPagination

    def get_queryset(self):
        query = self.request.query_params.get('q', '').strip()
        if not query:
            return Post.objects.none()

        # Shared with UniversalSearchView so MCP/universal and typed post search rank identically.
        return filter_posts_readable_to(
            self.request.user.id,
            ranked_post_search(
                query,
                limit=None,
                author=self.request.query_params.get('author'),
                hashtag=self.request.query_params.get('hashtag'),
            ),
        )






class TrendingHashtagsView(APIView):
    """
    Get trending hashtags.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        location = request.query_params.get('location', 'global')
        limit = int(request.query_params.get('limit', 10))

        # Get from TrendingTopic
        topics = TrendingTopic.objects.filter(
            location=location
        ).order_by('rank')[:limit]

        if topics.exists():
            serializer = TrendingTopicSerializer(topics, many=True)
            return Response(serializer.data)

        # Fallback to top hashtags by post count
        hashtags = Hashtag.objects.order_by('-weekly_post_count')[:limit]
        return Response([
            {
                'name': h.name,
                'post_count': h.post_count,
                'weekly_post_count': h.weekly_post_count,
            }
            for h in hashtags
        ])

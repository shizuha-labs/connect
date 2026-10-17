"""
Feed views for Shizuha Connect.
"""

from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.db.models import Q
from django.utils import timezone

from connect.models import Post, Follow, FeedItem, TrendingTopic
from connect.serializers.posts import PostSerializer
from connect.serializers.feed import TrendingTopicSerializer
from connect.services.privacy import filter_posts_readable_to


@api_view(['GET'])
@permission_classes([AllowAny])
def health_check(request):
    """Health check endpoint."""
    return Response({'status': 'healthy', 'service': 'shizuha-connect'})


class HomeFeedView(generics.ListAPIView):
    """
    Home feed with algorithmic ranking.
    Shows posts from followed users plus trending content.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer

    def get_queryset(self):
        user_id = self.request.user.id

        # Get users the current user follows
        following_ids = list(
            Follow.objects.filter(follower_id=user_id)
            .values_list('following_id', flat=True)
        )

        # Include own posts
        following_ids.append(user_id)

        # Get posts from followed users and public posts
        queryset = Post.objects.filter(
            Q(author_id__in=following_ids) | Q(visibility='public')
        ).distinct()

        # Order by recency and engagement (simple algorithm)
        # In production, this would use FeedItem materialized view
        return filter_posts_readable_to(user_id, queryset).order_by('-created_at')


class FollowingFeedView(generics.ListAPIView):
    """
    Chronological feed showing only posts from followed users.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer

    def get_queryset(self):
        user_id = self.request.user.id

        # Get users the current user follows
        following_ids = list(
            Follow.objects.filter(follower_id=user_id)
            .values_list('following_id', flat=True)
        )

        # Get posts from followed users only
        queryset = Post.objects.filter(
            author_id__in=following_ids,
            visibility__in=['public', 'followers']
        )
        return filter_posts_readable_to(user_id, queryset).order_by('-created_at')


class TrendingFeedView(generics.ListAPIView):
    """
    Trending posts across the platform.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer

    def get_queryset(self):
        # Get posts with high engagement from last 24 hours
        since = timezone.now() - timezone.timedelta(hours=24)

        # Note: Don't slice here - let pagination handle the limit
        queryset = Post.objects.filter(
            created_at__gte=since,
            visibility='public'
        )
        return filter_posts_readable_to(self.request.user.id, queryset).order_by('-likes_count', '-reposts_count', '-comments_count', '-created_at')

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)

        # Add trending topics to response
        trending_topics = TrendingTopic.objects.filter(
            location='global'
        ).order_by('rank')[:10]

        # If response.data is a dict (paginated), add trending_topics to it
        # If it's a list (unpaginated), wrap in dict
        if isinstance(response.data, dict):
            response.data['trending_topics'] = TrendingTopicSerializer(
                trending_topics, many=True
            ).data
        else:
            response.data = {
                'results': response.data,
                'trending_topics': TrendingTopicSerializer(
                    trending_topics, many=True
                ).data
            }

        return response

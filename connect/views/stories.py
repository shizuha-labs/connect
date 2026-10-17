"""
Stories views for Shizuha Connect.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db import models

from connect.models import Connection, Story, StoryView, StoryReaction, Follow
from connect.serializers.stories import (
    StorySerializer, StoryCreateSerializer, StoryViewSerializer, StoryReactionSerializer
)
from connect.services.privacy import users_share_org


def _users_connected(user_a_id, user_b_id):
    if user_a_id == user_b_id:
        return True
    a, b = sorted((int(user_a_id), int(user_b_id)))
    return Connection.objects.filter(user_a_id=a, user_b_id=b, status='accepted').exists()


def _can_access_story(user, story):
    """CON-150: unilateral follow is not enough for story access.

    Stories are visible to the author, same-org users, or accepted connections.
    This keeps ephemeral content behind a mutual/org relationship instead of an
    attacker following a cross-org author and viewing/reacting to every story.
    """
    if story.author_id == user.id:
        return True
    if _users_connected(user.id, story.author_id):
        return True
    if story.visibility == 'connections':
        return False
    return users_share_org(user.id, story.author_id)


class StoryViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing stories.
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Get active stories from followed users that the requester is allowed
        # to see. A unilateral follow is only a discovery signal; CON-150 gates
        # actual read/react access on same-org or accepted-connection scope.
        following_ids = list(
            Follow.objects.filter(follower_id=self.request.user.id)
            .values_list('following_id', flat=True)
        )
        following_ids.append(self.request.user.id)  # Include own stories
        candidate = Story.objects.filter(
            author_id__in=following_ids,
            expires_at__gt=timezone.now()
        ).order_by('-created_at')
        allowed_story_ids = [
            story.id
            for story in candidate
            if _can_access_story(self.request.user, story)
        ]
        return candidate.filter(id__in=allowed_story_ids)

    def get_serializer_class(self):
        if self.action == 'create':
            return StoryCreateSerializer
        return StorySerializer

    def perform_create(self, serializer):
        serializer.save(
            author_id=self.request.user.id,
            author_email=self.request.user.email,
            author_username=self.request.user.username
        )

    def perform_update(self, serializer):
        if serializer.instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only modify your own stories.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only delete your own stories.")
        instance.delete()

    @action(detail=True, methods=['post'])
    def view(self, request, pk=None):
        """Record a view on the story."""
        story = self.get_object()

        view, created = StoryView.objects.get_or_create(
            story=story,
            viewer_id=request.user.id,
            defaults={'viewer_email': request.user.email}
        )

        if created:
            Story.objects.filter(pk=story.pk).update(
                views_count=models.F('views_count') + 1
            )

        return Response({
            'status': 'viewed' if created else 'already_viewed'
        })

    @action(detail=True, methods=['get'])
    def views(self, request, pk=None):
        """Get story views (author only)."""
        story = self.get_object()

        if story.author_id != request.user.id:
            return Response(
                {'error': 'Not authorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        views = StoryView.objects.filter(story=story).order_by('-viewed_at')
        page = self.paginate_queryset(views)
        if page is not None:
            serializer = StoryViewSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = StoryViewSerializer(views, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def react(self, request, pk=None):
        """React to a story.

        Accepts the documented ``reaction_type`` param (like/love/wow/haha,
        default 'like') used by the MCP ``connect_react_to_story`` tool, and
        keeps backward compatibility with the legacy ``reaction`` (emoji) field.
        """
        story = self.get_object()
        reaction = request.data.get('reaction_type') or request.data.get('reaction')
        message = request.data.get('message', '')

        if not reaction:
            return Response(
                {'error': 'Reaction is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        story_reaction = StoryReaction.objects.create(
            story=story,
            user_id=request.user.id,
            user_email=request.user.email,
            reaction=reaction,
            message=message
        )

        return Response(
            StoryReactionSerializer(story_reaction).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['get'])
    def reactions(self, request, pk=None):
        """Get story reactions (author only)."""
        story = self.get_object()

        if story.author_id != request.user.id:
            return Response(
                {'error': 'Not authorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        reactions = StoryReaction.objects.filter(story=story).order_by('-created_at')
        serializer = StoryReactionSerializer(reactions, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def my_stories(self, request):
        """Get current user's active stories."""
        stories = Story.objects.filter(
            author_id=request.user.id,
            expires_at__gt=timezone.now()
        ).order_by('-created_at')

        serializer = StorySerializer(stories, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def archive(self, request):
        """Get current user's expired stories (archive)."""
        stories = Story.objects.filter(
            author_id=request.user.id,
            expires_at__lte=timezone.now()
        ).order_by('-created_at')

        page = self.paginate_queryset(stories)
        if page is not None:
            serializer = StorySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = StorySerializer(stories, many=True)
        return Response(serializer.data)

"""
Profile views for Shizuha Connect.
"""

from rest_framework import viewsets, status, generics, filters
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from connect.profile_bootstrap import get_or_create_social_profile
from connect.services.privacy import filter_posts_readable_to, filter_profiles_readable_to
from connect.models import (
    SocialProfile, Skill, UserSkill, Endorsement,
    Experience, Education, Post, Bookmark
)
from connect.serializers.profile import (
    SocialProfileSerializer, SocialProfileCreateSerializer, SocialProfileUpdateSerializer,
    SkillSerializer, UserSkillSerializer, EndorsementSerializer,
    ExperienceSerializer, EducationSerializer,
)
from connect.serializers.posts import PostSerializer, BookmarkSerializer
from connect.pagination import StandardResultsSetPagination


class ProfileViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing social profiles.
    """
    permission_classes = [IsAuthenticated]
    lookup_field = 'username'
    # CON-342: the default DRF lookup regex [^/.]+ rejects usernames containing
    # dots (e.g. nagi.shizuha), so /profiles/<username>/... fell through to an
    # HTML 404 instead of a typed API 404. Allow Django's standard username
    # charset (letters/digits/@/./+/-) so every username form reaches the view.
    lookup_value_regex = r'[\w.@+-]+'

    def get_queryset(self):
        return filter_profiles_readable_to(self.request.user.id, SocialProfile.objects.all())

    def get_serializer_class(self):
        if self.action == 'create':
            return SocialProfileCreateSerializer
        if self.action in ['update', 'partial_update']:
            return SocialProfileUpdateSerializer
        return SocialProfileSerializer

    def get_object(self):
        username = self.kwargs.get('username')
        return get_object_or_404(self.get_queryset(), username=username)

    def perform_update(self, serializer):
        # CON-142 P1.1: ProfileViewSet resolves by URL username against a
        # visibility-filtered (not owner-scoped) queryset and had no write guard,
        # so any authenticated user (any org) could PUT/PATCH another user's
        # profile. Enforce object ownership server-side.
        if serializer.instance.user_id != self.request.user.id:
            raise PermissionDenied("You can only modify your own profile.")
        serializer.save()

    def perform_destroy(self, instance):
        # CON-142 P1.1: same IDOR on delete.
        if instance.user_id != self.request.user.id:
            raise PermissionDenied("You can only delete your own profile.")
        instance.delete()

    @action(detail=True, methods=['get'])
    def posts(self, request, username=None):
        """Get posts by user."""
        profile = self.get_object()
        posts = filter_posts_readable_to(
            request.user.id,
            Post.objects.filter(author_id=profile.user_id),
        ).order_by('-created_at')
        page = self.paginate_queryset(posts)
        if page is not None:
            serializer = PostSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        serializer = PostSerializer(posts, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def followers(self, request, username=None):
        """Get followers of user."""
        from connect.models import Follow
        profile = self.get_object()
        follows = Follow.objects.filter(following_id=profile.user_id).select_related()
        follower_ids = follows.values_list('follower_id', flat=True)
        followers = filter_profiles_readable_to(request.user.id, SocialProfile.objects.filter(user_id__in=follower_ids))
        page = self.paginate_queryset(followers)
        if page is not None:
            serializer = SocialProfileSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = SocialProfileSerializer(followers, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def following(self, request, username=None):
        """Get users followed by this user."""
        from connect.models import Follow
        profile = self.get_object()
        follows = Follow.objects.filter(follower_id=profile.user_id)
        following_ids = follows.values_list('following_id', flat=True)
        following = filter_profiles_readable_to(request.user.id, SocialProfile.objects.filter(user_id__in=following_ids))
        page = self.paginate_queryset(following)
        if page is not None:
            serializer = SocialProfileSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = SocialProfileSerializer(following, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def skills(self, request, username=None):
        """Get user's skills."""
        profile = self.get_object()
        skills = UserSkill.objects.filter(profile=profile).select_related('skill')
        serializer = UserSkillSerializer(skills, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def experience(self, request, username=None):
        """Get user's work experience."""
        profile = self.get_object()
        experiences = Experience.objects.filter(profile=profile)
        serializer = ExperienceSerializer(experiences, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def education(self, request, username=None):
        """Get user's education."""
        profile = self.get_object()
        education = Education.objects.filter(profile=profile)
        serializer = EducationSerializer(education, many=True)
        return Response(serializer.data)


class MyProfileView(generics.RetrieveUpdateAPIView):
    """
    Get or update the current user's profile.
    """
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return SocialProfileUpdateSerializer
        return SocialProfileSerializer

    def get_object(self):
        return get_or_create_social_profile(self.request.user)


class MyPostsView(generics.ListAPIView):
    """
    Get current user's posts.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer

    def get_queryset(self):
        return Post.objects.filter(author_id=self.request.user.id).order_by('-created_at')


class MyBookmarksView(generics.ListAPIView):
    """
    Get current user's bookmarks.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookmarkSerializer

    def get_queryset(self):
        return Bookmark.objects.filter(user_id=self.request.user.id).select_related('post')


class SkillViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing skills catalog.

    The global Connect pagination class orders by ``-created_at`` for feed-like
    models. ``Skill`` is a small catalog model without that field, so use page
    number pagination and explicit search/ordering here instead of letting DRF
    cursor pagination raise FieldError on /api/skills/.
    """
    queryset = Skill.objects.all().order_by('name')
    serializer_class = SkillSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'slug'
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'slug', 'category']
    ordering_fields = ['name', 'slug', 'category']
    ordering = ['name']


class ExperienceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing work experience.

    CON-256: default CreatedAtCursorPagination orders by ``-created_at``, but
    Experience has no created_at (orders by is_current/start_date). Without an
    explicit page-number pagination class, GET /api/experience/ raises
    FieldError → HTTP 500. Same pattern as SkillViewSet.
    """
    serializer_class = ExperienceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['start_date', 'end_date', 'is_current', 'title', 'company_name']
    ordering = ['-is_current', '-start_date']

    def get_queryset(self):
        return Experience.objects.filter(profile__user_id=self.request.user.id)

    def perform_create(self, serializer):
        profile = get_object_or_404(SocialProfile, user_id=self.request.user.id)
        serializer.save(profile=profile)


class EducationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing education.

    CON-256: default CreatedAtCursorPagination orders by ``-created_at``, but
    Education has no created_at (orders by end_date). Without an explicit
    page-number pagination class, GET /api/education/ raises FieldError →
    HTTP 500. Same pattern as SkillViewSet / ExperienceViewSet.
    """
    serializer_class = EducationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ['start_date', 'end_date', 'school_name', 'degree']
    ordering = ['-end_date']

    def get_queryset(self):
        return Education.objects.filter(profile__user_id=self.request.user.id)

    def perform_create(self, serializer):
        profile = get_object_or_404(SocialProfile, user_id=self.request.user.id)
        serializer.save(profile=profile)

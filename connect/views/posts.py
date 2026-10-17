"""
Post views for Shizuha Connect.
"""

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone

from connect.models import (
    Post, PostMedia, Reaction, Comment, CommentLike, Bookmark
)
from connect.models.profile import SocialProfile
from connect.serializers.posts import (
    PostSerializer, PostCreateSerializer, PostUpdateSerializer,
    ReactionSerializer, CommentSerializer, CommentCreateSerializer,
    BookmarkSerializer,
)
from connect.services.privacy import filter_posts_readable_to


def _author_fields(user):
    """Return denormalized author fields for Post creation."""
    display_name = (
        SocialProfile.objects
        .filter(user_id=user.id)
        .values_list('display_name', flat=True)
        .first()
    ) or ''
    return {
        'author_id': user.id,
        'author_email': user.email,
        'author_username': user.username,
        'author_display_name': display_name,
    }


class PostViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing posts.
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Do not pre-filter visibility='public'. Detail/update/delete/repost
        # all resolve through this queryset; a followers post the author can
        # already see on /me/posts/ must not 404 here (CON-229).
        # filter_posts_readable_to is the visibility gate.
        queryset = Post.objects.all()
        # Filter by author if specified
        author_id = self.request.query_params.get('author_id')
        if author_id:
            queryset = queryset.filter(author_id=author_id)
        # Filter by post type
        post_type = self.request.query_params.get('type')
        if post_type:
            queryset = queryset.filter(post_type=post_type)
        # Filter by hashtag (CON-78): match the PostHashtag M2M, consistent with
        # the post-search service (connect/search/services.py). Tolerate a
        # leading '#'; hashtag names are stored lowercased by the extraction
        # signal, so match case-insensitively. distinct() guards against row
        # duplication from the M2M join.
        hashtag = self.request.query_params.get('hashtag')
        if hashtag:
            queryset = queryset.filter(
                hashtag_links__hashtag__name__iexact=hashtag.strip().lstrip('#')
            ).distinct()
        return filter_posts_readable_to(self.request.user.id, queryset).order_by('-created_at')

    def get_serializer_class(self):
        if self.action == 'create':
            return PostCreateSerializer
        if self.action in ['update', 'partial_update']:
            return PostUpdateSerializer
        return PostSerializer

    def perform_create(self, serializer):
        serializer.save(**_author_fields(self.request.user))

    def perform_update(self, serializer):
        # CON-142 P1.4: get_queryset is scoped to visibility='public', not the
        # author, so update/partial_update would otherwise let ANY authenticated
        # user (any org) edit/re-visibility another user's public post. Enforce
        # object ownership server-side.
        if serializer.instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only modify your own posts.")
        serializer.save()

    def perform_destroy(self, instance):
        # CON-142 P1.4: same IDOR on delete.
        if instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only delete your own posts.")
        instance.delete()

    @action(detail=True, methods=['post'])
    def react(self, request, pk=None):
        """Add reaction to post."""
        post = self.get_object()
        reaction_type = request.data.get('reaction_type', 'like')

        reaction, created = Reaction.objects.update_or_create(
            post=post,
            user_id=request.user.id,
            defaults={
                'reaction_type': reaction_type,
                'user_email': request.user.email,
            }
        )

        return Response({
            'status': 'reacted' if created else 'updated',
            'reaction_type': reaction.reaction_type
        })

    @action(detail=True, methods=['post'])
    def unreact(self, request, pk=None):
        """Remove reaction from post."""
        post = self.get_object()
        deleted, _ = Reaction.objects.filter(
            post=post,
            user_id=request.user.id
        ).delete()

        return Response({
            'status': 'removed' if deleted else 'not_found'
        })

    @action(detail=True, methods=['post'])
    def repost(self, request, pk=None):
        """Repost a post."""
        original_post = self.get_object()

        # Check if already reposted
        existing = Post.objects.filter(
            author_id=request.user.id,
            original_post=original_post,
            post_type='repost'
        ).exists()

        if existing:
            return Response(
                {'error': 'Already reposted'},
                status=status.HTTP_400_BAD_REQUEST
            )

        repost = Post.objects.create(
            **_author_fields(request.user),
            post_type='repost',
            content='',
            original_post=original_post,
        )

        # Update repost count
        Post.objects.filter(pk=original_post.pk).update(
            reposts_count=models.F('reposts_count') + 1
        )

        serializer = PostSerializer(repost)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def quote(self, request, pk=None):
        """Quote a post with comment."""
        original_post = self.get_object()
        content = request.data.get('content', '')

        if not content:
            return Response(
                {'error': 'Content is required for quote posts'},
                status=status.HTTP_400_BAD_REQUEST
            )

        quote_post = Post.objects.create(
            **_author_fields(request.user),
            post_type='quote',
            content=content,
            original_post=original_post,
        )

        # Update quotes count
        from django.db import models
        Post.objects.filter(pk=original_post.pk).update(
            quotes_count=models.F('quotes_count') + 1
        )

        serializer = PostSerializer(quote_post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def bookmark(self, request, pk=None):
        """Bookmark a post."""
        post = self.get_object()
        collection_id = request.data.get('collection_id')
        note = request.data.get('note', '')

        bookmark, created = Bookmark.objects.get_or_create(
            user_id=request.user.id,
            post=post,
            defaults={
                'collection_id': collection_id,
                'note': note,
            }
        )

        if not created:
            bookmark.collection_id = collection_id
            bookmark.note = note
            bookmark.save()

        return Response({
            'status': 'bookmarked' if created else 'updated'
        })

    @action(detail=True, methods=['post'])
    def unbookmark(self, request, pk=None):
        """Remove bookmark from post."""
        post = self.get_object()
        deleted, _ = Bookmark.objects.filter(
            user_id=request.user.id,
            post=post
        ).delete()

        return Response({
            'status': 'removed' if deleted else 'not_found'
        })

    @action(detail=True, methods=['get', 'post'])
    def comments(self, request, pk=None):
        """Get or add comments to post."""
        post = self.get_object()

        if request.method == 'GET':
            comments = Comment.objects.filter(post=post, parent=None).order_by('created_at')
            page = self.paginate_queryset(comments)
            if page is not None:
                serializer = CommentSerializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = CommentSerializer(comments, many=True)
            return Response(serializer.data)

        # POST - add comment
        serializer = CommentCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(
                post=post,
                author_id=request.user.id,
                author_email=request.user.email,
                author_name=request.user.get_full_name(),
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def view(self, request, pk=None):
        """Record a view on the post."""
        from django.db import models
        post = self.get_object()
        Post.objects.filter(pk=post.pk).update(
            views_count=models.F('views_count') + 1
        )
        return Response({'status': 'viewed'})


class CommentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing comments.
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        readable_posts = filter_posts_readable_to(
            self.request.user.id,
            Post.objects.all(),
        )
        return Comment.objects.filter(post__in=readable_posts)

    def get_serializer_class(self):
        if self.action == 'create':
            return CommentCreateSerializer
        return CommentSerializer

    def perform_update(self, serializer):
        # CON-142 P1.3: get_queryset is Comment.objects.all() with no write
        # guard, so update/partial_update would let any user edit any comment.
        if serializer.instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only modify your own comments.")
        serializer.save()

    def perform_destroy(self, instance):
        # CON-142 P1.3: same IDOR on delete.
        if instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only delete your own comments.")
        instance.delete()

    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """Like a comment."""
        comment = self.get_object()
        like, created = CommentLike.objects.get_or_create(
            comment=comment,
            user_id=request.user.id
        )
        return Response({'status': 'liked' if created else 'already_liked'})

    @action(detail=True, methods=['post'])
    def unlike(self, request, pk=None):
        """Unlike a comment."""
        comment = self.get_object()
        deleted, _ = CommentLike.objects.filter(
            comment=comment,
            user_id=request.user.id
        ).delete()
        return Response({'status': 'unliked' if deleted else 'not_found'})

    @action(detail=True, methods=['get', 'post'])
    def replies(self, request, pk=None):
        """Get or add replies to a comment."""
        parent = self.get_object()

        if request.method == 'GET':
            replies = Comment.objects.filter(parent=parent).order_by('created_at')
            serializer = CommentSerializer(replies, many=True)
            return Response(serializer.data)

        # POST - add reply
        serializer = CommentCreateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(
                post=parent.post,
                parent=parent,
                depth=parent.depth + 1,
                author_id=request.user.id,
                author_email=request.user.email,
                author_name=request.user.get_full_name(),
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ShareDriveFileView(APIView):
    """Share a Drive file to the feed."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        file_id = request.data.get('file_id')
        message = request.data.get('message', '')

        if not file_id:
            return Response(
                {'error': 'file_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # TODO: Validate access to file via Drive API
        # For now, create post with shared content
        post = Post.objects.create(
            **_author_fields(request.user),
            content=message,
            shared_content_type='drive_file',
            shared_content_id=file_id,
            shared_content_data={'file_id': file_id},  # Would be populated with file metadata
        )

        serializer = PostSerializer(post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SharePulseTaskView(APIView):
    """Share a Pulse task to the feed."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        task_id = request.data.get('task_id')
        message = request.data.get('message', '')

        if not task_id:
            return Response(
                {'error': 'task_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        post = Post.objects.create(
            **_author_fields(request.user),
            content=message,
            shared_content_type='pulse_task',
            shared_content_id=task_id,
            shared_content_data={'task_id': task_id},
        )

        serializer = PostSerializer(post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ShareWikiPageView(APIView):
    """Share a Wiki page to the feed."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        page_id = request.data.get('page_id')
        message = request.data.get('message', '')

        if not page_id:
            return Response(
                {'error': 'page_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        post = Post.objects.create(
            **_author_fields(request.user),
            content=message,
            shared_content_type='wiki_page',
            shared_content_id=page_id,
            shared_content_data={'page_id': page_id},
        )

        serializer = PostSerializer(post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ShareNoteView(APIView):
    """Share a Note to the feed."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        note_id = request.data.get('note_id')
        message = request.data.get('message', '')

        if not note_id:
            return Response(
                {'error': 'note_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        post = Post.objects.create(
            **_author_fields(request.user),
            content=message,
            shared_content_type='note',
            shared_content_id=note_id,
            shared_content_data={'note_id': note_id},
        )

        serializer = PostSerializer(post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

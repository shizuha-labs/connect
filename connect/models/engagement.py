"""
Engagement models for Shizuha Connect.

Handles reactions, comments, and bookmarks.
"""

import uuid
from django.db import models


class Reaction(models.Model):
    """Reactions/likes on posts"""

    REACTION_TYPES = [
        ('like', 'Like'),
        ('love', 'Love'),
        ('celebrate', 'Celebrate'),
        ('support', 'Support'),
        ('insightful', 'Insightful'),
        ('curious', 'Curious'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name='reactions')
    user_id = models.IntegerField(db_index=True)
    user_email = models.EmailField(blank=True)
    reaction_type = models.CharField(max_length=20, choices=REACTION_TYPES, default='like')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_reaction'
        unique_together = ['post', 'user_id']
        indexes = [
            models.Index(fields=['post', 'reaction_type']),
            models.Index(fields=['user_id', 'created_at']),
        ]

    def __str__(self):
        return f"User {self.user_id} {self.reaction_type}d Post {self.post_id}"


class Comment(models.Model):
    """
    Threaded comments on posts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name='all_comments')

    author_id = models.IntegerField(db_index=True)
    author_email = models.EmailField()
    author_name = models.CharField(max_length=255, blank=True)

    content = models.TextField(max_length=1000)

    # Threading
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='replies'
    )
    depth = models.PositiveIntegerField(default=0)

    # Engagement
    likes_count = models.PositiveIntegerField(default=0)

    # Status
    is_edited = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_comment'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['post', 'created_at']),
            models.Index(fields=['author_id']),
            models.Index(fields=['parent', 'created_at']),
        ]

    def __str__(self):
        return f"Comment by {self.author_email} on Post {self.post_id}"


class CommentLike(models.Model):
    """Likes on comments"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='likes')
    user_id = models.IntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_comment_like'
        unique_together = ['comment', 'user_id']

    def __str__(self):
        return f"User {self.user_id} liked Comment {self.comment_id}"


class BookmarkCollection(models.Model):
    """Collections for organizing bookmarks"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=100)
    description = models.TextField(max_length=300, blank=True)
    is_private = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bookmark_collection'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} by User {self.user_id}"


class Bookmark(models.Model):
    """Saved/bookmarked posts"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)
    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name='bookmarks')

    # Optional organization into collections
    collection = models.ForeignKey(
        BookmarkCollection,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bookmarks'
    )

    note = models.TextField(max_length=500, blank=True)  # Personal note about bookmark
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_bookmark'
        unique_together = ['user_id', 'post']
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', '-created_at']),
        ]

    def __str__(self):
        return f"User {self.user_id} bookmarked Post {self.post_id}"

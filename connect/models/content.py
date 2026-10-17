"""
Content models for Shizuha Connect.

Handles posts, media, hashtags, and mentions.
"""

import uuid
from django.db import models
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField


class Post(models.Model):
    """
    Main post model supporting text, media, and shared content.
    """

    POST_TYPES = [
        ('post', 'Regular Post'),
        ('repost', 'Repost/Retweet'),
        ('quote', 'Quote Post'),
        ('article', 'Long-form Article'),
        ('poll', 'Poll'),
    ]

    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('followers', 'Followers Only'),
        ('connections', 'Connections Only'),
        ('mentioned', 'Mentioned Users Only'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author_id = models.IntegerField(db_index=True)
    author_email = models.EmailField()
    author_username = models.CharField(max_length=150, blank=True, default='')
    author_display_name = models.CharField(max_length=200, blank=True, default='')

    # Content
    post_type = models.CharField(max_length=20, choices=POST_TYPES, default='post')
    content = models.TextField(max_length=3000)
    content_html = models.TextField(blank=True, help_text="Rendered HTML for display")

    # For reposts/quotes
    original_post = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reposts'
    )

    # Thread support
    reply_to = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='replies'
    )
    thread_root = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='thread_posts'
    )
    thread_depth = models.PositiveIntegerField(default=0)

    # Visibility
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default='public')

    # Cross-service shared content
    shared_content_type = models.CharField(max_length=50, blank=True)  # drive_file, pulse_task, wiki_page, note
    shared_content_id = models.CharField(max_length=100, blank=True)
    shared_content_data = models.JSONField(default=dict, blank=True)  # Cached preview data

    # Engagement stats (denormalized)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    reposts_count = models.PositiveIntegerField(default=0)
    quotes_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)

    # Content metadata
    language = models.CharField(max_length=10, blank=True)
    is_sensitive = models.BooleanField(default=False)

    # Status
    is_pinned = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)

    # Full-text search
    search_vector = SearchVectorField(null=True, blank=True)

    class Meta:
        db_table = 'connect_post'
        ordering = ['-created_at']
        indexes = [
            GinIndex(fields=['search_vector']),
            models.Index(fields=['author_id', '-created_at']),
            models.Index(fields=['visibility', '-created_at']),
            models.Index(fields=['reply_to', '-created_at']),
            models.Index(fields=['thread_root', 'thread_depth']),
            models.Index(fields=['post_type', '-created_at']),
        ]

    def __str__(self):
        return f"Post by {self.author_email}: {self.content[:50]}..."


class PostMedia(models.Model):
    """Media attachments for posts"""

    MEDIA_TYPES = [
        ('image', 'Image'),
        ('video', 'Video'),
        ('gif', 'GIF'),
        ('audio', 'Audio'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media')

    media_type = models.CharField(max_length=10, choices=MEDIA_TYPES)
    file_url = models.URLField()
    thumbnail_url = models.URLField(blank=True)

    # Dimensions
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True, help_text="Duration in seconds for video/audio")
    file_size = models.PositiveIntegerField(null=True, blank=True, help_text="File size in bytes")

    # Accessibility
    alt_text = models.CharField(max_length=1000, blank=True)

    # Order in post
    position = models.PositiveIntegerField(default=0)

    # Processing status
    is_processed = models.BooleanField(default=False)
    processing_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_post_media'
        ordering = ['position']

    def __str__(self):
        return f"{self.media_type} for Post {self.post_id}"


class Hashtag(models.Model):
    """Hashtag tracking"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, db_index=True)

    # Stats
    post_count = models.PositiveIntegerField(default=0)
    weekly_post_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_hashtag'
        ordering = ['-post_count']

    def __str__(self):
        return f"#{self.name}"


class PostHashtag(models.Model):
    """Many-to-many for posts and hashtags"""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='hashtag_links')
    hashtag = models.ForeignKey(Hashtag, on_delete=models.CASCADE, related_name='post_links')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_post_hashtag'
        unique_together = ['post', 'hashtag']

    def __str__(self):
        return f"#{self.hashtag.name} in Post {self.post_id}"


class Mention(models.Model):
    """User mentions in posts"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='mentions')
    mentioned_user_id = models.IntegerField(db_index=True)
    mentioned_email = models.EmailField()
    mentioned_username = models.CharField(max_length=50, blank=True)

    # Position in text for rendering
    start_index = models.PositiveIntegerField()
    end_index = models.PositiveIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_mention'
        indexes = [
            models.Index(fields=['mentioned_user_id', 'created_at']),
        ]

    def __str__(self):
        return f"@{self.mentioned_username} mentioned in Post {self.post_id}"

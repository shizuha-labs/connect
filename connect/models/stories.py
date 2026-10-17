"""
Stories models for Shizuha Connect.

Ephemeral content that expires after 24 hours.
"""

import uuid
from django.db import models
from django.utils import timezone
from datetime import timedelta


class Story(models.Model):
    """
    Ephemeral content that expires after 24 hours.
    """

    STORY_TYPES = [
        ('image', 'Image'),
        ('video', 'Video'),
        ('text', 'Text'),
    ]
    VISIBILITY_CHOICES = [
        ('org_or_connections', 'Same org or accepted connections'),
        ('connections', 'Accepted connections only'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author_id = models.IntegerField(db_index=True)
    author_email = models.EmailField(blank=True)
    author_username = models.CharField(max_length=50, blank=True)

    story_type = models.CharField(max_length=10, choices=STORY_TYPES)
    visibility = models.CharField(
        max_length=32,
        choices=VISIBILITY_CHOICES,
        default='org_or_connections',
        help_text='CON-150: stories are never exposed by unilateral follow alone.',
    )

    # Content
    media_url = models.URLField(blank=True)
    text_content = models.TextField(max_length=500, blank=True)
    background_color = models.CharField(max_length=7, blank=True)  # For text stories
    font_style = models.CharField(max_length=50, blank=True)

    # Duration (for videos)
    duration = models.PositiveIntegerField(null=True, blank=True, help_text="Duration in seconds")

    # Interactive elements
    stickers = models.JSONField(default=list, blank=True)  # Polls, questions, links, etc.
    link_url = models.URLField(blank=True)
    link_title = models.CharField(max_length=100, blank=True)

    # Location
    location = models.CharField(max_length=100, blank=True)

    # Music
    music_track = models.JSONField(default=dict, blank=True)  # {title, artist, preview_url}

    # Stats
    views_count = models.PositiveIntegerField(default=0)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'connect_story'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['author_id', '-created_at']),
            models.Index(fields=['expires_at']),
        ]

    def save(self, *args, **kwargs):
        # Set expires_at to 24 hours from creation if not set
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Story by {self.author_email}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def time_remaining(self):
        remaining = self.expires_at - timezone.now()
        if remaining.total_seconds() < 0:
            return timedelta(0)
        return remaining


class StoryView(models.Model):
    """Track who viewed stories"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='views')
    viewer_id = models.IntegerField(db_index=True)
    viewer_email = models.EmailField(blank=True)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_story_view'
        unique_together = ['story', 'viewer_id']
        ordering = ['-viewed_at']

    def __str__(self):
        return f"User {self.viewer_id} viewed Story {self.story_id}"


class StoryReaction(models.Model):
    """Quick reactions to stories"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='reactions')
    user_id = models.IntegerField(db_index=True)
    user_email = models.EmailField(blank=True)
    reaction = models.CharField(max_length=10)  # emoji
    message = models.CharField(max_length=200, blank=True)  # Optional message with reaction
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_story_reaction'
        ordering = ['-created_at']

    def __str__(self):
        return f"User {self.user_id} reacted {self.reaction} to Story {self.story_id}"

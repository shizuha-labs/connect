"""
Feed models for Shizuha Connect.

Handles materialized feeds and trending topics.
"""

import uuid
from django.db import models


class FeedItem(models.Model):
    """
    Materialized feed items for efficient timeline rendering.
    This is a denormalized cache that gets populated by Celery tasks.
    """

    FEED_TYPES = [
        ('home', 'Home Feed'),
        ('following', 'Following Feed'),
        ('company', 'Company Feed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)
    feed_type = models.CharField(max_length=20, choices=FEED_TYPES, default='home')

    post = models.ForeignKey('Post', on_delete=models.CASCADE, related_name='feed_items')

    # For algorithm ranking
    score = models.FloatField(default=0)

    # Reason for appearing in feed
    reason = models.CharField(max_length=100, blank=True)  # followed_user, liked_by_connection, trending, etc.
    reason_user_id = models.IntegerField(null=True, blank=True)  # Who triggered this (e.g., who liked it)

    created_at = models.DateTimeField(auto_now_add=True)
    post_created_at = models.DateTimeField()  # Denormalized for sorting

    class Meta:
        db_table = 'connect_feed_item'
        ordering = ['-score', '-post_created_at']
        indexes = [
            models.Index(fields=['user_id', 'feed_type', '-score']),
            models.Index(fields=['user_id', 'feed_type', '-post_created_at']),
            models.Index(fields=['post', 'user_id']),
        ]

    def __str__(self):
        return f"Feed item for User {self.user_id}: Post {self.post_id}"


class TrendingTopic(models.Model):
    """Trending hashtags and topics"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Topic info
    name = models.CharField(max_length=100)
    hashtag = models.ForeignKey(
        'Hashtag',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='trending_entries'
    )

    # Stats
    post_count = models.PositiveIntegerField(default=0)
    velocity = models.FloatField(default=0)  # Rate of growth

    # Ranking
    rank = models.PositiveIntegerField()
    score = models.FloatField()

    # Location-specific trending
    location = models.CharField(max_length=100, default='global')

    # Category
    category = models.CharField(max_length=50, blank=True)  # politics, sports, tech, entertainment, etc.

    # Time window
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()

    # Sample post for preview
    sample_post_id = models.UUIDField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_trending_topic'
        ordering = ['rank']
        indexes = [
            models.Index(fields=['location', 'rank']),
            models.Index(fields=['window_end']),
        ]

    def __str__(self):
        return f"#{self.name} (Rank {self.rank})"

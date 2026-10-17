"""
Social graph models for Shizuha Connect.

Handles follows, connections, blocking, and muting.
"""

import uuid
from django.db import models
from django.utils import timezone


class Follow(models.Model):
    """
    Asymmetric follow relationship (Twitter-style).
    User A follows User B - A sees B's posts in feed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    follower_id = models.IntegerField(db_index=True)
    following_id = models.IntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # For notification settings
    notify_on_post = models.BooleanField(default=False)

    class Meta:
        db_table = 'connect_follow'
        unique_together = ['follower_id', 'following_id']
        indexes = [
            models.Index(fields=['follower_id', 'created_at']),
            models.Index(fields=['following_id', 'created_at']),
        ]

    def __str__(self):
        return f"User {self.follower_id} follows User {self.following_id}"


class Connection(models.Model):
    """
    Symmetric connection (LinkedIn-style).
    Both users must accept to be connected.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('withdrawn', 'Withdrawn'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Lower user_id always goes first for consistency
    user_a_id = models.IntegerField(db_index=True)
    user_b_id = models.IntegerField(db_index=True)

    # Who initiated the connection
    requester_id = models.IntegerField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    message = models.TextField(max_length=300, blank=True)  # Connection request message

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'connect_connection'
        unique_together = ['user_a_id', 'user_b_id']
        indexes = [
            models.Index(fields=['user_a_id', 'status']),
            models.Index(fields=['user_b_id', 'status']),
        ]

    def save(self, *args, **kwargs):
        # Ensure user_a_id < user_b_id for consistency
        if self.user_a_id > self.user_b_id:
            self.user_a_id, self.user_b_id = self.user_b_id, self.user_a_id

        # Set accepted_at when status changes to accepted
        if self.status == 'accepted' and not self.accepted_at:
            self.accepted_at = timezone.now()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Connection between {self.user_a_id} and {self.user_b_id} ({self.status})"


class Block(models.Model):
    """User blocking"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    blocker_id = models.IntegerField(db_index=True)
    blocked_id = models.IntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_block'
        unique_together = ['blocker_id', 'blocked_id']

    def __str__(self):
        return f"User {self.blocker_id} blocked User {self.blocked_id}"


class Mute(models.Model):
    """User/keyword muting"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)

    # Either mute a user or a keyword
    muted_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    muted_keyword = models.CharField(max_length=100, blank=True)

    # Mute settings
    mute_notifications = models.BooleanField(default=True)
    mute_from_feed = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'connect_mute'
        indexes = [
            models.Index(fields=['user_id', 'muted_user_id']),
            models.Index(fields=['user_id', 'muted_keyword']),
        ]

    def __str__(self):
        if self.muted_user_id:
            return f"User {self.user_id} muted User {self.muted_user_id}"
        return f"User {self.user_id} muted keyword '{self.muted_keyword}'"

    @property
    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

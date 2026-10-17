"""
Meeting models for video conferencing in Shizuha Connect.
"""

import uuid
import secrets
import string
from django.db import models
from django.utils import timezone


def generate_meeting_code():
    """Generate a meeting code in xxx-xxxx-xxx format."""
    chars = string.ascii_lowercase + string.digits
    part1 = ''.join(secrets.choice(chars) for _ in range(3))
    part2 = ''.join(secrets.choice(chars) for _ in range(4))
    part3 = ''.join(secrets.choice(chars) for _ in range(3))
    return f"{part1}-{part2}-{part3}"


class Meeting(models.Model):
    """
    Video meeting/conference room.
    """
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(blank=True, default='')

    # Meeting code for easy sharing (xxx-xxxx-xxx format)
    meeting_code = models.CharField(
        max_length=12,
        unique=True,
        default=generate_meeting_code,
        db_index=True
    )

    # Host information (stored for cross-service lookup)
    host_id = models.PositiveIntegerField(db_index=True)
    host_email = models.EmailField(blank=True, default='')
    host_name = models.CharField(max_length=255, blank=True, default='')

    # Scheduling
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # LiveKit room
    livekit_room_name = models.CharField(max_length=255, unique=True)

    # Meeting settings (stored as JSON)
    waiting_room_enabled = models.BooleanField(default=False)
    recording_enabled = models.BooleanField(default=False)
    chat_enabled = models.BooleanField(default=True)
    screen_share_enabled = models.BooleanField(default=True)
    max_participants = models.PositiveIntegerField(default=100)

    # Status and stats
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    participant_count = models.PositiveIntegerField(default=0)
    peak_participants = models.PositiveIntegerField(default=0)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['host_id', 'status']),
            models.Index(fields=['status', 'scheduled_start']),
        ]

    def __str__(self):
        return f"{self.title or 'Meeting'} ({self.meeting_code})"

    def save(self, *args, **kwargs):
        if not self.livekit_room_name:
            self.livekit_room_name = f"meeting-{self.id}"
        super().save(*args, **kwargs)

    def start(self):
        """Start the meeting."""
        self.status = 'active'
        self.started_at = timezone.now()
        self.save(update_fields=['status', 'started_at', 'updated_at'])

    def end(self):
        """End the meeting."""
        self.status = 'ended'
        self.ended_at = timezone.now()
        self.save(update_fields=['status', 'ended_at', 'updated_at'])

    def cancel(self):
        """Cancel the meeting."""
        self.status = 'cancelled'
        self.save(update_fields=['status', 'updated_at'])

    @property
    def is_active(self):
        return self.status == 'active'

    @property
    def can_join(self):
        return self.status in ['scheduled', 'active']

    def get_shareable_link(self, base_url=''):
        """Get shareable meeting link."""
        return f"{base_url}/connect/meeting/{self.meeting_code}"


class MeetingParticipant(models.Model):
    """
    Participant in a meeting.
    """
    ROLE_CHOICES = [
        ('host', 'Host'),
        ('co_host', 'Co-host'),
        ('presenter', 'Presenter'),
        ('participant', 'Participant'),
    ]

    STATUS_CHOICES = [
        ('invited', 'Invited'),
        ('waiting', 'In Waiting Room'),
        ('joined', 'Joined'),
        ('left', 'Left'),
        ('removed', 'Removed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='participants'
    )

    # User information
    user_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    user_email = models.EmailField(blank=True, default='')
    user_name = models.CharField(max_length=255, blank=True, default='')

    # Guest support (users without account)
    is_guest = models.BooleanField(default=False)
    guest_name = models.CharField(max_length=255, blank=True, default='')

    # Role and status
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='participant')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='invited')

    # Timing
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    # Meeting state
    is_muted = models.BooleanField(default=False)
    is_video_off = models.BooleanField(default=False)
    is_screen_sharing = models.BooleanField(default=False)
    is_hand_raised = models.BooleanField(default=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['joined_at']
        unique_together = [['meeting', 'user_id']]
        indexes = [
            models.Index(fields=['meeting', 'status']),
        ]

    def __str__(self):
        name = self.user_name or self.guest_name or self.user_email or f"User {self.user_id}"
        return f"{name} in {self.meeting.meeting_code}"

    @property
    def display_name(self):
        return self.user_name or self.guest_name or self.user_email or 'Anonymous'

    def join(self):
        """Mark participant as joined."""
        self.status = 'joined'
        self.joined_at = timezone.now()
        self.save(update_fields=['status', 'joined_at', 'updated_at'])

        # Update meeting participant count
        self.meeting.participant_count = self.meeting.participants.filter(
            status='joined'
        ).count()
        if self.meeting.participant_count > self.meeting.peak_participants:
            self.meeting.peak_participants = self.meeting.participant_count
        self.meeting.save(update_fields=['participant_count', 'peak_participants', 'updated_at'])

    def leave(self):
        """Mark participant as left."""
        self.status = 'left'
        self.left_at = timezone.now()
        self.save(update_fields=['status', 'left_at', 'updated_at'])

        # Update meeting participant count
        self.meeting.participant_count = self.meeting.participants.filter(
            status='joined'
        ).count()
        self.meeting.save(update_fields=['participant_count', 'updated_at'])


class MeetingInvite(models.Model):
    """
    Meeting invitation sent to users.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='invites'
    )

    # Invited user
    invited_user_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    invited_email = models.EmailField()
    invited_name = models.CharField(max_length=255, blank=True, default='')

    # Inviter
    inviter_id = models.PositiveIntegerField()
    inviter_name = models.CharField(max_length=255, blank=True, default='')

    # Response
    accepted = models.BooleanField(default=False)
    declined = models.BooleanField(default=False)
    responded_at = models.DateTimeField(null=True, blank=True)

    # Optional message
    message = models.TextField(blank=True, default='')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [['meeting', 'invited_email']]

    def __str__(self):
        return f"Invite to {self.invited_email} for {self.meeting.meeting_code}"

    def accept(self):
        """Accept the invitation."""
        self.accepted = True
        self.declined = False
        self.responded_at = timezone.now()
        self.save(update_fields=['accepted', 'declined', 'responded_at', 'updated_at'])

    def decline(self):
        """Decline the invitation."""
        self.accepted = False
        self.declined = True
        self.responded_at = timezone.now()
        self.save(update_fields=['accepted', 'declined', 'responded_at', 'updated_at'])

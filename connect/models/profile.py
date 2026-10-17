"""
Profile models for Shizuha Connect.

These models extend user profiles with social networking capabilities,
professional information, skills, and endorsements.
"""

import uuid
from django.db import models
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField


class SocialProfile(models.Model):
    """
    Extended social profile linked to Shizuha ID UserProfile.
    This is the Connect-specific profile data.
    """

    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('connections', 'Connections Only'),
        ('private', 'Private'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(unique=True, db_index=True, help_text="User ID from Shizuha ID")
    user_email = models.EmailField(db_index=True)

    # Display info
    display_name = models.CharField(max_length=100)
    username = models.CharField(max_length=50, unique=True, db_index=True)
    bio = models.TextField(max_length=500, blank=True)
    headline = models.CharField(max_length=220, blank=True, help_text="Professional headline")

    # Profile media
    avatar_url = models.URLField(blank=True)
    banner_url = models.URLField(blank=True)

    # Location
    location = models.CharField(max_length=100, blank=True)
    website = models.URLField(blank=True)

    # Professional info
    current_position = models.CharField(max_length=200, blank=True)
    current_company = models.CharField(max_length=200, blank=True)
    industry = models.CharField(max_length=100, blank=True)

    # Privacy settings
    profile_visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default='public'
    )

    # Stats (denormalized for performance)
    followers_count = models.PositiveIntegerField(default=0)
    following_count = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)
    connections_count = models.PositiveIntegerField(default=0)

    # Verification
    is_verified = models.BooleanField(default=False)
    verified_type = models.CharField(max_length=50, blank=True)  # blue, gold, etc.

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    # Full-text search
    skills_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)

    # CON-24: onboarding wizard state — tracks which steps the user has completed.
    # Schema: {completed_steps: [str], completed_at: ISO8601|null}
    onboarding_state = models.JSONField(
        default=dict, blank=True,
        help_text='Onboarding wizard progress: {completed_steps: [...], completed_at: null|ISO8601}',
    )

    class Meta:
        db_table = 'connect_social_profile'
        indexes = [
            GinIndex(fields=['search_vector']),
            models.Index(fields=['username']),
            models.Index(fields=['location']),
            models.Index(fields=['industry']),
            models.Index(fields=['created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"@{self.username}"


class Skill(models.Model):
    """Skills catalog for endorsements"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    category = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = 'connect_skill'
        ordering = ['name']

    def __str__(self):
        return self.name


class UserSkill(models.Model):
    """User skills with endorsement counts"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(SocialProfile, on_delete=models.CASCADE, related_name='skills')
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE)
    endorsement_count = models.PositiveIntegerField(default=0)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'connect_user_skill'
        unique_together = ['profile', 'skill']
        ordering = ['-endorsement_count', 'display_order']

    def __str__(self):
        return f"{self.profile.username} - {self.skill.name}"


class Endorsement(models.Model):
    """Skill endorsements from other users"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_skill = models.ForeignKey(UserSkill, on_delete=models.CASCADE, related_name='endorsements')
    endorser_id = models.IntegerField(db_index=True)
    endorser_email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_endorsement'
        unique_together = ['user_skill', 'endorser_id']

    def __str__(self):
        return f"{self.endorser_email} endorsed {self.user_skill}"


class Experience(models.Model):
    """Work experience entries"""

    EMPLOYMENT_TYPES = [
        ('full_time', 'Full-time'),
        ('part_time', 'Part-time'),
        ('contract', 'Contract'),
        ('temporary', 'Temporary'),
        ('internship', 'Internship'),
        ('freelance', 'Freelance'),
        ('self_employed', 'Self-employed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(SocialProfile, on_delete=models.CASCADE, related_name='experiences')

    title = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200)
    employer_id = models.UUIDField(null=True, blank=True,
        help_text="External Guild Employer UUID; unresolved references retain company_name")
    location = models.CharField(max_length=100, blank=True)
    employment_type = models.CharField(max_length=20, choices=EMPLOYMENT_TYPES, blank=True)

    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)

    description = models.TextField(blank=True)

    class Meta:
        db_table = 'connect_experience'
        ordering = ['-is_current', '-start_date']

    def __str__(self):
        return f"{self.title} at {self.company_name}"


class Education(models.Model):
    """Education entries"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(SocialProfile, on_delete=models.CASCADE, related_name='education')

    school_name = models.CharField(max_length=200)
    degree = models.CharField(max_length=100, blank=True)
    field_of_study = models.CharField(max_length=100, blank=True)

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    description = models.TextField(blank=True)
    activities = models.TextField(blank=True)

    class Meta:
        db_table = 'connect_education'
        ordering = ['-end_date']

    def __str__(self):
        return f"{self.degree} at {self.school_name}"

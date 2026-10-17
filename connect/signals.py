"""
Django signals for Shizuha Connect.

Handles automatic updates when models change.
"""

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.contrib.postgres.search import SearchVector
from django.db import IntegrityError, connection, models
from django.utils import timezone
import logging
import re

logger = logging.getLogger(__name__)
HASHTAG_RE = re.compile(r"(?<!\w)#(\w{1,100})")
MAX_HASHTAGS_PER_POST = 30


def _postgres_search_available():
    """SearchVector updates use PostgreSQL's to_tsvector()."""
    return connection.vendor == 'postgresql'


def extract_hashtags(text):
    """Return lower-cased, de-duplicated hashtag names in first-seen order."""
    seen = set()
    tags = []
    for match in HASHTAG_RE.finditer(text or ""):
        tag = match.group(1).lower()
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= MAX_HASHTAGS_PER_POST:
            break
    return tags


def _get_or_create_hashtag(name):
    from connect.models import (Hashtag)
    try:
        hashtag, _ = Hashtag.objects.get_or_create(name=name)
        return hashtag
    except IntegrityError:
        return Hashtag.objects.get(name=name)


def sync_post_hashtags(post):
    """Synchronize PostHashtag rows for one post. Fail-soft by design."""
    try:
        from connect.models import (Hashtag, PostHashtag)

        desired_names = extract_hashtags(post.content)
        desired_hashtags = {name: _get_or_create_hashtag(name) for name in desired_names}
        existing_links = {
            link.hashtag.name: link
            for link in PostHashtag.objects.select_related('hashtag').filter(post=post)
        }
        desired_set = set(desired_hashtags)
        existing_set = set(existing_links)

        stale_names = existing_set - desired_set
        new_names = desired_set - existing_set

        if stale_names:
            stale_ids = [existing_links[name].hashtag_id for name in stale_names]
            PostHashtag.objects.filter(post=post, hashtag_id__in=stale_ids).delete()
            Hashtag.objects.filter(id__in=stale_ids, post_count__gt=0).update(
                post_count=models.F('post_count') - 1,
            )

        if new_names:
            now = timezone.now()
            PostHashtag.objects.bulk_create(
                [PostHashtag(post=post, hashtag=desired_hashtags[name]) for name in new_names],
                ignore_conflicts=True,
            )
            Hashtag.objects.filter(id__in=[desired_hashtags[name].id for name in new_names]).update(
                post_count=models.F('post_count') + 1,
                last_used_at=now,
            )
    except Exception:
        logger.exception('Failed to sync hashtags for post %s', getattr(post, 'pk', None))


@receiver(post_save, sender='connect.Post')
def update_post_search_vector(sender, instance, **kwargs):
    """Update full-text search vector and hashtag links for posts."""
    sync_post_hashtags(instance)
    if not _postgres_search_available():
        return
    try:
        from connect.models import (Post)
        # CON-351: normalize slash/hyphen/underscore-joined product tokens
        # (e.g. "Tally/Zoho/CSV", "GST/TDS/TCS") so each component is
        # individually searchable. Postgres' default tokenizer treats
        # "Tally/Zoho" as one token; splitting on separators makes the
        # product names discoverable.
        normalized = _normalize_search_text(instance.content)
        Post.objects.filter(pk=instance.pk).update(
            search_vector=(
                SearchVector(models.Value(normalized), weight='A')
                + SearchVector('author_username', weight='B')
                + SearchVector('author_display_name', weight='B')
            )
        )
    except Exception:
        logger.exception('Failed to update search vector for post %s', instance.pk)


def _normalize_search_text(text: str | None) -> str:
    """Split slash/hyphen/underscore-joined tokens for full-text indexing.

    ``Tally/Zoho/CSV import`` → ``Tally Zoho CSV import`` so each product
    token lands in the search vector. Preserves spaces and word boundaries;
    empty/None input returns an empty string.
    """
    if not text:
        return ''
    return re.sub(r'[/\-_]+', ' ', text)


def _profile_search_vector():
    return (
        SearchVector('username', weight='A')
        + SearchVector('display_name', weight='A')
        + SearchVector('headline', weight='B')
        + SearchVector('current_position', weight='B')
        + SearchVector('current_company', weight='B')
        + SearchVector('industry', weight='B')
        + SearchVector('skills_text', weight='B')
        + SearchVector('bio', weight='C')
    )


def _profile_skills_text(profile_id):
    from connect.models import (UserSkill)

    return ' '.join(
        UserSkill.objects.filter(profile_id=profile_id)
        .select_related('skill')
        .order_by('skill__name')
        .values_list('skill__name', flat=True)
    )


def refresh_profile_search_fields(profile_id, *, refresh_skills=False):
    if connection.vendor != 'postgresql':
        return
    from connect.models import (SocialProfile)

    updates = {'search_vector': _profile_search_vector()}
    if refresh_skills:
        updates['skills_text'] = _profile_skills_text(profile_id)
    SocialProfile.objects.filter(pk=profile_id).update(**updates)


@receiver(post_save, sender='connect.SocialProfile')
def update_profile_search_vector(sender, instance, **kwargs):
    """Update weighted full-text search vector for profiles."""
    # CON-54's inline _postgres_search_available() guard is now redundant here:
    # refresh_profile_search_fields() guards internally (vendor != postgresql -> return),
    # so SQLite unit tests stay safe. Take master's weighted-FTS + skill-sync path.
    try:
        refresh_profile_search_fields(instance.pk)
    except Exception:
        logger.exception('Failed to update search vector for profile %s', instance.pk)


@receiver(post_save, sender='connect.UserSkill')
@receiver(post_delete, sender='connect.UserSkill')
def update_profile_skill_search_text(sender, instance, **kwargs):
    """Keep denormalized skills_text and FTS vector in sync with skills."""
    try:
        refresh_profile_search_fields(instance.profile_id, refresh_skills=True)
    except Exception:
        logger.exception('Failed to update skill search text for profile %s', instance.profile_id)


@receiver(post_save, sender='connect.Skill')
def update_profiles_for_skill_name(sender, instance, **kwargs):
    """Refresh profiles when a skill name changes."""
    try:
        profile_ids = list(instance.userskill_set.values_list('profile_id', flat=True))
        for profile_id in profile_ids:
            refresh_profile_search_fields(profile_id, refresh_skills=True)
    except Exception:
        logger.exception('Failed to refresh profiles for skill %s', instance.pk)






@receiver(post_save, sender='connect.Follow')
def update_follow_counts(sender, instance, created, **kwargs):
    """Update follower/following counts when follow is created."""
    if created:
        from connect.models import (SocialProfile)
        # Increment following count for follower
        SocialProfile.objects.filter(user_id=instance.follower_id).update(
            following_count=models.F('following_count') + 1
        )
        # Increment followers count for following
        SocialProfile.objects.filter(user_id=instance.following_id).update(
            followers_count=models.F('followers_count') + 1
        )


@receiver(post_delete, sender='connect.Follow')
def update_follow_counts_on_delete(sender, instance, **kwargs):
    """Update follower/following counts when follow is deleted."""
    from django.db import models
    from connect.models import (SocialProfile)
    # Decrement following count for follower
    SocialProfile.objects.filter(user_id=instance.follower_id).update(
        following_count=models.F('following_count') - 1
    )
    # Decrement followers count for following
    SocialProfile.objects.filter(user_id=instance.following_id).update(
        followers_count=models.F('followers_count') - 1
    )


@receiver(post_save, sender='connect.Reaction')
def update_post_likes_count(sender, instance, created, **kwargs):
    """Update post likes count when reaction is added."""
    if created:
        from django.db import models
        from connect.models import (Post)
        Post.objects.filter(pk=instance.post_id).update(
            likes_count=models.F('likes_count') + 1
        )


@receiver(post_delete, sender='connect.Reaction')
def update_post_likes_count_on_delete(sender, instance, **kwargs):
    """Update post likes count when reaction is removed."""
    from django.db import models
    from connect.models import (Post)
    Post.objects.filter(pk=instance.post_id).update(
        likes_count=models.F('likes_count') - 1
    )


@receiver(post_save, sender='connect.Comment')
def update_post_comments_count(sender, instance, created, **kwargs):
    """Update post comments count when comment is added."""
    if created:
        from django.db import models
        from connect.models import (Post)
        Post.objects.filter(pk=instance.post_id).update(
            comments_count=models.F('comments_count') + 1
        )


@receiver(post_delete, sender='connect.Comment')
def update_post_comments_count_on_delete(sender, instance, **kwargs):
    """Update post comments count when comment is removed."""
    from django.db import models
    from connect.models import (Post)
    Post.objects.filter(pk=instance.post_id).update(
        comments_count=models.F('comments_count') - 1
    )


@receiver(post_save, sender='connect.CommentLike')
def update_comment_likes_count(sender, instance, created, **kwargs):
    """CON-347: keep Comment.likes_count in sync when a comment is liked."""
    if created:
        from django.db import models
        from connect.models import (Comment)
        Comment.objects.filter(pk=instance.comment_id).update(
            likes_count=models.F('likes_count') + 1
        )


@receiver(post_delete, sender='connect.CommentLike')
def update_comment_likes_count_on_delete(sender, instance, **kwargs):
    """CON-347: decrement Comment.likes_count when a comment like is removed."""
    from django.db import models
    from connect.models import (Comment)
    Comment.objects.filter(pk=instance.comment_id).update(
        likes_count=models.F('likes_count') - 1
    )






@receiver(post_save, sender='connect.DirectMessage')
def update_conversation_last_message(sender, instance, created, **kwargs):
    """Update conversation last message info when message is sent."""
    if created:
        from django.db import models
        from connect.models import (Conversation)
        Conversation.objects.filter(pk=instance.conversation_id).update(
            last_message_at=instance.created_at,
            last_message_preview=instance.content[:100],
            message_count=models.F('message_count') + 1
        )


@receiver(post_save, sender='connect.Endorsement')
def update_endorsement_count(sender, instance, created, **kwargs):
    """Update user skill endorsement count."""
    if created:
        from django.db import models
        from connect.models import (UserSkill)
        UserSkill.objects.filter(pk=instance.user_skill_id).update(
            endorsement_count=models.F('endorsement_count') + 1
        )


@receiver(post_delete, sender='connect.Endorsement')
def update_endorsement_count_on_delete(sender, instance, **kwargs):
    """Update user skill endorsement count when endorsement is removed."""
    from django.db import models
    from connect.models import (UserSkill)
    UserSkill.objects.filter(pk=instance.user_skill_id).update(
        endorsement_count=models.F('endorsement_count') - 1
    )


@receiver(post_save, sender='connect.ConversationParticipant')
def ensure_participant_social_profile_signal(sender, instance, created, **kwargs):
    """
    CON-52: when a user joins a conversation, make sure they have a searchable
    SocialProfile. Agents are messageable by username but never visit the
    Connect UI, so they otherwise have no profile — invisible to people-search
    and blank in conversation lists. Enqueued off the request path; never blocks
    or breaks message delivery.
    """
    if not created:
        return
    try:
        from connect.tasks import ensure_participant_social_profile
        ensure_participant_social_profile.delay(instance.user_id)
    except Exception:
        logger.warning(
            "CON-52: failed to enqueue profile ensure for user %s",
            getattr(instance, 'user_id', '?'), exc_info=True,
        )

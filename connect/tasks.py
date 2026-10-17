"""
Celery tasks for Shizuha Connect.

Handles asynchronous operations like:
- Feed generation
- Notification delivery
- Media processing
- Trending calculations
"""

import hashlib
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, models
from django.utils import timezone

logger = logging.getLogger(__name__)


# Feed Generation Tasks

@shared_task
def generate_feed_for_user(user_id: int):
    """
    Regenerate cached feed for a user.
    Called when feed cache expires or user explicitly refreshes.
    """
    from connect.models import Post, Follow, FeedItem

    logger.info(f"Generating feed for user {user_id}")

    # Get users the current user follows
    following_ids = list(
        Follow.objects.filter(follower_id=user_id)
        .values_list('following_id', flat=True)
    )

    if not following_ids:
        return

    # Get recent posts from followed users
    posts = Post.objects.filter(
        author_id__in=following_ids,
        visibility__in=['public', 'followers'],
        created_at__gte=timezone.now() - timedelta(days=7)
    ).order_by('-created_at')[:500]

    # Clear old feed items
    FeedItem.objects.filter(user_id=user_id).delete()

    # Create new feed items with scores
    for post in posts:
        score = calculate_post_score(post)
        FeedItem.objects.create(
            user_id=user_id,
            feed_type='home',
            post=post,
            score=score,
            reason='followed_user',
            post_created_at=post.created_at
        )


@shared_task
def fan_out_post(post_id: str):
    """
    Fan out new post to followers' feeds.
    For high-follower accounts, may use lazy evaluation.
    """
    from connect.models import Post, Follow, FeedItem, SocialProfile

    try:
        post = Post.objects.get(pk=post_id)
    except Post.DoesNotExist:
        logger.error(f"Post {post_id} not found for fan-out")
        return

    # Get author's followers
    follower_ids = list(
        Follow.objects.filter(following_id=post.author_id)
        .values_list('follower_id', flat=True)
    )

    logger.info(f"Fanning out post {post_id} to {len(follower_ids)} followers")

    # For accounts with many followers, limit immediate fan-out
    if len(follower_ids) > 10000:
        # Just update trending, let users pull on demand
        logger.info(f"Large follower count, skipping full fan-out")
        return

    score = calculate_post_score(post)

    # Create feed items for each follower
    feed_items = []
    for follower_id in follower_ids:
        feed_items.append(FeedItem(
            user_id=follower_id,
            feed_type='home',
            post=post,
            score=score,
            reason='followed_user',
            post_created_at=post.created_at
        ))

    FeedItem.objects.bulk_create(feed_items, ignore_conflicts=True)


def calculate_post_score(post) -> float:
    """
    Calculate engagement score for a post.
    Higher score = higher in feed.
    """
    from django.utils import timezone

    # Recency score (decays over time)
    age_hours = (timezone.now() - post.created_at).total_seconds() / 3600
    recency_score = max(0, 1 - (age_hours / 168))  # Decays to 0 over 1 week

    # Engagement score
    engagement = (
        post.likes_count * 1 +
        post.comments_count * 3 +
        post.reposts_count * 5 +
        post.quotes_count * 4
    )
    engagement_score = min(1, engagement / 100)  # Cap at 100 interactions

    # Combine scores
    return (recency_score * 0.4) + (engagement_score * 0.6)


# Notification Tasks

@shared_task
def send_reaction_notification(post_id: str, reactor_id: int, reaction_type: str):
    """Notify post author of reaction."""
    from connect.models import Post, SocialProfile

    try:
        post = Post.objects.get(pk=post_id)
    except Post.DoesNotExist:
        return

    # Don't notify if reacting to own post
    if post.author_id == reactor_id:
        return

    # TODO: Send push notification via shizuha-id
    logger.info(f"Notification: User {reactor_id} {reaction_type}d post {post_id}")


@shared_task
def send_follow_notification(follower_id: int, followed_id: int):
    """Notify user of new follower."""
    # TODO: Send push notification via shizuha-id
    logger.info(f"Notification: User {follower_id} followed User {followed_id}")


@shared_task
def send_mention_notification(post_id: str, mentioned_user_id: int):
    """Notify user of mention."""
    # TODO: Send push notification via shizuha-id
    logger.info(f"Notification: User {mentioned_user_id} mentioned in post {post_id}")


def _is_agent_participant(participant):
    """True if this conversation participant is an AI agent (gets the AGENT runtime
    inbox channel) rather than a human (DASHBOARD channel). Both share the
    user_inbox_<id> transport but are gated/deduped under distinct channel keys."""
    return getattr(participant, 'participant_type', 'human') == 'agent'


def fanout_dm_inapp_external(message, participants, title, body, *, now=None):
    """CON-15 B.2: gate + dedup the non-mobile HLD channels through the shared
    notification policy engine.

    * dashboard (human) / agent (AI runtime) — a per-recipient in-app notification
      on the user_inbox_<id> WS group, gated by `should_notify_channel` and
      deduped by `mark_dispatched` under the recipient's channel key.
    * external bridge — only where a bridge is configured for the recipient; the
      cooldown suppressor backs off a flapping bridge and failures (re)arm it.

    Mobile push stays in `send_dm_notification`'s loop (same engine). Each channel
    is independent: suppression/dedup on one never blocks another."""
    from connect.services.notifications import (
        should_notify_channel, mark_dispatched, DASHBOARD, AGENT, EXTERNAL,
    )
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    from connect.services.participant_identity import project_agent_delivery_identity

    now = now or timezone.now()
    participants = list(participants)
    project_agent_delivery_identity(participants)
    conv = message.conversation
    channel_layer = get_channel_layer()
    notif_event = {
        'type': 'dm_notification',
        'conversation_id': str(conv.id),
        'message_id': str(message.id),
        'sender_id': message.sender_id,
        'sender_name': message.sender_name,
        'title': title,
        'preview': body,
    }

    for participant in participants:
        is_agent = _is_agent_participant(participant)
        inbox_channel = AGENT if is_agent else DASHBOARD

        # In-app inbox notification (dashboard for humans / runtime inbox for agents).
        notify, _reason = should_notify_channel(participant, message, inbox_channel, now=now)
        if notify and mark_dispatched(participant.user_id, conv.id, message.event_id, inbox_channel):
            try:
                async_to_sync(channel_layer.group_send)(
                    f'user_inbox_{participant.user_id}', dict(notif_event),
                )
            except Exception as e:
                logger.warning(
                    f"CON-15 {inbox_channel} inbox notify failed for {participant.user_id}: {e}")

        # HIVE-602 (operator 2026-07-10): event-driven wake — a STOPPED agent
        # recipient never hears the inbox group_send, and Hive's lifecycle only
        # watches Pulse workload. Nudge Hive to start the agent the moment a DM
        # lands; the WS missed-message replay then delivers this message on
        # boot. Best-effort and wake-only: failures never block the fanout.
        if is_agent:
            from connect.views.messaging import is_ack_only_message
            from connect.services.qa_probe import is_qa_fixture_identity, is_production_fleet_agent
            probe = is_ack_only_message(getattr(message, 'content', '') or '')
            qa_to_prod = is_production_fleet_agent(
                participant_type='agent',
                email=getattr(participant, 'user_email', '') or '',
                username=getattr(participant, 'user_name', '') or '',
            ) and is_qa_fixture_identity(
                email=getattr(message, 'sender_email', '') or '',
                username=getattr(message, 'sender_name', '') or '',
            )
            if not probe and not qa_to_prod:
                _wake_agent_runtime(participant, message)

        # External bridge — humans only, and only where a bridge is configured.
        if not is_agent:
            notify, _reason = should_notify_channel(participant, message, EXTERNAL, now=now)
            if notify:
                _fanout_external_bridge(participant, message, now=now)


def _fanout_external_bridge(participant, message, *, now=None):
    """CON-15 B.2 external-bridge channel adapter — the channel-policy gate has
    already passed (`should_notify_channel(EXTERNAL)`: enabled + not quiet hours +
    not in cooldown). Dedup on the external channel key, then deliver via the
    recipient's configured outbound bridge with cooldown backoff on failure.

    Connect has no GENERAL outbound-bridge DM transport — `services.bridge`'s
    outbound send is provider/policy-gated (WhatsApp + explicit channel policy) —
    so `_resolve_outbound_bridge_channel` returns None for ordinary internal DMs and
    this is a clean no-op. The dedup + cooldown bookkeeping (record_bridge_*) are in
    place so a real transport plugs in here without touching the policy engine."""
    from connect.services.notifications import (
        mark_dispatched, record_bridge_failure, record_bridge_success, EXTERNAL,
    )
    bridge_channel = _resolve_outbound_bridge_channel(participant)
    if not bridge_channel:
        return  # no deliverable external bridge for this recipient — nothing to send
    if not mark_dispatched(participant.user_id, message.conversation_id, message.event_id, EXTERNAL):
        return  # already attempted for this (recipient, message) on the external channel
    try:
        _deliver_external_bridge(participant, message, bridge_channel)
        record_bridge_success(participant.user_id, bridge_channel, now=now)
    except Exception as e:
        cd = record_bridge_failure(participant.user_id, bridge_channel, now=now)
        logger.warning(
            f"CON-15 external bridge delivery failed for {participant.user_id} "
            f"on {bridge_channel}: {e} (cooldown until {cd.cooldown_until})")


def _resolve_outbound_bridge_channel(participant):
    """Recipient's deliverable outbound bridge channel key, or None.

    Returns None for internal-only DMs (no policy-approved outbound bridge),
    keeping the external channel a no-op. This is the single seam to extend when a
    general outbound-bridge DM transport lands; the cooldown engine keys on the
    string this returns."""
    return None


def _deliver_external_bridge(participant, message, bridge_channel):
    """Outbound-bridge transport seam. Reached only once a deliverable bridge is
    resolved; raises on failure so the caller arms the cooldown. Wraps
    `services.bridge.send_outbound_bridge_message` (provider/policy-gated) when wired."""
    raise NotImplementedError("outbound bridge DM transport not yet wired")


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_dm_notification(self, message_id: str):
    """Notify recipients of new DM via push notification."""
    from connect.models import DirectMessage, ConversationParticipant
    from connect.services.privacy import allowed_delivery_user_ids

    try:
        message = DirectMessage.objects.select_related('conversation').get(pk=message_id)
    except DirectMessage.DoesNotExist:
        return

    # Don't push for system messages
    if message.message_type == 'system':
        return

    conversation = message.conversation
    now = timezone.now()

    # Get all active, non-sender participants
    participants = ConversationParticipant.objects.filter(
        conversation=conversation,
        has_left=False,
    ).exclude(user_id=message.sender_id)

    push_url = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api') + '/internal/push/send/'

    # Build title/body based on conversation type
    content_preview = (message.content or '')[:200]
    if conversation.conversation_type == 'group':
        title = conversation.name or 'Group Chat'
        sender_first = message.sender_name.split()[0] if message.sender_name else 'Someone'
        body = f"{sender_first}: {content_preview}"[:200]
    else:
        title = message.sender_name or 'New Message'
        body = content_preview

    # CON-15 B.2: fan out the non-mobile HLD channels (in-app dashboard / agent
    # runtime inbox via user_inbox, and external bridge) through the SAME policy
    # engine + idempotency ledger, so suppression and at-least-once dedup cover
    # every channel — not only the mobile push below (kei finding / kai contract).
    try:
        fanout_dm_inapp_external(message, participants, title, body, now=now)
    except Exception as e:  # never let in-app fanout break the mobile push path
        logger.error(f"CON-15 in-app/external fanout failed for {message_id}: {e}")

    # CON-15: gate every recipient through the shared notification policy engine
    # (full ordered suppression incl. the read-receipt race the inline check missed)
    # and the at-least-once idempotency ledger, so a Celery retry / WS-reconnect
    # replay does not re-push the same message on the `mobile` channel.
    from connect.services.notifications import should_notify_channel, mark_dispatched, MOBILE
    recipient_ids = []
    for participant in participants:
        # CON-15 B.2: channel-aware decision — global suppressors PLUS the mobile
        # per-channel opt-out and quiet hours (mobile is an interrupt channel).
        notify, _reason = should_notify_channel(participant, message, MOBILE, now=now)
        if not notify:
            continue
        if not mark_dispatched(participant.user_id, conversation.id, message.event_id, MOBILE):
            continue  # already pushed for this (recipient, message) — dedup
        recipient_ids.append(participant.user_id)

    if not recipient_ids:
        return

    # Send one push request per recipient (shizuha-id expects per-user)
    # but limit to a single session to avoid opening N connections
    session = requests.Session()
    session.headers.update({
        'X-Internal-Service': 'shizuha-connect',
        'Content-Type': 'application/json',
    })

    failed = False
    for user_id in recipient_ids:
        payload = {
            'user_id': user_id,
            'title': title,
            'body': body,
            'data': {
                'type': 'chat_message',
                'screen': 'ConversationDetail',
                'conversation_id': str(conversation.id),
                'message_id': str(message.id),
                'sender_id': message.sender_id,
            },
            'channel_id': 'chat_messages',
            'priority': 'high',
        }

        try:
            response = session.post(push_url, json=payload, timeout=10)
            if response.ok:
                result = response.json()
                logger.info(f"Push sent to user {user_id}: {result.get('sent', 0)} devices")
            else:
                logger.warning(f"Push endpoint returned {response.status_code} for user {user_id}")
        except requests.exceptions.ConnectionError:
            # Push service is down — retry the whole task
            failed = True
            break
        except Exception as e:
            logger.error(f"Push notification error for user {user_id}: {e}")

    session.close()

    if failed:
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            logger.error(f"Push notification permanently failed for message {message_id}")


# Agent Mention Tasks

@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def notify_agent_mention(self, conversation_id: str, message_id: str,
                         agent_uuid: str, sender_name: str, sender_id: int):
    """
    Notify an AI agent that it was @mentioned in a group conversation.

    Gathers recent conversation context, sends typing indicator, and
    POSTs to shizuha-agent's internal API for response generation.
    """
    from connect.models import DirectMessage, Conversation, ConversationParticipant
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    try:
        conversation = Conversation.objects.get(pk=conversation_id)
        message = DirectMessage.objects.get(pk=message_id)
    except (Conversation.DoesNotExist, DirectMessage.DoesNotExist):
        logger.error(f"Conversation {conversation_id} or message {message_id} not found")
        return

    # Gather last 50 messages for context
    recent_messages = DirectMessage.objects.filter(
        conversation_id=conversation_id,
        is_deleted=False,
    ).order_by('-created_at')[:50]

    context_messages = []
    for msg in reversed(recent_messages):
        context_messages.append({
            'id': str(msg.id),
            'sender_id': msg.sender_id,
            'sender_name': msg.sender_name,
            'sender_is_agent': msg.sender_is_agent,
            'content': msg.content,
            'message_type': msg.message_type,
            'created_at': msg.created_at.isoformat(),
        })

    # Get participant list for context
    participants = list(
        ConversationParticipant.objects.filter(
            conversation_id=conversation_id,
            has_left=False,
        ).values('user_id', 'user_name', 'participant_type', 'agent_role')
    )

    # Send typing indicator via channel layer
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'chat_{conversation_id}',
            {
                'type': 'typing_indicator',
                'user_id': 0,  # System/agent
                'user_name': f'AI Agent',
                'is_typing': True,
            }
        )
    except Exception:
        logger.exception("Failed to send typing indicator for agent mention")

    # POST to shizuha-agent internal API
    agent_url = getattr(
        settings, 'SHIZUHA_AGENT_API_URL',
        'http://shizuha-agent:8017/api'
    )
    payload = {
        'conversation_id': conversation_id,
        'message_id': message_id,
        'agent_id': agent_uuid,
        'sender_name': sender_name,
        'sender_id': sender_id,
        'conversation_name': conversation.name,
        'messages': context_messages,
        'participants': participants,
    }

    try:
        response = requests.post(
            f'{agent_url}/internal/group-chat/respond/',
            json=payload,
            headers={
                'X-Internal-Service': 'shizuha-connect',
                'Content-Type': 'application/json',
            },
            timeout=30,
        )
        if response.status_code == 202:
            logger.info(
                f"Agent {agent_uuid} notified of mention in conversation {conversation_id}"
            )
        else:
            logger.warning(
                f"Agent service returned {response.status_code} for mention: {response.text[:200]}"
            )
    except requests.exceptions.ConnectionError:
        logger.warning("Agent service unavailable, retrying...")
        try:
            self.retry(countdown=15)
        except self.MaxRetriesExceededError:
            logger.error(
                f"Failed to notify agent {agent_uuid} after retries"
            )
    except Exception as e:
        logger.error(f"Error notifying agent {agent_uuid}: {e}")


# Daily Standup Automation (PLAT-401 / PLAT-239)

DEFAULT_STANDUP_PROMPT = (
    "🌅 **Daily Standup** — 3 things:\n"
    "1. What did you complete since last standup?\n"
    "2. What are you working on today?\n"
    "3. Any blockers or help needed?\n\n"
    "Reply in this thread. Shizuha reads responses at the weekly council (EVOL-8)."
)


def _daily_standup_client_message_id(conversation_id, local_date):
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"shizuha-connect:daily-standup:{conversation_id}:{local_date.isoformat()}",
    )


@shared_task
def post_daily_standup_prompt():
    """PLAT-401: post the daily standup prompt to the Daily Standup group chat.

    Scheduled via Celery beat (weekdays 09:17 IST). The poster identity is the
    CoS (shizuha) — resolved from the target group's participant list so no
    user_id/agent_id is hardcoded; the message is created + WS-broadcast exactly
    like an agent message (mirrors views/internal_api.py). Fail-soft: if the
    conversation or poster isn't found, it logs a warning and skips rather than
    erroring the beat. All knobs are settings/env-configurable.
    """
    from connect.models import Conversation, ConversationParticipant, DirectMessage
    from connect.serializers.messaging import DirectMessageSerializer
    from connect.views.internal_api import _safe_broadcast

    conv_id = getattr(settings, 'STANDUP_CONVERSATION_ID', '') or ''
    poster_email = getattr(settings, 'STANDUP_POSTER_EMAIL', 'shizuha@shizuha.com')
    content = getattr(settings, 'STANDUP_PROMPT', DEFAULT_STANDUP_PROMPT) or DEFAULT_STANDUP_PROMPT

    if not conv_id:
        logger.warning("Daily standup: STANDUP_CONVERSATION_ID not configured; skipping.")
        return "skipped: no conversation configured"

    conversation = Conversation.objects.filter(pk=conv_id).first()
    if not conversation:
        logger.warning("Daily standup: conversation %s not found; skipping.", conv_id)
        return f"skipped: conversation {conv_id} not found"

    # Resolve the poster from the group's own participants (poster must be a
    # participant to post, same invariant the internal agent-message API enforces).
    poster = (
        ConversationParticipant.objects
        .filter(conversation=conversation, has_left=False, user_email__iexact=poster_email)
        .first()
    )
    if not poster:
        logger.warning(
            "Daily standup: poster %s is not a participant of %s; skipping.",
            poster_email, conv_id,
        )
        return f"skipped: poster {poster_email} not a participant"

    poster_name = poster.user_name or 'Shizuha'
    client_message_id = _daily_standup_client_message_id(conversation.pk, timezone.localdate())
    existing = DirectMessage.objects.filter(
        conversation=conversation,
        client_message_id=client_message_id,
    ).first()
    if existing:
        logger.info(
            "Daily standup prompt already posted to %s for %s (message %s)",
            conv_id, timezone.localdate(), existing.id,
        )
        return f"skipped duplicate standup for {conv_id} on {timezone.localdate()}"

    try:
        message = DirectMessage.objects.create(
            conversation=conversation,
            sender_id=poster.user_id,
            sender_email=poster.user_email,
            sender_name=poster_name,
            message_type='text',
            content=content,
            client_message_id=client_message_id,
            sender_is_agent=(poster.participant_type == 'agent'),
            agent_id=poster.agent_id,
        )
    except IntegrityError:
        existing = DirectMessage.objects.filter(
            conversation=conversation,
            client_message_id=client_message_id,
        ).first()
        if existing:
            logger.info(
                "Daily standup prompt race collapsed for %s on %s (message %s)",
                conv_id, timezone.localdate(), existing.id,
            )
            return f"skipped duplicate standup for {conv_id} on {timezone.localdate()}"
        raise

    # Update preview + human unread counts (post_save signal handles message_count).
    conversation.refresh_from_db()
    first_name = poster_name.split()[0] if poster_name else 'Shizuha'
    Conversation.objects.filter(pk=conversation.pk).update(
        last_message_preview=f"{first_name}: {content}"[:100]
    )
    ConversationParticipant.objects.filter(
        conversation=conversation, has_left=False, participant_type='human',
    ).update(unread_count=models.F('unread_count') + 1)

    # Broadcast to the chat group over WS, then push to offline participants.
    message_data = DirectMessageSerializer(message).data
    _safe_broadcast(
        f'chat_{conversation.id}',
        {'type': 'new_message', 'message_data': message_data},
    )
    send_dm_notification.delay(str(message.id))

    logger.info(
        "Daily standup prompt posted to %s as %s (message %s)",
        conv_id, poster_email, message.id,
    )
    return f"posted standup to {conv_id} as {poster_email}"


# Media Processing Tasks

@shared_task
def process_post_media(media_id: str):
    """
    Process uploaded media:
    - Generate thumbnails
    - Extract dimensions
    - Transcode video if needed
    """
    from connect.models import PostMedia

    try:
        media = PostMedia.objects.get(pk=media_id)
    except PostMedia.DoesNotExist:
        return

    # TODO: Implement actual media processing
    # For now, just mark as processed
    media.is_processed = True
    media.save()

    logger.info(f"Processed media {media_id}")


@shared_task
def process_story_media(story_id: str):
    """Process story media."""
    from connect.models import Story

    try:
        story = Story.objects.get(pk=story_id)
    except Story.DoesNotExist:
        return

    # TODO: Implement story media processing
    logger.info(f"Processed story {story_id}")


# Trending & Analytics Tasks

@shared_task
def calculate_trending_topics():
    """
    Calculate trending hashtags/topics.
    Runs every 15 minutes.
    """
    from connect.models import Hashtag, TrendingTopic, PostHashtag
    from django.db.models import Count

    logger.info("Calculating trending topics")

    now = timezone.now()
    # On a sparse/new platform a 6h window is almost always empty; a 7-day
    # window surfaces genuinely-recent trends while the UI covers still-empty
    # cases with onboarding prompts. Env-tunable.
    window_days = int(os.environ.get('CONNECT_TRENDING_WINDOW_DAYS', '7') or 7)
    window_start = now - timedelta(days=window_days)

    # Count hashtag usage in the window AND distinct authors (CON-102 diversity):
    # rank by distinct authors first so a tag many people use outranks one spammed
    # by a single user — a 1-author tag is not a real "trend".
    trending_hashtags = PostHashtag.objects.filter(
        created_at__gte=window_start
    ).values('hashtag').annotate(
        count=Count('id'),
        authors=Count('post__author_id', distinct=True),
    ).order_by('-authors', '-count')[:20]

    # Clear old trending topics
    TrendingTopic.objects.filter(window_end__lt=now).delete()

    # Create new trending topics
    for rank, item in enumerate(trending_hashtags, 1):
        try:
            hashtag = Hashtag.objects.get(pk=item['hashtag'])
            TrendingTopic.objects.update_or_create(
                hashtag=hashtag,
                location='global',
                defaults={
                    'name': hashtag.name,
                    'post_count': item['count'],
                    'rank': rank,
                    'score': item['count'],
                    'velocity': 0,  # TODO: Calculate velocity
                    'window_start': window_start,
                    'window_end': now + timedelta(minutes=15),
                }
            )
        except Hashtag.DoesNotExist:
            continue

    logger.info(f"Updated {len(trending_hashtags)} trending topics")


@shared_task
def batch_update_profile_stats():
    """
    Update denormalized follower/post counts for all profiles.
    Runs hourly to catch any missed signal updates.
    """
    from connect.models import SocialProfile, Follow, Post
    from django.db.models import Count

    logger.info("Batch updating profile stats")

    # Update follower counts
    for profile in SocialProfile.objects.all():
        followers = Follow.objects.filter(following_id=profile.user_id).count()
        following = Follow.objects.filter(follower_id=profile.user_id).count()
        posts = Post.objects.filter(author_id=profile.user_id).count()

        SocialProfile.objects.filter(pk=profile.pk).update(
            followers_count=followers,
            following_count=following,
            posts_count=posts
        )


# Cleanup Tasks

@shared_task
def cleanup_stale_presence_sessions():
    """Transition missed-heartbeat presence sessions stale -> offline."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.contrib.auth import get_user_model
    from connect.services.presence import cleanup_stale_presence, publish_presence_bridge

    snapshots = cleanup_stale_presence()
    if not snapshots:
        return {'updated_users': 0}

    channel_layer = get_channel_layer()
    # Fallback org source only for sessions that predate org persistence; the
    # snapshot's organization_id (captured from the live JWT) is authoritative
    # and is the only correct tenant for federated users with no local org.
    users = {
        user.id: user
        for user in get_user_model().objects.filter(id__in=[snapshot.user_id for snapshot in snapshots])
    }
    for snapshot in snapshots:
        try:
            event = snapshot.as_event()
            event['type'] = 'presence_update'
            async_to_sync(channel_layer.group_send)('presence_global', event)
            user = users.get(snapshot.user_id)
            org_id = snapshot.organization_id
            if org_id is None:
                org_id = getattr(user, 'organization_id', 1) if user else 1
            publish_presence_bridge(snapshot, org_id=org_id)
        except Exception:
            logger.debug("Failed to broadcast stale presence cleanup for user %s", snapshot.user_id)

    return {'updated_users': len(snapshots)}


@shared_task
def expire_stories():
    """
    Delete expired stories and their media.
    Runs every hour.
    """
    from connect.models import Story

    now = timezone.now()
    expired = Story.objects.filter(expires_at__lt=now)
    count = expired.count()

    # TODO: Delete media from storage
    expired.delete()

    logger.info(f"Expired {count} stories")


@shared_task
def cleanup_old_feed_items():
    """
    Remove old feed items to prevent table bloat.
    Runs daily.
    """
    from connect.models import FeedItem

    # Keep last 500 items per user
    cutoff = timezone.now() - timedelta(days=7)
    deleted, _ = FeedItem.objects.filter(created_at__lt=cutoff).delete()

    logger.info(f"Cleaned up {deleted} old feed items")


@shared_task
def ensure_participant_social_profile(user_id: int):
    """
    CON-52: ensure a conversation participant has a searchable SocialProfile.

    Agents are messageable by username but never get a Connect profile, so they
    are invisible to people-search and render blank in conversation lists. This
    runs off the message-send request path (enqueued from a ConversationParticipant
    post_save signal) and resolves the agent's display fields from shizuha-id.
    """
    from connect.services.profile_sync import ensure_social_profile

    ensure_social_profile(user_id)



def _evaluate_security_anomaly_event(event: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a raw Connect security event in the worker.

    PLAT-1237 keeps the request hook to field-copying only. Any cross-service
    identity enrichment and fan-out aggregation belongs here, off the DM hot
    path.
    """
    if event.get('reasons'):
        event['event_type'] = event.get('event_type') or 'connect.direct_message.anomaly'
        try:
            from connect.metrics import record_connect_security_event_evaluated
            record_connect_security_event_evaluated('anomaly')
        except Exception:
            pass
        return event
    if event.get('event_type') != 'connect.direct_message.event':
        return event

    from connect.models import DirectMessage
    from connect.services.security_monitor import _as_int, _identity_org_ids, normalize_org_ids

    sender_id = _as_int(event.get('sender_user_id'))
    recipient_id = _as_int(event.get('recipient_user_id'))
    sender_org_ids = normalize_org_ids(event.get('sender_org_ids'))
    recipient_org_ids = normalize_org_ids(event.get('recipient_org_ids'))

    if not sender_org_ids or not recipient_org_ids:
        identity_orgs = _identity_org_ids(uid for uid in (sender_id, recipient_id) if uid)
        if sender_id is not None:
            sender_org_ids = sender_org_ids or identity_orgs.get(sender_id, set())
        if recipient_id is not None:
            recipient_org_ids = recipient_org_ids or identity_orgs.get(recipient_id, set())

    shared_org_ids = sorted(sender_org_ids & recipient_org_ids)
    disjoint_known_orgs = bool(sender_org_ids and recipient_org_ids and not shared_org_ids)
    reasons: list[str] = []
    if disjoint_known_orgs:
        reasons.append('cross_org_direct_message')
        if event.get('sender_is_agent'):
            reasons.append('internal_agent_external_user_dm')

    fanout_threshold = int(getattr(settings, 'CONNECT_SECURITY_DM_FANOUT_THRESHOLD', 20) or 0)
    fanout_window_seconds = int(getattr(settings, 'CONNECT_SECURITY_DM_FANOUT_WINDOW_SECONDS', 300) or 0)
    fanout_count = None
    if sender_id is not None and fanout_threshold > 0 and fanout_window_seconds > 0:
        since = timezone.now() - timedelta(seconds=fanout_window_seconds)
        fanout_count = (
            DirectMessage.objects.filter(
                sender_id=sender_id,
                conversation__conversation_type='direct',
                created_at__gte=since,
            )
            .values('conversation_id')
            .distinct()
            .count()
        )
        if fanout_count >= fanout_threshold:
            reasons.append('abnormal_direct_message_fanout')

    event.update({
        'event_type': 'connect.direct_message.anomaly',
        'reasons': sorted(set(reasons)),
        'sender_org_ids': sorted(sender_org_ids),
        'recipient_org_ids': sorted(recipient_org_ids),
        'shared_org_ids': shared_org_ids,
        'fanout_count': fanout_count,
        'fanout_window_seconds': fanout_window_seconds,
        'evaluated_at': timezone.now().isoformat(),
    })
    try:
        from connect.metrics import record_connect_security_event_evaluated
        record_connect_security_event_evaluated('anomaly' if event['reasons'] else 'no_anomaly')
    except Exception:
        pass
    return event

def _resolve_security_alert_recipient_id():
    configured = getattr(settings, 'CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID', '') or ''
    if configured:
        try:
            return int(configured)
        except (TypeError, ValueError):
            logger.warning("CONNECT_SECURITY_ALERT_RECIPIENT_USER_ID is not an integer: %r", configured)
            return None

    username = getattr(settings, 'CONNECT_SECURITY_ALERT_RECIPIENT_USERNAME', '') or ''
    if not username:
        return None
    id_base = getattr(settings, 'SHIZUHA_ID_API_URL', 'http://shizuha-id:8001/api')
    try:
        resp = requests.get(
            f"{id_base}/internal/users/by-username/",
            params={'username': username},
            headers={'X-Internal-Service': 'shizuha-connect'},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            return int(data.get('id') or data.get('user_id'))
        logger.warning("security alert recipient lookup for %s returned %s", username, resp.status_code)
    except Exception as exc:
        logger.warning("security alert recipient lookup for %s failed: %s", username, exc)
    return None


def _format_security_anomaly_alert(event: dict) -> str:
    if event.get('event_type') == 'platform.boundary_denial':
        return '\n'.join([
            '🚨 Platform boundary denial detected',
            f"Reason: {event.get('reason')}",
            f"Service/operation: {event.get('service')} / {event.get('operation')}",
            f"Actor: {event.get('actor_username')} (user_id={event.get('actor_user_id')}, orgs={event.get('source_org_ids')})",
            f"Target org: {event.get('target_org_id')}",
            f"Route: {event.get('request_method')} {event.get('route')}",
            f"Correlation: {event.get('correlation_id')}",
            f"Detected at: {event.get('detected_at')}",
        ])
    reasons = ', '.join(event.get('reasons') or ['unknown'])
    sender = event.get('sender_username') or event.get('sender_email') or event.get('sender_user_id')
    recipient = event.get('recipient_name') or event.get('recipient_email') or event.get('recipient_user_id')
    lines = [
        "🚨 Connect runtime anomaly detected",
        f"Reason(s): {reasons}",
        f"Event: {event.get('event_type')}",
        f"Sender: {sender} (user_id={event.get('sender_user_id')}, agent={event.get('sender_is_agent')}, orgs={event.get('sender_org_ids')})",
        f"Recipient: {recipient} (user_id={event.get('recipient_user_id')}, orgs={event.get('recipient_org_ids')})",
        f"Conversation: {event.get('conversation_id')}",
    ]
    if event.get('message_id'):
        lines.append(f"Message: {event.get('message_id')}")
    if event.get('fanout_count') is not None:
        lines.append(
            f"Fan-out: {event.get('fanout_count')} direct conversations in "
            f"{event.get('fanout_window_seconds')}s"
        )
    lines.append(f"Detected at: {event.get('detected_at')}")
    return '\n'.join(lines)


def _suppress_repeated_boundary_alert(event: dict) -> bool:
    """Atomically keep the first alert and suppress repeats for a short window."""
    if event.get('event_type') != 'platform.boundary_denial':
        return False
    window = int(getattr(settings, 'CONNECT_SECURITY_EVENT_REPEAT_WINDOW_SECONDS', 300))
    if window <= 0:
        return False
    fingerprint = '|'.join(str(event.get(key) or '') for key in (
        'service', 'operation', 'reason', 'actor_user_id', 'target_org_id',
    ))
    digest = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    from connect.services.security_monitor import claim_security_dedupe
    return not claim_security_dedupe(
        f'connect:security:boundary-alert:{digest}',
        ttl_seconds=window,
    )


@shared_task
def relay_security_event_outbox():
    """Periodic, bounded recovery relay for committed CON-217 outbox rows."""
    from connect.services.security_outbox import relay_due_security_events

    return relay_due_security_events()


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def process_security_event_outbox(self, outbox_id: str):
    """Converge one durable event to an explicit terminal outcome.

    The task accepts only the Postgres UUID.  Payloads never become broker
    acceptance truth, and an expired worker lease makes process loss replayable.
    """
    from connect.models import SecurityEventOutbox
    from connect.services.security_monitor import _enabled
    from connect.services.security_outbox import (
        claim_for_delivery,
        claim_repeat_window,
        deliver_security_dm,
        mark_terminal,
        schedule_delivery_retry,
    )

    worker_id = str(getattr(self.request, 'id', '') or uuid.uuid4())
    event, claim_status = claim_for_delivery(outbox_id, worker_id=worker_id)
    if event is None:
        return {'status': claim_status}

    try:
        if not _enabled():
            mark_terminal(outbox_id, SecurityEventOutbox.Status.DISABLED, reason='alerts_disabled')
            return {'status': SecurityEventOutbox.Status.DISABLED}

        event = _evaluate_security_anomaly_event(dict(event))
        if not event.get('reasons'):
            mark_terminal(
                outbox_id,
                SecurityEventOutbox.Status.SUPPRESSED_NO_ANOMALY,
                reason='no_anomaly',
            )
            return {'status': SecurityEventOutbox.Status.SUPPRESSED_NO_ANOMALY}

        if claim_repeat_window(outbox_id, event):
            try:
                from connect.metrics import record_connect_security_event_evaluated
                record_connect_security_event_evaluated('repeat_suppressed')
            except Exception:
                pass
            mark_terminal(
                outbox_id,
                SecurityEventOutbox.Status.SUPPRESSED_REPEAT,
                reason='repeat_window',
            )
            return {'status': SecurityEventOutbox.Status.SUPPRESSED_REPEAT}

        recipient_user_id = _resolve_security_alert_recipient_id()
        if not recipient_user_id:
            mark_terminal(
                outbox_id,
                SecurityEventOutbox.Status.NO_RECIPIENT,
                reason='no_recipient',
            )
            try:
                from connect.metrics import record_connect_security_alert_failure
                record_connect_security_alert_failure('no_recipient')
            except Exception:
                pass
            return {'status': SecurityEventOutbox.Status.NO_RECIPIENT}

        message, created = deliver_security_dm(
            outbox_id,
            recipient_user_id=recipient_user_id,
            content=_format_security_anomaly_alert(event),
        )
        try:
            from connect.metrics import record_connect_security_alert_sent
            record_connect_security_alert_sent()
        except Exception:
            pass
        return {
            'status': SecurityEventOutbox.Status.SENT,
            'message_id': str(message.pk),
            'created': created,
        }
    except Exception as exc:
        status = schedule_delivery_retry(outbox_id, exc)
        try:
            from connect.metrics import record_connect_security_alert_failure
            record_connect_security_alert_failure(
                'dead_lettered' if status == SecurityEventOutbox.Status.DEAD_LETTERED else 'delivery_retry',
            )
        except Exception:
            pass
        logger.exception('CON-217 security outbox delivery failed id=%s status=%s', outbox_id, status)
        return {'status': status, 'error_class': exc.__class__.__name__}


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def send_security_anomaly_alert(self, event: dict):
    """DM the configured security lead about a Connect runtime anomaly.

    This task is intentionally best-effort. The user-facing request that emitted
    the event must not fail if the security alert channel is temporarily down.
    """
    event = _evaluate_security_anomaly_event(dict(event or {}))
    if not event.get('reasons'):
        logger.info("Connect security event evaluated with no anomaly: %s", event)
        return {'status': 'skipped', 'reason': 'no_anomaly'}

    # A Celery retry is the same logical delivery and already owns the repeat
    # fingerprint claimed by its first attempt.  Only independent task
    # deliveries participate in repeat suppression; otherwise the first
    # transient send failure would suppress its own retry for the full window.
    delivery_retry = int(getattr(self.request, 'retries', 0) or 0) > 0
    if not delivery_retry and _suppress_repeated_boundary_alert(event):
        try:
            from connect.metrics import record_connect_security_event_evaluated
            record_connect_security_event_evaluated('repeat_suppressed')
        except Exception:
            pass
        return {'status': 'skipped', 'reason': 'repeat_suppressed'}

    recipient_user_id = _resolve_security_alert_recipient_id()
    if not recipient_user_id:
        try:
            from connect.metrics import record_connect_security_alert_failure
            record_connect_security_alert_failure('no_recipient')
        except Exception:
            pass
        logger.warning("Connect security anomaly detected but no alert recipient is configured: %s", event)
        return {'status': 'skipped', 'reason': 'no_recipient'}

    try:
        from connect.models import ConversationParticipant, DirectMessage
        from connect.serializers.messaging import DirectMessageSerializer
        from connect.views.internal_api import (
            SYSTEM_USERNAME,
            _get_or_create_system_dm,
            _get_system_user_id,
            _safe_broadcast,
        )

        system_user_id = _get_system_user_id()
        conversation, _created = _get_or_create_system_dm(recipient_user_id)
        content = _format_security_anomaly_alert(event)
        message = DirectMessage.objects.create(
            conversation=conversation,
            sender_id=system_user_id,
            sender_email='system@shizuha.com',
            sender_name='Connect Security Monitor',
            sender_username=SYSTEM_USERNAME,
            message_type='text',
            content=content,
        )
        conversation.refresh_from_db()
        _safe_broadcast(
            f'chat_{conversation.id}',
            {'type': 'new_message', 'message_data': DirectMessageSerializer(message).data},
        )
        ConversationParticipant.objects.filter(
            conversation=conversation,
            has_left=False,
        ).exclude(user_id=system_user_id).update(unread_count=models.F('unread_count') + 1)
        try:
            from connect.metrics import record_connect_security_alert_sent
            record_connect_security_alert_sent()
        except Exception:
            pass
        logger.warning("Connect security anomaly alert sent to user_id=%s: %s", recipient_user_id, event)
        return {'status': 'sent', 'recipient_user_id': recipient_user_id, 'message_id': str(message.id)}
    except Exception as exc:
        try:
            from connect.metrics import record_connect_security_alert_failure
            record_connect_security_alert_failure('send_exception')
        except Exception:
            pass
        logger.exception("failed to send Connect security anomaly alert")
        raise self.retry(exc=exc)


_WAKE_DEDUP: dict[int, float] = {}


def _wake_agent_runtime(participant, message):
    """Ask Hive to start a stopped agent that just received a DM (HIVE-602).

    Shared-secret service call; 60s in-process dedup per recipient so message
    bursts don't hammer Hive. Hive answers 'already_running' idempotently.
    """
    import os as _os
    import time as _time

    url = (_os.environ.get('HIVE_CONNECT_WAKE_URL') or '').strip()
    token = (_os.environ.get('HIVE_CONNECT_WAKE_TOKEN') or '').strip()
    if not url or not token:
        return
    now_s = _time.monotonic()
    last = _WAKE_DEDUP.get(participant.user_id)
    if last is not None and now_s - last < 60:
        return
    _WAKE_DEDUP[participant.user_id] = now_s
    email = (getattr(participant, 'user_email', '') or '').strip().lower()
    if not email:
        return
    try:
        import requests as _requests
        resp = _requests.post(
            url,
            json={'email': email, 'reason': f'connect_dm:{message.conversation_id}'},
            headers={'X-Hive-Wake-Token': token},
            timeout=3,
        )
        logger.info('HIVE-602 connect-wake %s -> %s %s', email, resp.status_code, resp.text[:80])
    except Exception as e:
        logger.warning('HIVE-602 connect-wake failed for %s: %s', email, e)

"""CON-7 privacy/blocking policy helpers.

This module is the single server-side policy surface for block/privacy checks
that cut across REST, websocket, bridge, notification, and search/read paths.
A block in either direction is treated as a hard privacy boundary.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.conf import settings
from django.db.models import Q, QuerySet

from connect.models import Block, Connection, Conversation, ConversationParticipant, Follow, Mention, Post, SocialProfile


def _as_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None




def normalize_org_ids(source) -> set[int]:
    """Extract organization ids from JWT/user/identity payload variants."""
    if not source:
        return set()
    if isinstance(source, (str, int)):
        parsed = _as_int(source)
        return {parsed} if parsed is not None else set()
    # CON-133: callers pass already-normalized sets/lists of ids back in
    # (users_share_org re-normalizes its actor/recipient overrides); without
    # this branch a top-level iterable fell through to the attr lookup and
    # silently normalized to the empty set — failing the gate closed.
    if isinstance(source, (list, tuple, set, frozenset)):
        collected: set[int] = set()
        for item in source:
            collected |= normalize_org_ids(item)
        return collected
    ids: set[int] = set()
    if isinstance(source, dict):
        for key in (
            "organization_id",
            "org_id",
            "organization",
            "org",
            "tenant_id",
            "primary_organization_id",
        ):
            parsed = _as_int(source.get(key))
            if parsed is not None:
                ids.add(parsed)
        for key in ("organization_ids", "org_ids", "organizations", "organization_memberships", "memberships"):
            value = source.get(key)
            if isinstance(value, dict):
                iterable = value.keys()
            elif isinstance(value, (list, tuple, set)):
                iterable = value
            else:
                continue
            for item in iterable:
                if isinstance(item, dict):
                    raw = item.get("id") or item.get("organization_id") or item.get("org_id")
                else:
                    raw = item
                parsed = _as_int(raw)
                if parsed is not None:
                    ids.add(parsed)
        return ids
    for attr in ("organization_id", "org_id", "tenant_id"):
        parsed = _as_int(getattr(source, attr, None))
        if parsed is not None:
            ids.add(parsed)
    return ids


def _membership_org_ids(user_id) -> set[int] | None:
    """Active org memberships for a user, from the admin service.

    CON-129 convergence: this delegates to ``org_tenancy.get_user_org_ids`` so
    there is a SINGLE server-side org-membership resolution + cache path across
    Connect, rather than the parallel lookup the CON-133 hotfix introduced here.
    org_tenancy is the canonical resolver (admin ``OrganizationMembership`` via
    ``GET /internal/users/<id>/memberships/`` — the projection shizuha-id stamps
    into JWTs as ``organization_memberships``; the legacy single
    ``organization_id`` field is deliberately NOT consulted) and it carries the
    two-tier fresh/stale cache so a short admin blip does not take messaging
    down. Returns ``None`` when membership cannot be established; callers fail
    closed.
    """
    from connect.services import org_tenancy

    org_ids = org_tenancy.get_user_org_ids(user_id)
    return set(org_ids) if org_ids is not None else None


def users_share_org(actor_id, recipient_id, *, actor_org_ids=None, recipient_org_ids=None) -> bool:
    """Return whether two users share at least one active org membership.

    Direct messaging is internal-only across shared organizations. Unknown orgs
    fail closed by default, so a missing identity/org lookup cannot silently
    reopen the CON-129 cross-tenant DM gap.
    """
    a = _as_int(actor_id)
    b = _as_int(recipient_id)
    if a is None or b is None:
        return False
    if a == b:
        return True
    if not bool(getattr(settings, "CONNECT_REQUIRE_SHARED_ORG_FOR_DM", True)):
        return True
    actor_orgs = normalize_org_ids(actor_org_ids) if actor_org_ids is not None else _membership_org_ids(a)
    recipient_orgs = normalize_org_ids(recipient_org_ids) if recipient_org_ids is not None else _membership_org_ids(b)
    return bool(actor_orgs and recipient_orgs and actor_orgs.intersection(recipient_orgs))


def users_connected(user_a_id, user_b_id) -> bool:
    """Return True when two users have an accepted explicit connection."""
    a = _as_int(user_a_id)
    b = _as_int(user_b_id)
    if a is None or b is None or a == b:
        return False
    low, high = sorted((a, b))
    return Connection.objects.filter(user_a_id=low, user_b_id=high, status='accepted').exists()


def user_ids_sharing_org(viewer_id, candidate_user_ids) -> set[int]:
    """Candidate user ids that share an active org with ``viewer_id``.

    Uses one membership lookup for the viewer and passes it through to the
    canonical gate for each candidate. Unknown org membership fails closed; the
    viewer's own id is always visible to itself.
    """
    viewer = _as_int(viewer_id)
    if viewer is None:
        return set()
    candidate_ids = {_as_int(uid) for uid in candidate_user_ids}
    candidate_ids.discard(None)
    if not candidate_ids:
        return set()
    actor_orgs = _membership_org_ids(viewer)
    allowed = {viewer} if viewer in candidate_ids else set()
    for candidate_id in candidate_ids:
        if candidate_id == viewer:
            continue
        if users_share_org(viewer, candidate_id, actor_org_ids=actor_orgs):
            allowed.add(candidate_id)
    return allowed


def profile_visible_to_user(viewer_id, profile, *, actor_org_ids=None) -> bool:
    """CON-142 read-symmetry gate for profile/detail/list surfaces."""
    viewer = _as_int(viewer_id)
    owner = _as_int(getattr(profile, 'user_id', None))
    if viewer is None or owner is None:
        return False
    if viewer == owner:
        return True
    if blocked_pair_exists(viewer, owner):
        return False
    visibility = getattr(profile, 'profile_visibility', 'public') or 'public'
    if visibility == 'private':
        return False
    if users_connected(viewer, owner):
        return True
    if visibility == 'connections':
        return False
    return users_share_org(viewer, owner, actor_org_ids=actor_org_ids)


def filter_profiles_readable_to(viewer_id, queryset: QuerySet[SocialProfile]) -> QuerySet[SocialProfile]:
    """Filter profiles to rows the viewer may read/enumerate.

    ``filter_profiles_visible_to`` is intentionally block-only legacy privacy;
    CON-142 read surfaces need the stronger object/read-symmetry gate here.
    """
    viewer = _as_int(viewer_id)
    if viewer is None:
        return queryset.none()
    actor_orgs = _membership_org_ids(viewer)
    allowed_pks = [
        profile.pk
        for profile in queryset
        if profile_visible_to_user(viewer, profile, actor_org_ids=actor_orgs)
    ]
    return queryset.filter(pk__in=allowed_pks)


def post_visible_to_user(viewer_id, post, *, actor_org_ids=None) -> bool:
    """CON-142 read-symmetry gate for post/feed/search surfaces."""
    viewer = _as_int(viewer_id)
    author = _as_int(getattr(post, 'author_id', None))
    if viewer is None or author is None:
        return False
    if viewer == author:
        return True
    if blocked_pair_exists(viewer, author):
        return False
    same_org = users_share_org(viewer, author, actor_org_ids=actor_org_ids)
    visibility = getattr(post, 'visibility', 'public') or 'public'
    if visibility == 'public':
        return same_org or users_connected(viewer, author)
    if visibility == 'followers':
        return same_org and Follow.objects.filter(follower_id=viewer, following_id=author).exists()
    if visibility == 'connections':
        return users_connected(viewer, author)
    if visibility == 'mentioned':
        return same_org and Mention.objects.filter(post=post, mentioned_user_id=viewer).exists()
    return False


def filter_posts_readable_to(viewer_id, queryset: QuerySet[Post]) -> QuerySet[Post]:
    """Filter posts to rows the viewer may read/enumerate."""
    viewer = _as_int(viewer_id)
    if viewer is None:
        return queryset.none()
    actor_orgs = _membership_org_ids(viewer)
    allowed_pks = [
        post.pk
        for post in queryset
        if post_visible_to_user(viewer, post, actor_org_ids=actor_orgs)
    ]
    return queryset.filter(pk__in=allowed_pks)


def filter_participants_readable_to(viewer_id, queryset: QuerySet[ConversationParticipant]) -> QuerySet[ConversationParticipant]:
    """Hide legacy cross-org human roster entries from non-shared viewers."""
    viewer = _as_int(viewer_id)
    if viewer is None:
        return queryset.none()
    participants = list(queryset)
    human_ids = [p.user_id for p in participants if getattr(p, 'participant_type', 'human') == 'human']
    shared = user_ids_sharing_org(viewer, human_ids)
    allowed_pks = [
        p.pk for p in participants
        if p.user_id == viewer
        or getattr(p, 'participant_type', 'human') != 'human'
        or p.user_id in shared
    ]
    return queryset.filter(pk__in=allowed_pks)

def blocked_pair_exists(user_a_id, user_b_id) -> bool:
    """Return True when either user has blocked the other."""
    a = _as_int(user_a_id)
    b = _as_int(user_b_id)
    if a is None or b is None or a == b:
        return False
    return Block.objects.filter(
        Q(blocker_id=a, blocked_id=b) | Q(blocker_id=b, blocked_id=a)
    ).exists()


def blocked_counterparty_ids(user_id) -> set[int]:
    """All users separated from ``user_id`` by a block in either direction."""
    uid = _as_int(user_id)
    if uid is None:
        return set()
    rows = Block.objects.filter(Q(blocker_id=uid) | Q(blocked_id=uid)).values_list(
        "blocker_id", "blocked_id"
    )
    blocked = set()
    for blocker_id, blocked_id in rows:
        other = blocked_id if blocker_id == uid else blocker_id
        if other != uid:
            blocked.add(int(other))
    return blocked


def active_participant_ids(conversation_or_id) -> list[int]:
    conversation_id = getattr(conversation_or_id, "id", conversation_or_id)
    return list(
        ConversationParticipant.objects.filter(
            conversation_id=conversation_id,
            has_left=False,
        ).values_list("user_id", flat=True)
    )


def conversation_blocked_for_user(conversation_or_id, user_id) -> bool:
    """Whether ``user_id`` is separated from any active participant by a block."""
    uid = _as_int(user_id)
    if uid is None:
        return True
    participant_ids = [pid for pid in active_participant_ids(conversation_or_id) if int(pid) != uid]
    if not participant_ids:
        return False
    return Block.objects.filter(
        Q(blocker_id=uid, blocked_id__in=participant_ids)
        | Q(blocked_id=uid, blocker_id__in=participant_ids)
    ).exists()


def conversation_has_any_block(conversation_or_id) -> bool:
    """Whether any active participant pair in a conversation is blocked."""
    ids = active_participant_ids(conversation_or_id)
    if len(ids) < 2:
        return False
    return Block.objects.filter(blocker_id__in=ids, blocked_id__in=ids).exists()


def visible_conversation_ids_for_user(user_id, *, include_archived=False):
    """Conversation ids visible to ``user_id`` after membership + block policy."""
    uid = _as_int(user_id)
    if uid is None:
        return Conversation.objects.none().values_list("id", flat=True)
    participants = ConversationParticipant.objects.filter(user_id=uid, has_left=False)
    if not include_archived:
        participants = participants.filter(is_archived=False)
    candidate_ids = list(participants.values_list("conversation_id", flat=True))
    if not candidate_ids:
        return []
    blocked_ids = blocked_counterparty_ids(uid)
    if not blocked_ids:
        return candidate_ids
    blocked_conversation_ids = set(
        ConversationParticipant.objects.filter(
            conversation_id__in=candidate_ids,
            has_left=False,
            user_id__in=blocked_ids,
        ).values_list("conversation_id", flat=True)
    )
    return [cid for cid in candidate_ids if cid not in blocked_conversation_ids]


def can_access_conversation(conversation_or_id, user_id) -> bool:
    """Membership + block check for read/search/export/actions."""
    uid = _as_int(user_id)
    if uid is None:
        return False
    conversation_id = getattr(conversation_or_id, "id", conversation_or_id)
    if not ConversationParticipant.objects.filter(
        conversation_id=conversation_id,
        user_id=uid,
        has_left=False,
    ).exists():
        return False
    return not conversation_blocked_for_user(conversation_id, uid)


def allowed_delivery_user_ids(message) -> list[int]:
    """Active recipient ids allowed to receive/read a message under block policy."""
    participants = list(
        ConversationParticipant.objects.filter(
            conversation=message.conversation,
            has_left=False,
        ).exclude(user_id=message.sender_id)
    )
    if not participants:
        return []
    blocked_ids = blocked_counterparty_ids(message.sender_id)
    return [p.user_id for p in participants if p.user_id not in blocked_ids]


def actor_can_reach_recipient(
    actor_id,
    recipient_id,
    *,
    actor_org_ids=None,
    recipient_org_ids=None,
    require_shared_org=False,
) -> bool:
    if blocked_pair_exists(actor_id, recipient_id):
        return False
    if not require_shared_org:
        return True
    return users_share_org(
        actor_id,
        recipient_id,
        actor_org_ids=actor_org_ids,
        recipient_org_ids=recipient_org_ids,
    )


def filter_profiles_visible_to(viewer_id, queryset: QuerySet[SocialProfile]) -> QuerySet[SocialProfile]:
    blocked = blocked_counterparty_ids(viewer_id)
    if not blocked:
        return queryset
    return queryset.exclude(user_id__in=blocked)

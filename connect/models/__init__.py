"""
Shizuha Connect Models

This module exports all models for the Connect social networking service.
"""

from .profile import (
    SocialProfile,
    Skill,
    UserSkill,
    Endorsement,
    Experience,
    Education,
)

from .social import (
    Follow,
    Connection,
    Block,
    Mute,
)

from .content import (
    Post,
    PostMedia,
    Hashtag,
    PostHashtag,
    Mention,
)

from .engagement import (
    Reaction,
    Comment,
    CommentLike,
    Bookmark,
    BookmarkCollection,
)

from .messaging import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    DirectMessageIdempotency,
    VerifiedAlertDelivery,
    MessageRead,
    MessageDelivery,
    MessageDeletion,
    MessageStar,
    BridgeWebhookSecret,
    BridgeIdentityMapping,
    BridgeInboundEvent,
    BridgeChannelPolicy,
    BridgeOutboundEvent,
    PresenceSession,
    PresenceStatus,
    NotificationDispatch,
    NotificationPreference,
    BridgeDeliveryCooldown,
    SecurityPipelineMetricHealth,
    NOTIFICATION_CLASSES,
    NOTIFICATION_STATES,
    VALID_NOTIFICATION_CLASSES,
    SecurityEventOutbox,
    SecurityEventRepeatClaim,
    SecurityEventRelayHealth,
)

from .stories import (
    Story,
    StoryView,
    StoryReaction,
)

from .feed import (
    FeedItem,
    TrendingTopic,
)

from .meetings import (
    Meeting,
    MeetingParticipant,
    MeetingInvite,
)

from .integrations import (
    ExternalAccount,
)

# CON-137: register the CON-19 tombstone models. They are defined in
# connect/models/tombstone.py and created by migration 0021_con19_tombstone, but
# were never imported here — so the app registry never loaded them and
# `makemigrations --check` (the deploy.yml migration-gate) saw them as pending
# model DELETEs on every master push, failing the gate and blocking ALL connect
# backend auto-deploys (root cause of the CON-135 merged-but-not-deployed class).
from .tombstone import (
    AccountTombstone,
    TombstonedBlock,
)

__all__ = [
    # Profile
    'SocialProfile',
    'Skill',
    'UserSkill',
    'Endorsement',
    'Experience',
    'Education',
    # Social
    'Follow',
    'Connection',
    'Block',
    'Mute',
    # Content
    'Post',
    'PostMedia',
    'Hashtag',
    'PostHashtag',
    'Mention',
    # Engagement
    'Reaction',
    'Comment',
    'CommentLike',
    'Bookmark',
    'BookmarkCollection',
    # Company
    # Jobs
    # Messaging
    'Conversation',
    'ConversationParticipant',
    'DirectMessage',
    'DirectMessageIdempotency',
    'MessageRead',
    'MessageDelivery',
    'MessageDeletion',
    'MessageStar',
    'BridgeWebhookSecret',
    'BridgeIdentityMapping',
    'BridgeInboundEvent',
    'BridgeChannelPolicy',
    'BridgeOutboundEvent',
    'PresenceSession',
    'PresenceStatus',
    'NotificationDispatch',
    'NotificationPreference',
    'BridgeDeliveryCooldown',
    'SecurityPipelineMetricHealth',
    'SecurityEventOutbox',
    'SecurityEventRepeatClaim',
    'SecurityEventRelayHealth',
    # Stories
    'Story',
    'StoryView',
    'StoryReaction',
    # Feed
    'FeedItem',
    'TrendingTopic',
    # Meetings
    'Meeting',
    'MeetingParticipant',
    'MeetingInvite',
    'ExternalAccount',
]

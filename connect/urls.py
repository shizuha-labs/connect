"""
URL configuration for Shizuha Connect API.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    profile,
    posts,
    feed,
    social,
    messaging,
    stories,
    search,
    meetings,
    internal_api,
    users,
    bridges,
    onboarding,
    suggestions,
    integrations,
)

router = DefaultRouter()

# Profile endpoints
router.register(r'profiles', profile.ProfileViewSet, basename='profile')
router.register(r'skills', profile.SkillViewSet, basename='skill')
router.register(r'experience', profile.ExperienceViewSet, basename='experience')
router.register(r'education', profile.EducationViewSet, basename='education')

# Posts and engagement
router.register(r'posts', posts.PostViewSet, basename='post')
router.register(r'comments', posts.CommentViewSet, basename='comment')

# Companies and jobs

# Messaging
router.register(r'conversations', messaging.ConversationViewSet, basename='conversation')

# Stories
router.register(r'stories', stories.StoryViewSet, basename='story')

# Meetings (Video conferencing)
router.register(r'meetings', meetings.MeetingViewSet, basename='meeting')

urlpatterns = [
    # Health check
    path('health/', feed.health_check, name='health-check'),

    # User identity lookup (CON-2): resolve a username to its canonical Shizuha ID
    # user_id so agents can populate conversation participant_ids without a profile.
    path('users/by-username/<str:username>/uuid/', users.UserByUsernameView.as_view(), name='user-by-username-uuid'),

    # Onboarding wizard (CON-24)
    path('onboarding/status/', onboarding.OnboardingStatusView.as_view(), name='onboarding-status'),
    path('onboarding/steps/<str:step>/complete/', onboarding.OnboardingStepCompleteView.as_view(), name='onboarding-step-complete'),
    path('onboarding/suggestions/', onboarding.SuggestedConnectionsView.as_view(), name='onboarding-suggestions'),

    # "People you might know" feed suggestions (CON-79)
    path('suggestions/', suggestions.SuggestionsView.as_view(), name='suggestions'),

    # DOJO no-spend validation endpoints (DOJO-7 / VEN-55)

    # Feed endpoints
    path('feed/home/', feed.HomeFeedView.as_view(), name='feed-home'),
    path('feed/following/', feed.FollowingFeedView.as_view(), name='feed-following'),
    path('feed/trending/', feed.TrendingFeedView.as_view(), name='feed-trending'),

    # Social graph endpoints
    path('follow/<str:user_identifier>/', social.FollowView.as_view(), name='follow'),
    path('unfollow/<str:user_identifier>/', social.UnfollowView.as_view(), name='unfollow'),
    path('connections/', social.ConnectionListView.as_view(), name='connections'),
    path('connections/requests/', social.ConnectionRequestsView.as_view(), name='connection-requests'),
    path('connections/request/<str:user_identifier>/', social.SendConnectionRequestView.as_view(), name='send-connection-request'),
    path('connections/<uuid:pk>/accept/', social.AcceptConnectionView.as_view(), name='accept-connection'),
    path('connections/<uuid:pk>/reject/', social.RejectConnectionView.as_view(), name='reject-connection'),
    path('block/<str:user_identifier>/', social.BlockView.as_view(), name='block'),
    path('unblock/<str:user_identifier>/', social.UnblockView.as_view(), name='unblock'),
    path('mute/<str:user_identifier>/', social.MuteView.as_view(), name='mute'),
    path('unmute/<str:user_identifier>/', social.UnmuteView.as_view(), name='unmute'),

    # Search endpoints
    path('search/', search.UniversalSearchView.as_view(), name='search'),
    path('search/people/', search.PeopleSearchView.as_view(), name='search-people'),
    path('search/posts/', search.PostSearchView.as_view(), name='search-posts'),
    path('hashtags/trending/', search.TrendingHashtagsView.as_view(), name='trending-hashtags'),

    # Media upload
    path('upload/', messaging.MediaUploadView.as_view(), name='media-upload'),
    path('presence/', messaging.PresenceView.as_view(), name='presence'),
    path('hive/agents/', messaging.HivePersonalAgentsView.as_view(), name='hive-personal-agents'),
    path('hive/agents/conversation/', messaging.HiveAgentConversationView.as_view(), name='hive-agent-conversation'),

    # Direct messaging: send a DM to any platform user by email. Works for
    # both humans and agents as the caller. Creates the Direct conversation
    # on-demand if one doesn't already exist between the two parties.
    path('messaging/dm/', messaging.MessageUserView.as_view(), name='messaging-dm'),
    path('messaging/voice-turn/', messaging.VoiceTurnView.as_view(), name='messaging-voice-turn'),

    # CON-77: the caller's own outbox — recent DMs they have sent (newest-first),
    # so agents can detect/avoid duplicate sends without external state tracking.
    path('messaging/sent/', messaging.SentMessagesView.as_view(), name='messaging-sent'),

    # External channel bridge webhooks (unauthenticated; provider signature verified before processing).
    path('bridges/whatsapp/<str:provider_account_id>/webhook/', bridges.WhatsAppWebhookView.as_view(), name='bridge-whatsapp-webhook'),
    path('bridges/outbound/send/', bridges.OutboundBridgeSendView.as_view(), name='bridge-outbound-send'),
    path('bridges/<str:provider>/<str:provider_account_id>/delivery-callback/', bridges.OutboundBridgeDeliveryCallbackView.as_view(), name='bridge-outbound-delivery-callback'),

    # My profile shortcut
    path('integrations/', integrations.IntegrationListView.as_view(), name='integrations'),
    path('integrations/linkedin/', integrations.LinkedInIntegrationView.as_view(), name='integrations-linkedin'),

    path('me/', profile.MyProfileView.as_view(), name='my-profile'),
    path('me/posts/', profile.MyPostsView.as_view(), name='my-posts'),
    path('me/bookmarks/', profile.MyBookmarksView.as_view(), name='my-bookmarks'),

    # Integration endpoints
    path('share/drive/', posts.ShareDriveFileView.as_view(), name='share-drive'),
    path('share/pulse/', posts.SharePulseTaskView.as_view(), name='share-pulse'),
    path('share/wiki/', posts.ShareWikiPageView.as_view(), name='share-wiki'),
    path('share/notes/', posts.ShareNoteView.as_view(), name='share-note'),

    # Internal API (secured by X-Internal-Service header + Docker network)
    path('internal/agent-connection/',
         internal_api.InternalAgentConnectionView.as_view(), name='internal-agent-connection'),
    path('internal/conversations/<uuid:conversation_id>/agent-message/',
         internal_api.InternalAgentMessageView.as_view(), name='internal-agent-message'),
    path('internal/conversations/<uuid:conversation_id>/agent-typing/',
         internal_api.InternalAgentTypingView.as_view(), name='internal-agent-typing'),
    path('internal/system-message/',
         internal_api.InternalSystemMessageView.as_view(), name='internal-system-message'),
    path('internal/unread-recipients/',
         internal_api.InternalUnreadRecipientsView.as_view(), name='internal-unread-recipients'),
    path('internal/security-events/',
         internal_api.InternalBoundarySecurityEventView.as_view(), name='internal-security-events'),
    path('internal/verified-alert-deliveries/',
         internal_api.InternalVerifiedAlertDeliveryView.as_view(), name='internal-verified-alert-deliveries'),
    path('internal/verified-alert-deliveries/<uuid:receipt_id>/',
         internal_api.InternalVerifiedAlertDeliveryView.as_view(), name='internal-verified-alert-delivery-receipt'),
    path('internal/group-message/',
         internal_api.InternalGroupMessageView.as_view(), name='internal-group-message'),
    path('internal/agents/telemetry/',
         internal_api.AgentTelemetryView.as_view(), name='internal-agent-telemetry'),
    path('internal/agents/config/',
         internal_api.InternalAgentConfigView.as_view(), name='internal-agent-config'),
    path('internal/agents/auth-reject-trigger/',
         internal_api.InternalAgentAuthRejectTriggerView.as_view(), name='internal-agent-auth-reject-trigger'),

    # Router URLs
    path('', include(router.urls)),
]

"""
Messaging serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import (
    Conversation,
    ConversationParticipant,
    DirectMessage,
    MessageDelivery,
    PresenceSession,
    PresenceStatus,
)
from connect.models.profile import SocialProfile
from connect.services.display_names import (
    cached_identity_users,
    is_placeholder_name,
    preferred_participant_label,
)


class PresenceSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PresenceSession
        fields = [
            'id', 'session_key', 'client_id', 'client_type', 'client_label',
            'is_agent', 'status', 'last_heartbeat_at', 'stale_at',
            'offline_at', 'connected_at',
        ]
        read_only_fields = fields


class PresenceStatusSerializer(serializers.ModelSerializer):
    sessions = serializers.SerializerMethodField()

    class Meta:
        model = PresenceStatus
        fields = [
            'user_id', 'status', 'active_session_count',
            'degraded_session_count', 'last_seen_at', 'updated_at',
            'sessions',
        ]
        read_only_fields = fields

    def get_sessions(self, obj):
        request = self.context.get('request')
        if request is None:
            return []
        user = getattr(request, 'user', None)
        is_self = user and not user.is_anonymous and getattr(user, 'id', None) == obj.user_id
        if is_self or (user and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))):
            return (obj.details or {}).get('sessions', [])
        return []


class ConversationParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversationParticipant
        fields = [
            'id', 'user_id', 'user_email', 'user_name', 'is_admin',
            'nickname', 'last_read_at', 'last_read_event_id', 'unread_count',
            'is_muted', 'muted_until', 'notifications_enabled', 'is_pinned',
            'is_archived', 'has_left', 'joined_at',
            'participant_type', 'agent_id', 'agent_role',
        ]
        read_only_fields = [
            'id', 'joined_at', 'participant_type', 'agent_id', 'agent_role',
            'last_read_event_id',
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not is_placeholder_name(data.get('user_name'), instance.user_id):
            return data
        context = self.context if isinstance(self.context, dict) else {}
        profiles = context.setdefault('_social_profiles', {})
        if instance.user_id and instance.user_id not in profiles:
            profile = SocialProfile.objects.filter(user_id=instance.user_id).only(
                'user_id', 'username', 'display_name', 'user_email',
            ).first()
            if profile is not None:
                profiles[instance.user_id] = profile
        identities = cached_identity_users(context, [instance.user_id] if instance.user_id else [])
        label = preferred_participant_label(
            instance, profiles=profiles, identities=identities,
        )
        if label:
            data['user_name'] = label
        return data


class DirectMessageSerializer(serializers.ModelSerializer):
    conversation_id = serializers.UUIDField(read_only=True)
    # Authoritative ingress provenance for agent/runtime consumers.  Clients
    # must not infer Direct versus Group from a connection id or participant
    # count: both live and replay frames are built from this serializer.
    conversation_type = serializers.CharField(
        source='conversation.conversation_type', read_only=True,
    )
    sender_same_org = serializers.SerializerMethodField()
    content = serializers.SerializerMethodField()
    media_url = serializers.SerializerMethodField()
    media_metadata = serializers.SerializerMethodField()
    read_by = serializers.SerializerMethodField()
    delivered_to = serializers.SerializerMethodField()
    reply_to_content = serializers.SerializerMethodField()
    reply_to_sender_name = serializers.SerializerMethodField()
    reply_to_message_type = serializers.SerializerMethodField()
    reply_to_media_url = serializers.SerializerMethodField()

    forwarded_from_content = serializers.SerializerMethodField()
    forwarded_from_sender_name = serializers.SerializerMethodField()
    is_starred = serializers.SerializerMethodField()
    # Alias `conversation_seq_num` → `seqNum` in JSON payload. All three clients
    # already parse `seqNum` via the ordering tri-key (seqNum, createdAt, id);
    # no client-side rename needed. The server column is `conversation_seq_num`
    # for clarity.
    seqNum = serializers.IntegerField(source='conversation_seq_num', read_only=True, allow_null=True)

    def get_content(self, obj):
        # Soft-deleted messages: hide content but keep the row so clients can
        # render a "This message was deleted" tombstone in-place (WhatsApp-style)
        # instead of having the message silently disappear.
        if obj.is_deleted:
            return ''
        return obj.content

    def get_sender_same_org(self, obj):
        """Bounded recipient-relative admission provenance for agent clients.

        Missing recipient context or membership lookup is deliberately false;
        runtimes then fail open by delivering the message rather than silently
        suppressing an external/unknown sender.
        """
        recipient_org_ids = self.context.get('recipient_org_ids')
        if not recipient_org_ids:
            return False
        from connect.services.org_tenancy import get_user_org_ids
        sender_org_ids = get_user_org_ids(obj.sender_id)
        return bool(sender_org_ids and set(recipient_org_ids).intersection(sender_org_ids))

    def get_media_url(self, obj):
        if obj.is_deleted:
            return ''
        return obj.media_url

    def get_media_metadata(self, obj):
        if obj.is_deleted:
            return {}
        return obj.media_metadata

    class Meta:
        model = DirectMessage
        fields = [
            'id', 'conversation_id', 'conversation_type', 'sender_same_org',
            'sender_id', 'sender_email', 'sender_name',
            'sender_username',
            'message_type', 'content', 'media_url', 'media_metadata',
            'shared_post_id', 'reply_to', 'forwarded_from', 'reactions', 'is_edited',
            'is_deleted', 'created_at', 'updated_at',
            'read_by', 'delivered_to', 'reply_to_content', 'reply_to_sender_name',
            'reply_to_message_type', 'reply_to_media_url',
            'forwarded_from_content', 'forwarded_from_sender_name',
            'client_message_id', 'is_starred',
            'sender_is_agent', 'agent_id',
            # Event-log cursor fields. `event_id` is the globally-monotonic
            # BIGINT assigned by the Postgres sequence — clients store the
            # highest they've seen and send it back as `since_event_id` on WS
            # reconnect to replay only what they missed. `seqNum` (aliased
            # from `conversation_seq_num`) is per-conversation monotonic, used
            # for stable ordering independent of clock skew.
            'event_id', 'seqNum',
        ]
        read_only_fields = [
            'id', 'sender_id', 'sender_email', 'sender_name', 'sender_username',
            'created_at', 'updated_at', 'sender_is_agent', 'agent_id',
            'event_id', 'seqNum',
        ]

    def get_read_by(self, obj):
        return list(obj.read_receipts.values_list('user_id', flat=True))

    def get_delivered_to(self, obj):
        return list(obj.delivery_receipts.values_list('user_id', flat=True))

    def get_reply_to_content(self, obj):
        if obj.reply_to:
            return obj.reply_to.content[:100]
        return None

    def get_reply_to_sender_name(self, obj):
        if obj.reply_to:
            return obj.reply_to.sender_name
        return None

    def get_reply_to_message_type(self, obj):
        if obj.reply_to:
            return obj.reply_to.message_type
        return None

    def get_reply_to_media_url(self, obj):
        if obj.reply_to:
            return obj.reply_to.media_url
        return None

    def get_is_starred(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            from connect.models import MessageStar
            return MessageStar.objects.filter(message=obj, user_id=request.user.id).exists()
        return False

    def get_forwarded_from_content(self, obj):
        if obj.forwarded_from:
            return obj.forwarded_from.content[:100]
        return None

    def get_forwarded_from_sender_name(self, obj):
        if obj.forwarded_from:
            return obj.forwarded_from.sender_name
        return None


# Max message content length. Chosen between Telegram (4k) and WhatsApp (64k):
# big enough for long pasted code/docs, small enough to reject abusive payloads
# that bloat the DB and push notifications. Adjust up if real use cases hit this.
MAX_MESSAGE_CONTENT_LENGTH = 16_000


class DirectMessageCreateSerializer(serializers.ModelSerializer):
    client_message_id = serializers.UUIDField(required=False, allow_null=True)
    content = serializers.CharField(
        max_length=MAX_MESSAGE_CONTENT_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
    )

    class Meta:
        model = DirectMessage
        fields = ['message_type', 'content', 'media_url', 'media_metadata', 'reply_to', 'client_message_id']


class ConversationSerializer(serializers.ModelSerializer):
    participants = ConversationParticipantSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    participant_names = serializers.SerializerMethodField()
    is_muted = serializers.SerializerMethodField()
    is_pinned = serializers.SerializerMethodField()
    is_archived = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'conversation_type', 'name', 'description', 'avatar_url',
            'created_by_id', 'message_count', 'last_message_at',
            'last_message_preview', 'created_at', 'updated_at',
            'participants', 'last_message', 'unread_count', 'participant_names',
            'is_muted', 'is_pinned', 'is_archived'
        ]
        read_only_fields = [
            'id', 'message_count', 'last_message_at',
            'last_message_preview', 'created_at', 'updated_at'
        ]

    def _get_current_participant(self, obj):
        """Use prefetch cache from queryset's .prefetch_related('participants')."""
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            user_id = request.user.id
            for p in obj.participants.all():  # hits prefetch cache
                if p.user_id == user_id:
                    return p
        return None

    def get_is_muted(self, obj):
        p = self._get_current_participant(obj)
        return p.is_muted if p else False

    def get_is_pinned(self, obj):
        p = self._get_current_participant(obj)
        return getattr(p, 'is_pinned', False) if p else False

    def get_is_archived(self, obj):
        p = self._get_current_participant(obj)
        return getattr(p, 'is_archived', False) if p else False

    def get_last_message(self, obj):
        if obj.last_message_preview or obj.last_message_at:
            return {
                'content': obj.last_message_preview or '',
                'created_at': str(obj.last_message_at) if obj.last_message_at else None,
            }
        return None

    def get_unread_count(self, obj):
        p = self._get_current_participant(obj)
        return p.unread_count if p else 0

    def get_participant_names(self, obj):
        request = self.context.get('request')
        current_user_id = int(request.user.id) if request and request.user.is_authenticated else None

        active = []
        needs_resolve = []
        for p in obj.participants.all():  # hits prefetch cache
            if p.has_left:
                continue
            if obj.conversation_type == 'direct' and current_user_id and p.user_id == current_user_id:
                continue
            active.append(p)
            if is_placeholder_name(p.user_name, p.user_id):
                needs_resolve.append(p.user_id)

        # CON-36: batch-fetch SocialProfiles for participants missing a real
        # name. Agent accounts often store user_name as "" or "User <id>"
        # when shizuha-id was briefly unreachable at DM-create time.
        context = self.context if isinstance(self.context, dict) else {}
        profiles = context.setdefault('_social_profiles', {})
        missing_profile_ids = [uid for uid in needs_resolve if uid not in profiles]
        if missing_profile_ids:
            for profile in SocialProfile.objects.filter(
                user_id__in=missing_profile_ids
            ).only('user_id', 'username', 'display_name', 'user_email'):
                profiles[profile.user_id] = profile
        identities = cached_identity_users(context, needs_resolve)

        names = []
        heal = []
        for p in active:
            label = preferred_participant_label(
                p, profiles=profiles, identities=identities,
            )
            names.append(label)
            if (
                label
                and is_placeholder_name(p.user_name, p.user_id)
                and not is_placeholder_name(label, p.user_id)
            ):
                p.user_name = label
                heal.append(p)
        if heal:
            try:
                ConversationParticipant.objects.bulk_update(heal, ['user_name'])
            except Exception:
                # Listing must stay read-mostly; a heal failure is not user-visible
                # once `names` already carries the resolved labels.
                pass
        return names


class ConversationCreateSerializer(serializers.ModelSerializer):
    participant_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Conversation
        fields = ['conversation_type', 'name', 'description', 'avatar_url', 'participant_ids']

    def validate(self, data):
        conversation_type = data.get('conversation_type', 'direct')
        participant_ids = data.get('participant_ids', [])

        if conversation_type == 'direct' and len(participant_ids) != 1:
            raise serializers.ValidationError(
                "Direct messages must have exactly one other participant"
            )

        if conversation_type == 'group' and not data.get('name'):
            raise serializers.ValidationError(
                "Group conversations must have a name"
            )

        return data

    def create(self, validated_data):
        validated_data.pop('participant_ids', None)
        return super().create(validated_data)
